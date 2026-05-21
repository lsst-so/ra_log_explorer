"""Stdlib HTTP server: timeline UI + JSON API + on-demand fetches.

The server runs against a long-lived :class:`ServerContext` that holds:

  * the currently-loaded ``ServerState`` (or ``None`` — the "home" mode),
  * a :class:`~ra_log_explorer.jobs.JobManager` for fetch jobs the user
    kicks off from the home page,
  * a thread lock guarding ``state`` so an in-progress fetch can swap it
    in atomically.

HTTP surface (see ``architecture/architecture.md`` for the full schema):

  GET  /                            timeline.html (home + explore SPA)
  GET  /static/*                    static assets
  GET  /api/summary                 current loaded exposure, or {state: null}
  GET  /api/pod/<pod>               full parsed log for one pod
  GET  /api/cache                   list of cached windows on disk
  POST /api/fetch                   start a fetch; returns {jobId}
  GET  /api/fetch/<id>/status       JSON snapshot of a fetch job
  GET  /api/fetch/<id>/progress     SSE stream of fetch progress events
"""

from __future__ import annotations

import datetime as dt
import json
import mimetypes
import re
from dataclasses import asdict, dataclass, field, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import parse as parser
from .config import FetchSpec, cache_root
from .fetch import cacheDuSizeBytes, loadPodLogPath
from .jobs import FetchJob, JobManager

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"


# Palette tuned to be visually distinguishable on white. The first two
# entries are pinned to `isr` and `calibrateImage` so the most common bars
# always get the same colour; everything else is assigned by position in
# sorted order, guaranteeing zero collisions until we exceed the palette
# size. The full LSSTCam SFM+AOS step1a/step1b pipeline graph today emits
# ~16 distinct task labels, well under the 24 here.
_TASK_PALETTE = [
    "#d4801f",  # 0 orange (pinned: isr)
    "#1a9c8c",  # 1 teal   (pinned: calibrateImage)
    "#1d6fb0",  # 2 blue
    "#6a4ea3",  # 3 purple
    "#c1252b",  # 4 red
    "#4d9221",  # 5 green
    "#c41a85",  # 6 magenta
    "#a05f00",  # 7 brown
    "#168aad",  # 8 cyan
    "#842cad",  # 9 violet
    "#d4b500",  # 10 gold
    "#8b1ab3",  # 11 deep purple
    "#5b8a3f",  # 12 olive
    "#306b6b",  # 13 dark teal
    "#a52a2a",  # 14 dark red
    "#d96a9d",  # 15 pink
    "#005f73",  # 16 dark cyan
    "#9b59b6",  # 17 mauve
    "#27ae60",  # 18 emerald
    "#e67e22",  # 19 amber
    "#7f7f7f",  # 20 grey
    "#2c3e50",  # 21 slate
    "#16a085",  # 22 sea green
    "#34495e",  # 23 charcoal
]
_TASK_COLOR_PINNED = {
    "isr": _TASK_PALETTE[0],
    "calibrateImage": _TASK_PALETTE[1],
}


def _assignTaskColors(tasks: list[str]) -> dict[str, str]:
    """Return a deterministic {task: hex} mapping with no collisions.

    Sort order is alphabetical for stability across re-renders within a
    single run. We do not try to make the mapping stable across different
    *exposures* — pin the few important tasks in ``_TASK_COLOR_PINNED`` if
    cross-exposure consistency matters for that one.

    Pinned palette entries are only excluded from the rotation when the
    pinned task is actually present in ``tasks``; otherwise the full
    palette is available, which matters when a view doesn't include
    ``isr`` / ``calibrateImage``.
    """
    result: dict[str, str] = {}
    others: list[str] = []
    for t in sorted(tasks):
        if t in _TASK_COLOR_PINNED:
            result[t] = _TASK_COLOR_PINNED[t]
        else:
            others.append(t)
    usedPinned = set(result.values())
    available = [c for c in _TASK_PALETTE if c not in usedPinned]
    for i, t in enumerate(others):
        result[t] = available[i % len(available)]
    return result


@dataclass
class ServerState:
    """A loaded exposure's worth of parsed data."""

    cacheDir: Path
    cacheBytes: int
    meta: dict  # cache meta produced by fetch.fetchAll
    summaries: list[parser.PodSummary]
    expId: int
    tZero: dt.datetime
    referencePoints: list[dict] = field(default_factory=list)


