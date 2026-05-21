"""HTTP-level tests for the new endpoints.

Spin up the real server on an ephemeral port, hit it with `http.client`,
verify the responses. We stub fetch.fetchAll so no network traffic
happens.

These tests exist because the routing + body parsing + SSE handler in
`server.py` aren't otherwise covered by the existing unit suite.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from ra_log_explorer import exposureTimes
from ra_log_explorer import jobs as jobsModule
from ra_log_explorer import server as serverModule
from ra_log_explorer.config import FetchSpec
from ra_log_explorer.jobs import JobManager
from ra_log_explorer.server import ServerContext, _makeHandler

RunningServer = tuple[str, int, ServerContext]


def _freePort() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def runningServer(tmpCacheRoot: Path) -> Iterator[RunningServer]:
    """Yield (host, port, ctx) for a server bound to an ephemeral port.

    Closes the socket and joins the serve_forever thread at teardown.
    Each test gets a clean cache root via the ``tmpCacheRoot`` fixture
    so cache-listing tests don't see each other's leftovers.
    """
    from http.server import ThreadingHTTPServer

    ctx = ServerContext(jobs=JobManager())
    handler = _makeHandler(ctx)
    port = _freePort()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield "127.0.0.1", port, ctx
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=2.0)


def _get(host: str, port: int, path: str) -> tuple[int, dict]:
    conn = http.client.HTTPConnection(host, port, timeout=2.0)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read().decode("utf-8")
    conn.close()
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        parsed = {"_raw": body}
    return resp.status, parsed


def _post(host: str, port: int, path: str, body: dict) -> tuple[int, dict]:
    conn = http.client.HTTPConnection(host, port, timeout=2.0)
    conn.request("POST", path, body=json.dumps(body), headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    text = resp.read().decode("utf-8")
    conn.close()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = {"_raw": text}
    return resp.status, parsed


def _delete(host: str, port: int, path: str) -> tuple[int, dict]:
    conn = http.client.HTTPConnection(host, port, timeout=2.0)
    conn.request("DELETE", path)
    resp = conn.getresponse()
    text = resp.read().decode("utf-8")
    conn.close()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = {"_raw": text}
    return resp.status, parsed


# ----- /api/summary -------------------------------------------------------


def test_summary_empty_when_no_exposure(runningServer: RunningServer) -> None:
    host, port, _ctx = runningServer
    status, body = _get(host, port, "/api/summary")
    assert status == 200
    assert body["loaded"] is False
    assert "cache" in body


# ----- /api/cache --------------------------------------------------------


def test_cache_lists_completed_window(runningServer: RunningServer, tmpCacheRoot: Path) -> None:
    host, port, _ctx = runningServer
    # Manually plant a cache directory (the file layout is the same as
    # what fetchAll would produce).
    d = tmpCacheRoot / "yagan" / "rapid-analysis" / "2026-05-20T084534_267000Z__2026-05-20T085039_267000Z"
    (d / "pods").mkdir(parents=True)
    (d / "_meta.json").write_text(
        json.dumps(
            {
                "spec": {
                    "lokiAddr": "x",
                    "username": "u",
                    "cluster": "yagan",
                    "namespace": "rapid-analysis",
                    "fromIso": "2026-05-20T08:45:34.267000Z",
                    "toIso": "2026-05-20T08:50:39.267000Z",
                    "workers": 8,
                    "lineLimit": 50000,
                },
                "fetched_at": "2026-05-21T15:00:00+00:00",
                "pod_count": 42,
                "total_bytes": 12345,
                "pod_bytes": {},
                "errors": {},
                "window_in_past": True,
                "fromCache": False,
                "cacheReuse": "none",
            }
        )
    )
    status, body = _get(host, port, "/api/cache")
    assert status == 200
    assert len(body["windows"]) == 1
    w = body["windows"][0]
    assert w["cluster"] == "yagan"
    assert w["namespace"] == "rapid-analysis"
    assert w["podCount"] == 42
    assert w["fromIso"].startswith("2026-05-20T08:45:34")


def test_cache_skips_partial_directories(runningServer: RunningServer, tmpCacheRoot: Path) -> None:
    host, port, _ctx = runningServer
    d = tmpCacheRoot / "yagan" / "rapid-analysis" / "x"
    (d / "pods").mkdir(parents=True)
    (d / "_meta.json").write_text("{}")
    (d / ".partial").write_text("")
    status, body = _get(host, port, "/api/cache")
    assert status == 200
    assert body["windows"] == []


# ----- /api/fetch + /api/fetch/<id>/status --------------------------------


def test_fetch_validates_body(runningServer: RunningServer) -> None:
    host, port, _ctx = runningServer
    status, body = _post(host, port, "/api/fetch", {})
    assert status == 400
    assert "exposureId" in body["error"]


def test_fetch_validates_exposureId_is_int(runningServer: RunningServer) -> None:
    host, port, _ctx = runningServer
    status, body = _post(
        host,
        port,
        "/api/fetch",
        {"exposureId": "not-an-int", "tZero": "2026-05-20T08:46:16.267"},
    )
    assert status == 400


def test_fetch_validates_tZero_required(runningServer: RunningServer) -> None:
    host, port, _ctx = runningServer
    status, body = _post(host, port, "/api/fetch", {"exposureId": 1})
    assert status == 400


def test_fetch_starts_job_and_completes(
    runningServer: RunningServer, tmpCacheRoot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections.abc import Callable

    host, port, ctx = runningServer

    # Stub fetchAll so the job completes against a tmp cache dir.
    def fakeFetchAll(
        spec: FetchSpec,
        progress: Callable[[str, int, int], None] | None = None,
        forceRefresh: bool = False,
    ) -> tuple[Path, dict]:
        cacheDir = tmpCacheRoot / "fake"
        (cacheDir / "pods").mkdir(parents=True)
        return cacheDir, {
            "spec": {},
            "cacheReuse": "none",
            "pod_count": 0,
            "total_bytes": 0,
            "elapsed_s": 0.0,
            "fromCache": False,
        }

    monkeypatch.setattr(jobsModule, "fetchAll", fakeFetchAll)

    status, body = _post(
        host,
        port,
        "/api/fetch",
        {"exposureId": 2026051900722, "tZero": "2026-05-20T08:46:16.267"},
    )
    assert status == 202, body
    jobId = body["jobId"]

    # Poll status until done. Should take well under a second.
    for _ in range(100):
        status, body = _get(host, port, f"/api/fetch/{jobId}/status")
        assert status == 200
        if body["status"] in ("done", "error"):
            break
        time.sleep(0.02)
    assert body["status"] == "done", body
    assert body["expId"] == 2026051900722

    # Server's ServerState should now be populated.
    with ctx.jobs.stateLock:
        assert ctx.state is not None
        assert ctx.state.expId == 2026051900722


def test_fetch_status_404_for_unknown_job(runningServer: RunningServer) -> None:
    host, port, _ctx = runningServer
    status, body = _get(host, port, "/api/fetch/doesnotexist/status")
    assert status == 404
    assert "error" in body


def test_pod_endpoint_404_when_no_state_loaded(runningServer: RunningServer) -> None:
    host, port, _ctx = runningServer
    status, body = _get(host, port, "/api/pod/anything")
    assert status == 404


# ----- _buildSpecFromRequest unit-level coverage --------------------------


def test_buildSpecFromRequest_TAI_default() -> None:
    spec, expId, tZero, password = serverModule._buildSpecFromRequest(
        {"exposureId": 1, "tZero": "2026-05-20T08:46:16.267"}
    )
    assert expId == 1
    # 37s TAI->UTC adjustment applied by default
    assert tZero.hour == 8 and tZero.minute == 45 and tZero.second == 39
    assert spec.cluster == "yagan"
    assert spec.namespace == "rapid-analysis"
    assert password is None


def test_buildSpecFromRequest_UTC_opt_out() -> None:
    _, _, tZero, _ = serverModule._buildSpecFromRequest(
        {"exposureId": 1, "tZero": "2026-05-20T08:45:39.267", "tZeroUtc": True}
    )
    # No 37s offset applied
    assert tZero.hour == 8 and tZero.minute == 45 and tZero.second == 39


def test_buildSpecFromRequest_password_passthrough() -> None:
    _, _, _, password = serverModule._buildSpecFromRequest(
        {"exposureId": 1, "tZero": "2026-05-20T08:46:16.267", "password": "hunter2"}
    )
    assert password == "hunter2"


def test_buildSpecFromRequest_rejects_missing_expId() -> None:
    with pytest.raises(ValueError, match="exposureId"):
        serverModule._buildSpecFromRequest({"tZero": "2026-05-20T08:46:16.267"})


def test_buildSpecFromRequest_rejects_missing_tZero() -> None:
    with pytest.raises(ValueError, match="tZero"):
        serverModule._buildSpecFromRequest({"exposureId": 1})


def test_buildSpecFromRequest_rejects_bad_tZero() -> None:
    with pytest.raises(ValueError, match="ISO"):
        serverModule._buildSpecFromRequest({"exposureId": 1, "tZero": "not a date"})


# ----- /api/exposure-time/<dataId> ----------------------------------------


def _plantExposureTime(monkeypatch: pytest.MonkeyPatch, payload: dict[str, str] | None) -> None:
    """Stub urlopen so queryIsot returns the values we want for one day."""
    import io as _io
    from urllib.error import HTTPError as _HTTPError

    exposureTimes._loadDay.cache_clear()
    monkeypatch.setenv(exposureTimes.EXPOSURE_TIMINGS_URL_ENV, "https://stubbed/")

    def fakeUrlopen(url: str) -> object:
        if payload is None:
            raise _HTTPError(url, 404, "not found", {}, None)  # type: ignore[arg-type]
        return _io.BytesIO(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)


def test_exposure_time_returns_isot(runningServer: RunningServer, monkeypatch: pytest.MonkeyPatch) -> None:
    _plantExposureTime(monkeypatch, {"2026051900722": "2026-05-20T08:46:16.267"})
    host, port, _ = runningServer
    status, body = _get(host, port, "/api/exposure-time/2026051900722")
    assert status == 200
    assert body == {"dataId": 2026051900722, "tZero": "2026-05-20T08:46:16.267", "scale": "TAI"}


def test_exposure_time_404_for_unknown(runningServer: RunningServer, monkeypatch: pytest.MonkeyPatch) -> None:
    _plantExposureTime(monkeypatch, {"some-other-id": "..."})
    host, port, _ = runningServer
    status, body = _get(host, port, "/api/exposure-time/2026051900722")
    assert status == 404
    assert "exposure-time" in body["error"]


def test_exposure_time_404_for_unknown_day(
    runningServer: RunningServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plantExposureTime(monkeypatch, None)
    host, port, _ = runningServer
    status, body = _get(host, port, "/api/exposure-time/2026051900722")
    assert status == 404


def test_exposure_time_503_when_url_not_set(
    runningServer: RunningServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(exposureTimes.EXPOSURE_TIMINGS_URL_ENV, raising=False)
    exposureTimes._loadDay.cache_clear()
    host, port, _ = runningServer
    status, body = _get(host, port, "/api/exposure-time/2026051900722")
    assert status == 503
    assert "EXPOSURE_TIMINGS_URL" in body["error"]


# ----- DELETE /api/cache (single + all) -----------------------------------


def _plantCacheDir(root: Path, cluster: str, namespace: str, slug: str) -> Path:
    d = root / cluster / namespace / slug
    (d / "pods").mkdir(parents=True)
    (d / "_meta.json").write_text(
        json.dumps(
            {
                "spec": {
                    "lokiAddr": "x",
                    "username": "u",
                    "cluster": cluster,
                    "namespace": namespace,
                    "fromIso": "2026-05-20T08:45:00Z",
                    "toIso": "2026-05-20T08:50:00Z",
                    "workers": 8,
                    "lineLimit": 50000,
                },
                "fetched_at": "2026-05-21T15:00:00+00:00",
                "pod_count": 0,
                "total_bytes": 0,
                "pod_bytes": {},
                "errors": {},
                "window_in_past": True,
                "fromCache": False,
                "cacheReuse": "none",
            }
        )
    )
    return d


def test_delete_single_cache_window(runningServer: RunningServer, tmpCacheRoot: Path) -> None:
    host, port, _ = runningServer
    slug = "2026-05-20T084500Z__2026-05-20T085000Z"
    d = _plantCacheDir(tmpCacheRoot, "yagan", "rapid-analysis", slug)
    assert d.exists()
    status, body = _delete(host, port, f"/api/cache/yagan/rapid-analysis/{slug}")
    assert status == 200
    assert body["windows"] == []
    assert not d.exists()


def test_delete_unknown_cache_window(runningServer: RunningServer) -> None:
    host, port, _ = runningServer
    status, body = _delete(host, port, "/api/cache/yagan/rapid-analysis/nope")
    assert status == 404


def test_delete_rejects_path_traversal(runningServer: RunningServer) -> None:
    host, port, _ = runningServer
    # /api/cache/<cluster>/<ns>/<slug> route only matches safe components,
    # but probe a few escape attempts to be sure.
    for bad in [
        "/api/cache/..%2F..%2F..%2Fetc/passwd/x",
        "/api/cache/a/b/..",
        "/api/cache/a/b/.",
    ]:
        status, _ = _delete(host, port, bad)
        assert status == 404, bad


def test_delete_all_cache(runningServer: RunningServer, tmpCacheRoot: Path) -> None:
    host, port, _ = runningServer
    _plantCacheDir(tmpCacheRoot, "yagan", "rapid-analysis", "a__a")
    _plantCacheDir(tmpCacheRoot, "other", "ns", "b__b")
    status, body = _delete(host, port, "/api/cache")
    assert status == 200
    assert body["windows"] == []
    # The root itself should still exist (we recreate it).
    assert tmpCacheRoot.exists()


def test_delete_cache_window_clears_loaded_state_if_match(
    runningServer: RunningServer, tmpCacheRoot: Path
) -> None:
    host, port, ctx = runningServer
    slug = "2026-05-20T084500Z__2026-05-20T085000Z"
    d = _plantCacheDir(tmpCacheRoot, "yagan", "rapid-analysis", slug)
    # Pretend an exposure is loaded against this cache directory.
    with ctx.jobs.stateLock:
        ctx.state = serverModule.ServerState(
            cacheDir=d,
            cacheBytes=0,
            meta={},
            summaries=[],
            expId=1,
            tZero=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
    status, _ = _delete(host, port, f"/api/cache/yagan/rapid-analysis/{slug}")
    assert status == 200
    with ctx.jobs.stateLock:
        assert ctx.state is None
