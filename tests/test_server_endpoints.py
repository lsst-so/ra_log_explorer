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
from typing import Any

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


def test_night_fetch_validates_dayObs(runningServer: RunningServer) -> None:
    host, port, _ctx = runningServer
    status, body = _post(host, port, "/api/fetch-night", {})
    assert status == 400
    assert "dayObs" in body["error"]
    status, body = _post(host, port, "/api/fetch-night", {"dayObs": 12345})
    assert status == 400
    assert "YYYYMMDD" in body["error"]


def test_night_fetch_starts_job_and_populates_NightState(
    runningServer: RunningServer, tmpCacheRoot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections.abc import Callable

    host, port, ctx = runningServer

    def fakeFetchAll(
        spec: FetchSpec,
        progress: Callable[[str, int, int], None] | None = None,
        forceRefresh: bool = False,
    ) -> tuple[Path, dict]:
        # The night-spec must carry the AOS pod-regex.
        assert spec.podRegex == ".*aos.*"
        cacheDir = tmpCacheRoot / "fake-night"
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

    status, body = _post(host, port, "/api/fetch-night", {"dayObs": 20260521})
    assert status == 202, body
    jobId = body["jobId"]

    for _ in range(100):
        status, body = _get(host, port, f"/api/fetch/{jobId}/status")
        assert status == 200
        if body["status"] in ("done", "error"):
            break
        time.sleep(0.02)
    assert body["status"] == "done", body
    assert body["kind"] == "night"
    assert body["dayObs"] == 20260521

    with ctx.jobs.stateLock:
        assert ctx.state is None
        assert ctx.nightState is not None
        assert ctx.nightState.dayObs == 20260521


def test_summary_mode_field_distinguishes_exposure_from_night(
    runningServer: RunningServer, tmpCacheRoot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a night is loaded, /api/summary reports mode='night' with the
    night-shaped payload — not the exposure payload."""
    from ra_log_explorer.server import NightState

    host, port, ctx = runningServer
    cacheDir = tmpCacheRoot / "fake-night"
    (cacheDir / "pods").mkdir(parents=True)
    with ctx.jobs.stateLock:
        ctx.nightState = NightState(
            cacheDir=cacheDir,
            cacheBytes=0,
            meta={},
            summaries=[],
            dayObs=20260521,
            startTime=serverModule.dayObsStartUtc(20260521),
            endTime=serverModule.dayObsEndUtc(20260521),
        )
    # Block the token lookup so the histogram code falls back to empty.
    monkeypatch.delenv(exposureTimes.RSP_TOKEN_FILE_ENV, raising=False)
    status, body = _get(host, port, "/api/summary")
    assert status == 200
    assert body["loaded"] is True
    assert body["mode"] == "night"
    assert body["dayObs"] == 20260521
    assert body["stats"]["nTracebacks"] == 0


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


def _plantExposureTime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    obsEnd: str | None,
) -> Path:
    """Drop a fake token file in `tmp_path`, point the env at it, and stub
    `urlopen` so queryIsot returns ``obsEnd`` (or ``None`` if no row).
    """
    tokFile = tmp_path / "tok"
    tokFile.write_text("fake-token")
    monkeypatch.setenv(exposureTimes.RSP_TOKEN_FILE_ENV, str(tokFile))

    import io as _io

    def fakeUrlopen(req: object, **_kw: Any) -> object:
        payload = (
            {"columns": ["obs_end"], "data": [[obsEnd]]}
            if obsEnd is not None
            else {"columns": ["obs_end"], "data": []}
        )
        return _io.BytesIO(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    return tokFile


def test_exposure_time_returns_isot(
    runningServer: RunningServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Steer the cache file into a tmp path so this run doesn't pollute
    # (or accidentally read from) the developer's real cache.
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path / "cache"))
    _plantExposureTime(monkeypatch, tmp_path, "2026-05-20T08:46:16.267000")
    host, port, _ = runningServer
    status, body = _get(host, port, "/api/exposure-time/2026051900722")
    assert status == 200
    assert body == {
        "dataId": 2026051900722,
        "tZero": "2026-05-20T08:46:16.267000",
        "scale": "TAI",
        "fromCache": False,
    }


def test_exposure_time_404_when_no_row_anywhere(
    runningServer: RunningServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _plantExposureTime(monkeypatch, tmp_path, None)
    host, port, _ = runningServer
    status, body = _get(host, port, "/api/exposure-time/2026051900722")
    assert status == 404
    assert "exposure-time" in body["error"]


def test_exposure_time_503_when_token_file_missing(
    runningServer: RunningServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(exposureTimes.RSP_TOKEN_FILE_ENV, str(tmp_path / "nope"))
    host, port, _ = runningServer
    status, body = _get(host, port, "/api/exposure-time/2026051900722")
    assert status == 503
    assert "RSP token file not found" in body["error"]


def test_exposure_time_503_when_token_file_empty(
    runningServer: RunningServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tokFile = tmp_path / "tok"
    tokFile.write_text("   \n")
    monkeypatch.setenv(exposureTimes.RSP_TOKEN_FILE_ENV, str(tokFile))
    host, port, _ = runningServer
    status, body = _get(host, port, "/api/exposure-time/2026051900722")
    assert status == 503
    assert "empty" in body["error"]


def test_exposure_time_returns_cached_without_calling_consdb(
    runningServer: RunningServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the dataId is already in the on-disk cache, the endpoint must
    short-circuit: no token needed, no ConsDB call. The cache is
    immutable (exposure end-times never change once recorded)."""
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path))
    exposureTimes.storeCached(2026051900722, "2026-05-20T08:46:16.267000")

    def blowUp(*_args: object, **_kw: object) -> object:
        raise AssertionError("urlopen should not be reached on a cache hit")

    monkeypatch.setattr(exposureTimes, "urlopen", blowUp)
    # No RSP_TOKEN_FILE_ENV either — the cache hit should remove the need.
    monkeypatch.delenv(exposureTimes.RSP_TOKEN_FILE_ENV, raising=False)

    host, port, _ = runningServer
    status, body = _get(host, port, "/api/exposure-time/2026051900722")
    assert status == 200
    assert body["tZero"] == "2026-05-20T08:46:16.267000"
    assert body["fromCache"] is True