@dataclass
class ServerContext:
    """Long-lived per-process state shared between the handler threads."""

    jobs: JobManager
    state: ServerState | None = None  # mutate only while holding jobs.stateLock


def _toJsonable(obj: Any) -> Any:
    if isinstance(obj, dt.datetime):
        return obj.isoformat()
    if isinstance(obj, set):
        return sorted(obj)
    if isinstance(obj, Path):
        return str(obj)
    if is_dataclass(obj) and not isinstance(obj, type):
        return _toJsonable(asdict(obj))
    if isinstance(obj, dict):
        return {k: _toJsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_toJsonable(x) for x in obj]
    return obj


def _eventToDict(ev: parser.Event, tZero: dt.datetime) -> dict:
    return {
        "pod": ev.pod,
        "t": ev.t.isoformat(),
        "offsetS": (ev.t - tZero).total_seconds(),
        "kind": ev.kind,
        "level": ev.level,
        "expId": ev.expId,
        "detector": ev.detector,
        "visit": ev.visit,
        "who": ev.who,
        "taskLabel": ev.taskLabel,
        "durationS": ev.durationS,
        "flavor": ev.flavor,
        "message": ev.message,
        "raw": ev.raw,
    }


def _summaryToDict(s: parser.PodSummary, tZero: dt.datetime, expId: int) -> dict:
    """Build the per-pod payload for the timeline view.

    Events that explicitly reference `expId` are always included. Generic
    WARN/ERROR events (which carry no expId by construction) are included
    only if they fall inside the temporal window where this pod was working
    on the target exposure — defined as the interval between the first and
    last explicitly-relevant event, padded by a few seconds on either side.
    This keeps unrelated warnings from the previous/next exposure from
    cluttering the per-pod timeline.
    """
    targeted = [ev for ev in s.events if ev.expId == expId]
    window: tuple[dt.datetime, dt.datetime] | None = None
    if targeted:
        window = (
            targeted[0].t - dt.timedelta(seconds=3),
            targeted[-1].t + dt.timedelta(seconds=3),
        )

    relevant: list[parser.Event] = []
    for ev in s.events:
        if ev.expId == expId:
            relevant.append(ev)
            continue
        if ev.expId is not None:
            continue  # explicitly tagged to a different exposure
        # Untagged event (typically a generic WARN/ERROR). Keep iff it
        # falls inside this pod's working window for the target exposure.
        if window is None:
            # Pod has no explicit target events at all (e.g. head node only
            # references the exposure transitively via expRecord). Keep
            # untagged events so the user can still see warnings.
            relevant.append(ev)
        elif window[0] <= ev.t <= window[1]:
            relevant.append(ev)

    return {
        "pod": s.pod,
        "group": s.group,
        "instrument": s.instrument,
        "ordinal": s.ordinal,
        "nLines": s.nLines,
        "nWarn": s.nWarn,
        "nError": s.nError,
        "nTraceback": s.nTraceback,
        "firstTs": s.firstTs.isoformat() if s.firstTs else None,
        "lastTs": s.lastTs.isoformat() if s.lastTs else None,
        "events": [_eventToDict(ev, tZero) for ev in relevant],
    }


