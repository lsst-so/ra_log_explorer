"""Harness for the browser tests: a real page against a real server.

These drive Chromium against the actual application — the real HTTP
server, the real cache on disk, real captured logs (see :mod:`corpus`) —
so a test failure means a user-visible thing broke, not that a mock
drifted. The only thing standing in for reality is ``logcli``: there is
no Loki to talk to, so :func:`fakeLogcli` answers queries out of the
corpus instead.

**Missing dependencies fail; they never skip.** A UI suite that silently
skips itself reads exactly like a passing one, in CI and in a terminal
alike, and the whole point of these tests is to notice when the UI
breaks. If Playwright or its browser is absent you get an error telling
you what to install.
"""

from __future__ import annotations

import http.client
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

_INSTALL_HINT = (
    "The UI tests need Playwright and its Chromium build:\n"
    "    pip install -e '.[ui-test]'\n"
    "    playwright install chromium\n"
    "They fail rather than skip when it is missing — a UI suite that skips "
    "itself is indistinguishable from one that passes."
)

try:
    from playwright.sync_api import Page, Route, expect, sync_playwright
except ImportError as e:  # pragma: no cover - exercised by not having it installed
    raise ImportError(f"{e}\n\n{_INSTALL_HINT}") from e

# Imported after the guard above on purpose: if Playwright is missing,
# the message about installing it is what should surface, not an error
# from somewhere further down.
from ra_log_explorer import fetch  # noqa: E402
from ra_log_explorer.jobs import JobManager  # noqa: E402
from ra_log_explorer.server import ServerContext, _makeHandler  # noqa: E402

from .corpus import StagedCorpus, linkInto, unpackCorpus  # noqa: E402

# Assertions wait for the UI to catch up rather than sampling it once.
# Every render here is driven by a fetch that has already been issued, so
# a couple of seconds is generous; a longer one only makes a genuine
# failure take longer to report.
expect.set_options(timeout=5_000)


_UI_DIR = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark the tests in this directory ``ui`` so they can be selected.

    The hook is global even though the conftest is not, so it has to
    check the path: marking the whole session ``ui`` would make
    ``-m "not ui"`` deselect everything.
    """
    for item in items:
        if _UI_DIR in Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.ui)


def pytest_configure(config: pytest.Config) -> None:
    """Refuse to start without the browser, once and legibly.

    Left to itself this surfaces as Playwright's own error repeated on
    every test in the directory, which buries the one line that says what
    to install. Checked here so it is a single message before anything
    runs — and an error, not a skip.
    """
    if hasattr(config, "workerinput"):
        # An xdist worker: the controller already checked, and starting a
        # driver per worker just to re-check litters the run's output
        # with cancelled-task noise at exit.
        return
    # Resolve the path inside the context manager and raise outside it:
    # throwing through Playwright's teardown buries the message in async
    # cancellation noise.
    with sync_playwright() as pw:
        executable = pw.chromium.executable_path
    if not Path(executable).exists():  # pragma: no cover - depends on the machine
        raise pytest.UsageError(f"Chromium is not installed at {executable}.\n\n{_INSTALL_HINT}")


@pytest.fixture(scope="session")
def uiCorpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The unpacked log corpus, shared by every test in the session."""
    # getbasetemp().parent is shared across xdist workers; unpackCorpus
    # is safe against several of them racing for it.
    return unpackCorpus(tmp_path_factory.getbasetemp().parent / "ra-ui-corpus")


@pytest.fixture
def liveCorpus(corpus: StagedCorpus) -> StagedCorpus:
    """The corpus with live mode in mind — the sidecar is what makes the
    night sliceable, and it is there by default."""
    return corpus


@pytest.fixture
def corpus(uiCorpus: Path, tmpCacheRoot: Path) -> StagedCorpus:
    """The corpus hard-linked into this test's own cache root.

    The live-night sidecar is left in place, which is what lets
    ``stage*`` slice windows out of it. Tests that want a *fetch* to
    really happen call ``dropLiveSidecar()`` once they have staged
    whatever they need — otherwise the slice path would serve the window
    and the fetch under test would never run.
    """
    return linkInto(uiCorpus, tmpCacheRoot)


def _freePort() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class RunningApp:
    """A server under test, and the browser page pointed at it."""

    page: Page
    ctx: ServerContext
    origin: str
    basePath: str

    def url(self, path: str = "/") -> str:
        return f"{self.origin}{self.basePath}{path}"

    def goto(self, path: str = "/") -> None:
        self.page.goto(self.url(path))

    def apiJson(self, path: str) -> Any:
        """Ask the server directly — for asserting on state the UI acted on."""
        import json

        host, _, port = self.origin.removeprefix("http://").partition(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=5.0)
        try:
            conn.request("GET", f"{self.basePath}{path}")
            return json.loads(conn.getresponse().read() or b"null")
        finally:
            conn.close()


def _serve(ctx: ServerContext) -> Iterator[str]:
    port = _freePort()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _makeHandler(ctx))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2.0)