def test_exposure_time_writes_to_cache_on_consdb_hit(
    runningServer: RunningServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A successful ConsDB lookup persists the result so the next call
    is instant. The original `fromCache` is False to surface that the
    network was hit; subsequent calls report True."""
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path))
    _plantExposureTime(monkeypatch, tmp_path, "2026-05-20T08:46:16.267000")
    host, port, _ = runningServer
    status1, body1 = _get(host, port, "/api/exposure-time/2026051900722")
    assert status1 == 200
    assert body1["fromCache"] is False
    # Cache file now exists with the entry persisted.
    assert exposureTimes.lookupCached(2026051900722) == "2026-05-20T08:46:16.267000"


def test_exposure_time_accepts_tokenFile_query_param_override(
    runningServer: RunningServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The env var points at a missing file but the UI sends a working
    path via ?tokenFile=... — the override must win."""
    monkeypatch.setenv(exposureTimes.RSP_TOKEN_FILE_ENV, str(tmp_path / "nope"))
    overFile = tmp_path / "real-tok"
    overFile.write_text("fake-token")

    import io as _io

    def fakeUrlopen(req: object, **_kw: Any) -> object:
        return _io.BytesIO(
            json.dumps({"columns": ["obs_end"], "data": [["2026-05-20T08:46:16.267000"]]}).encode("utf-8")
        )

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    host, port, _ = runningServer
    qs = f"?tokenFile={overFile}"
    status, body = _get(host, port, f"/api/exposure-time/2026051900722{qs}")
    assert status == 200
    assert body["tZero"] == "2026-05-20T08:46:16.267000"


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


def test_night_traceback_endpoint_returns_dataId_block(
    runningServer: RunningServer, tmpCacheRoot: Path
) -> None:
    """Clicking a failure row should get back lines from the dataId's
    pickup through the end of its processing block, not just the
    captured traceback body."""
    import datetime as _dt

    from ra_log_explorer import parse as _parse
    from ra_log_explorer.server import NightState

    host, port, ctx = runningServer
    # Plant a one-pod night cache with a known dataId block + traceback.
    cacheDir = tmpCacheRoot / "yagan" / "rapid-analysis" / "win" / "pods=__aos__"
    podsDir = cacheDir / "pods"
    podsDir.mkdir(parents=True)
    podName = "s-lsstcam-run-aos-worker-aosworkerset-3"
    rawLines = [
        # 3 lines BEFORE the dataId is first mentioned — should be excluded.
        ("2026-05-21T22:46:00.000+00:00", "info", "warming up"),
        ("2026-05-21T22:46:05.000+00:00", "info", "still warming up"),
        # First line that mentions the dataId — the context window starts here.
        (
            "2026-05-21T22:47:00.000+00:00",
            "info",
            "2026-05-21 22:47:00,000 worker fn INFO   Running pipeline for 2026052100012 detector 5",
        ),
        (
            "2026-05-21T22:47:05.000+00:00",
            "info",
            "2026-05-21 22:47:05,000 worker fn INFO   isr started",
        ),
        ("2026-05-21T22:47:10.000+00:00", "error", "Traceback (most recent call last):"),
        ("2026-05-21T22:47:10.001+00:00", "error", '  File "/x/run.py", line 7, in run'),
        ("2026-05-21T22:47:10.002+00:00", "error", "RuntimeError: bang"),
        # A new dataId is picked up — carryover advances, so anything
        # from here on belongs to 2026052100013 and is OUT of this
        # traceback's context.
        (
            "2026-05-21T22:48:00.000+00:00",
            "info",
            "2026-05-21 22:48:00,000 worker fn INFO   Running pipeline for 2026052100013 detector 5",
        ),
        ("2026-05-21T22:48:01.000+00:00", "info", "doing the next thing"),
    ]
    podPath = podsDir / f"{podName}.jsonl"
    with open(podPath, "w") as fh:
        for ts, level, raw in rawLines:
            fh.write(
                json.dumps(
                    {
                        "timestamp": ts,
                        "labels": {"detected_level": level},
                        "line": raw + "\n",
                    }
                )
                + "\n"
            )
    summaries = _parse.summarizeAll(cacheDir)
    assert len(summaries) == 1
    s = summaries[0]
    assert s.tracebacks  # the parser found the RuntimeError
    bodyKey = f"{s.pod}@{s.tracebacks[0].t.isoformat()}"

    with ctx.jobs.stateLock:
        ctx.nightState = NightState(
            cacheDir=cacheDir,
            cacheBytes=0,
            meta={},
            summaries=summaries,
            dayObs=20260521,
            startTime=_dt.datetime(2026, 5, 21, 12, 0, tzinfo=_dt.timezone.utc),
            endTime=_dt.datetime(2026, 5, 22, 12, 0, tzinfo=_dt.timezone.utc),
        )
    status, body = _get(host, port, f"/api/night/traceback/{bodyKey.replace(':', '%3A')}")
    assert status == 200, body
    assert body["pod"] == podName
    assert body["expId"] == 2026052100012
    assert body["excClass"] == "RuntimeError"
    assert body["contextSource"] == "dataId-block"
    # We expect 5 lines: the 3 pickup-onwards lines (incl. isr started)
    # plus the 3 traceback lines, minus the post-block "next pickup".
    rawTexts = [ln["raw"] for ln in body["lines"]]
    assert any("Running pipeline for 2026052100012" in r for r in rawTexts)
    assert any("isr started" in r for r in rawTexts)
    assert any("Traceback" in r for r in rawTexts)
    assert any("RuntimeError: bang" in r for r in rawTexts)
    # Pre-block and post-block lines (carryover advances on the next
    # pickup, so doing-the-next-thing belongs to 2026052100013) are
    # excluded.
    assert not any("warming up" in r for r in rawTexts)
    assert not any("doing the next thing" in r for r in rawTexts)
    assert not any("2026052100013" in r for r in rawTexts)


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