def _buildSummaryPayload(state: ServerState) -> dict:
    matchingSummaries = parser.podsTouchingExp(state.summaries, state.expId)
    refs = list(state.referencePoints)
    # Head node's first acknowledgement of this exposure — the moment
    # ButlerWatcher+head observed it as "ready to process". Useful for
    # subtracting out readout + Butler ingest latency.
    firstDefineVisit: parser.Event | None = None
    for s in matchingSummaries:
        if s.group != "head":
            continue
        for ev in s.events:
            if ev.expId != state.expId:
                continue
            if ev.kind == "HEAD_DEFINE_VISIT" and (firstDefineVisit is None or ev.t < firstDefineVisit.t):
                firstDefineVisit = ev
    if firstDefineVisit is not None:
        refs.append(
            {
                "label": "head node first defined visit",
                "t": firstDefineVisit.t.isoformat(),
                "offsetS": (firstDefineVisit.t - state.tZero).total_seconds(),
                "source": "head",
            }
        )

    # Discover every distinct task label across the relevant pods so we can
    # emit a collision-free colour map for the timeline + dynamic legend.
    taskLabels: set[str] = set()
    for s in matchingSummaries:
        for ev in s.events:
            if ev.taskLabel:
                taskLabels.add(ev.taskLabel)
    taskColors = _assignTaskColors(sorted(taskLabels))

    return {
        "loaded": True,
        "expId": state.expId,
        "tZero": state.tZero.isoformat(),
        "cacheDir": str(state.cacheDir),
        "cacheBytes": state.cacheBytes,
        "meta": _toJsonable(state.meta),
        "referencePoints": refs,
        "taskColors": taskColors,
        "pods": [_summaryToDict(s, state.tZero, state.expId) for s in matchingSummaries],
        "podsAll": [
            {"pod": s.pod, "group": s.group, "nLines": s.nLines, "nWarn": s.nWarn, "nError": s.nError}
            for s in state.summaries
        ],
    }


def _podDetail(state: ServerState, pod: str) -> dict:
    logPath = loadPodLogPath(state.cacheDir, pod)
    lines: list[dict] = []
    for ln in parser.iterPodLines(logPath):
        lines.append(
            {
                "t": ln.timestamp.isoformat(),
                "offsetS": (ln.timestamp - state.tZero).total_seconds(),
                "level": ln.level,
                "logger": ln.logger,
                "function": ln.function,
                "message": ln.message,
                "raw": ln.raw,
            }
        )
    return {"pod": pod, "lines": lines}


# ----- cache listing --------------------------------------------------------