@pytest.fixture
def blockExternalRequests(page: Page) -> None:
    """Stop the page reaching the internet.

    The template pulls webfonts from a CDN. Letting a test suite depend
    on that makes it slower, flakier, and untrue offline; the fonts have
    no bearing on anything asserted here.
    """
    page.route("https://fonts.googleapis.com/**", lambda route: route.abort())
    page.route("https://fonts.gstatic.com/**", lambda route: route.abort())


@pytest.fixture
def appFactory(
    page: Page,
    corpus: StagedCorpus,
    siteCatalog: Any,
    blockExternalRequests: None,
) -> Iterator[Any]:
    """Build a :class:`RunningApp`, optionally under a base path.

    A factory rather than a plain fixture because the base path is a
    per-test choice: it is a deployment-shaped detail that has broken for
    real, so a couple of tests want it and the rest don't.
    """
    servers: list[Iterator[str]] = []

    def make(basePath: str = "", live: Any = None) -> RunningApp:
        ctx = ServerContext(
            jobs=JobManager(),
            sites=siteCatalog.catalog,
            siteName=siteCatalog.defaultName,
            basePath=basePath,
            live=live,
        )
        gen = _serve(ctx)
        servers.append(gen)
        origin = next(gen)
        return RunningApp(page=page, ctx=ctx, origin=origin, basePath=basePath)

    yield make
    for gen in servers:
        for _ in gen:  # run the generator's teardown
            pass


@pytest.fixture
def app(appFactory: Any) -> RunningApp:
    """The common case: a server at the root, nothing loaded yet."""
    return appFactory()


@pytest.fixture
def fakeLogcli(corpus: StagedCorpus, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Answer ``logcli`` out of the corpus instead of a cluster.

    Enough of the wire behaviour to drive a real fetch through the UI:
    ``series`` lists the pods with lines in the window, ``query`` writes
    that pod's lines for it, ``instant-query`` answers the count oracle.
    Deliberately re-implements the window filter rather than calling the
    slicer, so a bug in slicing can't hide behind a fixture that shares
    it.
    """
    import datetime as dt
    import json as jsonlib

    from ra_log_explorer.parse import _parseTimestamp

    calls: list[str] = []
    podsDir = corpus.nightDir / fetch.PODS_DIR_NAME

    def linesIn(pod: str, fromT: dt.datetime, toT: dt.datetime) -> list[bytes]:
        f = podsDir / f"{pod}.jsonl"
        if not f.exists():
            return []
        out = []
        with open(f, "rb") as fh:
            for raw in fh:
                try:
                    ts = _parseTimestamp(jsonlib.loads(raw)["timestamp"])
                except Exception:  # noqa: BLE001 - a malformed line is not this fixture's problem
                    continue
                if fromT <= ts < toT:
                    out.append(raw)
        return out

    def parseArg(args: list[str], flag: str) -> str:
        for a in args:
            if a.startswith(flag):
                return a[len(flag) :]
        return ""

    def fake(spec: Any, extraArgs: list[str], **kw: Any) -> bytes:
        calls.append(extraArgs[0])
        stdoutPath = kw.get("stdoutPath")
        if extraArgs[0] == "series":
            fromT = fetch._parseIso(parseArg(extraArgs, "--from="))
            toT = fetch._parseIso(parseArg(extraArgs, "--to="))
            matcher = extraArgs[1]
            import re as relib

            m = relib.search(r'pod=~"([^"]*)"', matcher)
            podRe = relib.compile(m.group(1)) if m else None
            out = []
            for f in sorted(podsDir.glob("*.jsonl")):
                pod = f.stem
                if podRe is not None and podRe.fullmatch(pod) is None:
                    continue
                if linesIn(pod, fromT, toT):
                    out.append(f'{{pod="{pod}"}}')
            return ("\n".join(out) + "\n").encode()
        if extraArgs[0] == "instant-query":
            return b""  # oracle unavailable => the chunker bisects blindly, which is fine
        # A log query: {…pod="X"…} plus --from/--to.
        import re as relib

        matcher = next((a for a in extraArgs if a.startswith("{")), "")
        m = relib.search(r'pod="([^"]*)"', matcher)
        if m is None or 'job="k8s/events"' in matcher:
            blob = b""
        else:
            fromT = fetch._parseIso(parseArg(extraArgs, "--from="))
            toT = fetch._parseIso(parseArg(extraArgs, "--to="))
            blob = b"".join(linesIn(m.group(1), fromT, toT))
        if stdoutPath is not None:
            Path(stdoutPath).write_bytes(blob)
            return b""
        return blob

    monkeypatch.setattr(fetch, "_run_logcli", fake)
    return calls


def routeJson(page: Page, pattern: str, payload: Any) -> None:
    """Serve a canned JSON body for every request matching ``pattern``."""
    import json as jsonlib

    def handler(route: Route) -> None:
        route.fulfill(status=200, content_type="application/json", body=jsonlib.dumps(payload))

    page.route(pattern, handler)