def _listCacheWindows() -> list[dict]:
    """Inspect the cache root and summarise each completed window.

    Skips directories without `_meta.json` (uninitialised) and those that
    still have a `.partial` flag (a crashed fetch). The result is sorted
    most-recent-fetched-first so the home page's "recent runs" list reads
    chronologically.
    """
    root = cache_root()
    rows: list[dict] = []
    if not root.exists():
        return rows
    for cluster in sorted(p for p in root.iterdir() if p.is_dir()):
        for ns in sorted(p for p in cluster.iterdir() if p.is_dir()):
            for window in sorted(p for p in ns.iterdir() if p.is_dir()):
                metaPath = window / "_meta.json"
                if not metaPath.exists() or (window / ".partial").exists():
                    continue
                try:
                    meta = json.loads(metaPath.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                spec = meta.get("spec") or {}
                rows.append(
                    {
                        "cluster": cluster.name,
                        "namespace": ns.name,
                        "windowDir": window.name,
                        "fromIso": spec.get("fromIso"),
                        "toIso": spec.get("toIso"),
                        "fetchedAt": meta.get("fetched_at"),
                        "podCount": meta.get("pod_count", 0),
                        "totalBytes": meta.get("total_bytes", 0),
                        "sizeOnDisk": cacheDuSizeBytes(window),
                    }
                )
    rows.sort(key=lambda r: r.get("fetchedAt") or "", reverse=True)
    return rows


def _cacheRootInfo() -> dict:
    root = cache_root()
    return {
        "path": str(root),
        "totalBytes": cacheDuSizeBytes(root),
    }


# ----- request handling ----------------------------------------------------


def _readJsonBody(handler: BaseHTTPRequestHandler) -> Any:
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    return json.loads(raw)


def _onFetchComplete(ctx: ServerContext) -> Any:
    """Return a callback that swaps in a new ServerState after a fetch."""

    def cb(job: FetchJob) -> None:
        if job.cacheDir is None:
            return  # fetchAll raised; caller will see an error event
        summaries = parser.summarizeAll(job.cacheDir)
        newState = ServerState(
            cacheDir=job.cacheDir,
            cacheBytes=cacheDuSizeBytes(cache_root()),
            meta=job.meta,
            summaries=summaries,
            expId=job.expId,
            tZero=job.tZero,
            referencePoints=[
                {
                    "label": "shutter close (caller-supplied)",
                    "t": job.tZero.isoformat(),
                    "offsetS": 0.0,
                    "source": "shutter close",
                }
            ],
        )
        with ctx.jobs.stateLock:
            ctx.state = newState

    return cb


def _makeHandler(ctx: ServerContext) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        # HTTP/1.0 is the default; that's fine for SSE because the spec
        # supports "read until close" — we just don't get keep-alive.

        def _send_json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_error_json(self, status: int, message: str) -> None:
            self._send_json({"error": message}, status=status)

        def _send_file(self, path: Path) -> None:
            if not path.exists() or not path.is_file():
                self.send_error(404, f"Not found: {path.name}")
                return
            mime, _ = mimetypes.guess_type(path.name)
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return  # silence default access logs

        # ----- SSE helper -----

        def _send_sse(self, job: FetchJob) -> None:
            """Stream a job's progress events as Server-Sent Events.

            Replays history then waits on the job's condition variable for
            further events until a terminal one (``done`` / ``error``) is
            sent. Safe for multiple concurrent readers (each starts at
            index 0 of the event list independently).
            """
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            idx = 0
            try:
                while True:
                    with job.condition:
                        # Wait for more events unless the job is already terminal
                        # AND we've drained everything that was produced.
                        while idx >= len(job.events) and not job.isTerminal():
                            job.condition.wait(timeout=15.0)
                        toSend = job.events[idx:]
                        idx = len(job.events)
                        terminalReached = job.isTerminal()
                    for ev in toSend:
                        chunk = f"data: {json.dumps(ev)}\n\n".encode("utf-8")
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    if terminalReached and idx >= len(job.events):
                        return
                    if not toSend:
                        # No events arrived in the wait window — send a comment
                        # ping so clients / proxies don't time out the stream.
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return  # client went away

        # ----- routing -----

        def do_GET(self) -> None:  # noqa: N802 (stdlib API)
            url = urlparse(self.path)
            path = url.path
            if path in ("/", "/index.html"):
                self._send_file(TEMPLATES_DIR / "timeline.html")
                return
            if path.startswith("/static/"):
                rel = path[len("/static/") :]
                if ".." in rel.split("/"):
                    self.send_error(400)
                    return
                self._send_file(STATIC_DIR / rel)
                return
            if path == "/api/summary":
                with ctx.jobs.stateLock:
                    state = ctx.state
                if state is None:
                    self._send_json({"loaded": False, "cache": _cacheRootInfo()})
                    return
                self._send_json(_buildSummaryPayload(state))
                return
            if path.startswith("/api/pod/"):
                with ctx.jobs.stateLock:
                    state = ctx.state
                if state is None:
                    self._send_error_json(404, "No exposure loaded")
                    return
                pod = path[len("/api/pod/") :].split("?")[0]
                if not re.match(r"^[A-Za-z0-9._-]+$", pod):
                    self.send_error(400, "Invalid pod name")
                    return
                self._send_json(_podDetail(state, pod))
                return
            if path == "/api/cache":
                self._send_json({"root": _cacheRootInfo(), "windows": _listCacheWindows()})
                return
            m = re.match(r"^/api/fetch/([A-Za-z0-9]+)/status$", path)
            if m:
                job = ctx.jobs.getJob(m.group(1))
                if job is None:
                    self._send_error_json(404, "No such job")
                    return
                self._send_json(
                    {
                        "jobId": job.jobId,
                        "status": job.status,
                        "expId": job.expId,
                        "tZero": job.tZero.isoformat(),
                        "fromIso": job.spec.fromIso,
                        "toIso": job.spec.toIso,
                        "startedAt": job.startedAt.isoformat() if job.startedAt else None,
                        "finishedAt": job.finishedAt.isoformat() if job.finishedAt else None,
                        "cacheDir": str(job.cacheDir) if job.cacheDir else None,
                        "cacheReuse": job.meta.get("cacheReuse"),
                        "error": job.error,
                        "eventCount": len(job.events),
                    }
                )
                return
            m = re.match(r"^/api/fetch/([A-Za-z0-9]+)/progress$", path)
            if m:
                job = ctx.jobs.getJob(m.group(1))
                if job is None:
                    self._send_error_json(404, "No such job")
                    return
                self._send_sse(job)
                return
            self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            if url.path == "/api/fetch":
                try:
                    body = _readJsonBody(self)
                except json.JSONDecodeError as e:
                    self._send_error_json(400, f"Bad JSON body: {e}")
                    return
                # Build the spec from the request, applying server-side defaults
                # where the client didn't supply a value. We never trust the
                # client to set arbitrary host/proto values for logcli.
                try:
                    spec, expId, tZero, password = _buildSpecFromRequest(body)
                except ValueError as e:
                    self._send_error_json(400, str(e))
                    return
                # The password — if any — is consumed by the fetch worker
                # thread (it sets LOKI_PASSWORD in the subprocess env) and
                # never persisted, returned, or logged.
                _maybeSetLokiPassword(password)
                job = ctx.jobs.createJob(spec, expId, tZero)
                ctx.jobs.startJob(job, onComplete=_onFetchComplete(ctx))
                self._send_json({"jobId": job.jobId}, status=202)
                return
            self.send_error(404)

    return Handler


# ----- request body helpers -------------------------------------------------


def _buildSpecFromRequest(body: dict) -> tuple[FetchSpec, int, dt.datetime, str | None]:
    """Translate a JSON fetch request body into (FetchSpec, expId, tZero, password).

    Raises ``ValueError`` for client-fixable mistakes (missing fields,
    unparseable timestamp); the handler converts those into a 400 response.
    """
    from .config import (
        DEFAULT_CLUSTER,
        DEFAULT_LOKI_ADDR,
        DEFAULT_NAMESPACE,
        DEFAULT_USERNAME,
        DEFAULT_WINDOW_AFTER_S,
        DEFAULT_WINDOW_BEFORE_S,
        DEFAULT_WORKERS,
    )

    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object")

    expIdRaw = body.get("exposureId")
    if expIdRaw is None:
        raise ValueError("exposureId is required")
    try:
        expId = int(expIdRaw)
    except (TypeError, ValueError) as e:
        raise ValueError("exposureId must be an integer") from e

    tZeroStr = body.get("tZero")
    if not tZeroStr:
        raise ValueError("tZero is required (ISO-8601 string)")
    tZeroInput = _parseClientIso(tZeroStr)
    if body.get("tZeroUtc", False):
        tZero = tZeroInput
    else:
        tZero = tZeroInput - dt.timedelta(seconds=37.0)  # TAI -> UTC

    windowBefore = float(body.get("windowBefore", DEFAULT_WINDOW_BEFORE_S))
    windowAfter = float(body.get("windowAfter", DEFAULT_WINDOW_AFTER_S))
    fromT = tZero - dt.timedelta(seconds=windowBefore)
    toT = tZero + dt.timedelta(seconds=windowAfter)

    spec = FetchSpec(
        lokiAddr=str(body.get("lokiAddr") or DEFAULT_LOKI_ADDR),
        username=str(body.get("username") or DEFAULT_USERNAME),
        cluster=str(body.get("cluster") or DEFAULT_CLUSTER),
        namespace=str(body.get("namespace") or DEFAULT_NAMESPACE),
        fromIso=_isoForLogcli(fromT),
        toIso=_isoForLogcli(toT),
        workers=int(body.get("workers") or DEFAULT_WORKERS),
    )
    password = body.get("password")
    if password is not None:
        password = str(password)
    return spec, expId, tZero, password


def _parseClientIso(s: str) -> dt.datetime:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if "+" not in s and "-" not in s[10:]:
        s = s + "+00:00"
    try:
        return dt.datetime.fromisoformat(s).astimezone(dt.timezone.utc)
    except ValueError as e:
        raise ValueError(f"tZero is not a valid ISO-8601 timestamp: {e}") from e


def _isoForLogcli(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _maybeSetLokiPassword(password: str | None) -> None:
    """If the client provided a password, set LOKI_PASSWORD in our process env.

    `fetch._run_logcli` reads LOKI_PASSWORD from `os.environ` when spawning
    logcli. Setting it here is *process-global*; we accept that risk because
    the tool is a single-user local UI. The password is never echoed back
    in any response, logged, or written to disk.
    """
    if not password:
        return
    import os

    os.environ["LOKI_PASSWORD"] = password


# ----- public entry point ---------------------------------------------------


def serve(host: str, port: int, ctx: ServerContext) -> None:
    handler = _makeHandler(ctx)
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"Serving on http://{host}:{port} (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        httpd.server_close()
