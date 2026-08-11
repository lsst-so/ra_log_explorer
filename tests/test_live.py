"""Tests for the live night cache: slicing in `fetch`, polling in `live`."""

from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from ra_log_explorer import exposureTimes, fetch, live, sites
from ra_log_explorer.config import (
    FetchSpec,
    currentDayObs,
    dayObsEndUtc,
    dayObsStartUtc,
    windowCachePath,
)

UTC = dt.timezone.utc


def _lokiLine(ts: dt.datetime, msg: str = "hello") -> bytes:
    """One Loki JSONL line stamped at ``ts`` (nanosecond-style, +00:00)."""
    stamp = ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "000+00:00"
    return (
        json.dumps({"labels": {"detected_level": "info"}, "line": msg, "timestamp": stamp}) + "\n"
    ).encode()


def _eventLine(ts: dt.datetime, name: str, body: str = "reason=Started kind=Pod count=2") -> bytes:
    """One k8s/events JSONL line, as the namespace-wide query returns it.

    ``name`` is the involved object — a pod for most events, but the
    stream also carries ReplicaSet / Job / Deployment events, which is
    exactly the breadth the poller keeps.
    """
    stamp = ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "000+00:00"
    return (json.dumps({"labels": {"name": name}, "line": body, "timestamp": stamp}) + "\n").encode()


def _t(minute: int, second: int = 0) -> dt.datetime:
    """A timestamp inside the 20260711 night (12:xx UTC)."""
    return dt.datetime(2026, 7, 11, 12, minute, second, tzinfo=UTC)


# ----- currentDayObs --------------------------------------------------------


def test_currentDayObs_afternoon_is_same_date() -> None:
    assert currentDayObs(dt.datetime(2026, 7, 11, 13, 0, tzinfo=UTC)) == 20260711


def test_currentDayObs_morning_is_previous_date() -> None:
    assert currentDayObs(dt.datetime(2026, 7, 12, 11, 59, tzinfo=UTC)) == 20260711


def test_currentDayObs_rolls_at_noon() -> None:
    assert currentDayObs(dt.datetime(2026, 7, 12, 12, 0, tzinfo=UTC)) == 20260712


def test_currentDayObs_inverts_dayObsStartUtc() -> None:
    assert currentDayObs(dayObsStartUtc(20260711)) == 20260711


# ----- firstOffsetAtOrAfter -------------------------------------------------


def _writeLines(path: Path, times: list[dt.datetime]) -> list[int]:
    """Write one line per timestamp; return each line's start offset."""
    offsets = []
    pos = 0
    with open(path, "wb") as fh:
        for ts in times:
            offsets.append(pos)
            raw = _lokiLine(ts)
            fh.write(raw)
            pos += len(raw)
    offsets.append(pos)  # EOF offset, for convenience
    return offsets


def test_firstOffsetAtOrAfter_exact_and_between(tmp_path: Path) -> None:
    f = tmp_path / "pod.jsonl"
    times = [_t(0), _t(1), _t(2), _t(3)]
    offsets = _writeLines(f, times)
    limit = offsets[-1]
    with open(f, "rb") as fh:
        # Exactly on a line's timestamp -> that line.
        assert fetch.firstOffsetAtOrAfter(fh, _t(1), limit) == offsets[1]
        # Between two lines -> the later one.
        assert fetch.firstOffsetAtOrAfter(fh, _t(1, 30), limit) == offsets[2]
        # Before everything -> offset 0; after everything -> limit.
        assert fetch.firstOffsetAtOrAfter(fh, _t(0) - dt.timedelta(hours=1), limit) == 0
        assert fetch.firstOffsetAtOrAfter(fh, _t(59), limit) == limit


def test_firstOffsetAtOrAfter_respects_limit(tmp_path: Path) -> None:
    """Bytes past the durable limit — e.g. a concurrent half-append — are
    invisible, even when they'd otherwise match."""
    f = tmp_path / "pod.jsonl"
    offsets = _writeLines(f, [_t(0), _t(1)])
    limit = offsets[-1]
    with open(f, "ab") as fh:
        fh.write(_lokiLine(_t(2))[:17])  # torn write beyond the limit
    with open(f, "rb") as fh:
        assert fetch.firstOffsetAtOrAfter(fh, _t(2), limit) == limit


# ----- slicing --------------------------------------------------------------


def _makeNightDir(
    root: Path,
    dayObs: int = 20260711,
    watermark: dt.datetime | None = None,
    podTimes: dict[str, list[dt.datetime]] | None = None,
    podEventTimes: dict[str, list[dt.datetime]] | None = None,
) -> Path:
    """Build a live night dir with a sidecar and per-pod files on disk."""
    fromIso = fetch._fmtLogcliTime(dayObsStartUtc(dayObs))
    toIso = fetch._fmtLogcliTime(dayObsEndUtc(dayObs))
    nightDir = windowCachePath("yagan", "rapid-analysis", fromIso, toIso)
    (nightDir / fetch.PODS_DIR_NAME).mkdir(parents=True)
    (nightDir / fetch.PODS_EVENTS_DIR_NAME).mkdir(parents=True)
    pods: dict[str, Any] = {}
    for pod, times in (podTimes or {}).items():
        f = nightDir / fetch.PODS_DIR_NAME / f"{pod}.jsonl"
        offsets = _writeLines(f, times)
        pods[pod] = {
            "watermarkIso": fetch._fmtLogcliTime(watermark or dayObsEndUtc(dayObs)),
            "bytes": offsets[-1],
            "lines": len(times),
            "eventBytes": 0,
            "eventLines": 0,
        }
    for pod, times in (podEventTimes or {}).items():
        f = nightDir / fetch.PODS_EVENTS_DIR_NAME / f"{pod}.jsonl"
        offsets = _writeLines(f, times)
        pods.setdefault(pod, {"watermarkIso": toIso, "bytes": 0, "lines": 0})
        pods[pod]["eventBytes"] = offsets[-1]
        pods[pod]["eventLines"] = len(times)
    sidecar = {
        "version": fetch.LIVE_SIDECAR_VERSION,
        "dayObs": dayObs,
        "fromIso": fromIso,
        "toIso": toIso,
        "watermarkIso": fetch._fmtLogcliTime(watermark) if watermark else toIso,
        "eventsWatermarkIso": fetch._fmtLogcliTime(watermark) if watermark else toIso,
        "finalised": watermark is None,
        "updatedAt": dt.datetime.now(UTC).isoformat(),
        "pods": pods,
        "eventPods": {},
        "errors": {},
        "incomplete_pods": {},
    }
    fetch.writeLiveSidecar(nightDir, sidecar)
    return nightDir


def _sliceSpec(fromT: dt.datetime, toT: dt.datetime) -> FetchSpec:
    return FetchSpec(
        lokiAddr="https://loki.example",
        username="u",
        cluster="yagan",
        namespace="rapid-analysis",
        fromIso=fetch._fmtLogcliTime(fromT),
        toIso=fetch._fmtLogcliTime(toT),
    )


def test_findNightDirCovering_honours_watermark(tmpCacheRoot: Path) -> None:
    nightDir = _makeNightDir(tmpCacheRoot, watermark=_t(30))
    found = fetch.findNightDirCovering("yagan", "rapid-analysis", _t(10), _t(20))
    assert found == nightDir
    # A window ending past the watermark is not covered yet.
    assert fetch.findNightDirCovering("yagan", "rapid-analysis", _t(10), _t(31)) is None
    # Wrong cluster: nothing.
    assert fetch.findNightDirCovering("manke", "rapid-analysis", _t(10), _t(20)) is None


def test_materializeNightSlice_produces_normal_cache(tmpCacheRoot: Path) -> None:
    podTimes = {
        "sfm-runner-1": [_t(0), _t(5), _t(10), _t(15), _t(25)],
        "head-node": [_t(4), _t(6)],
        "quiet-pod": [_t(29)],
    }
    nightDir = _makeNightDir(tmpCacheRoot, watermark=_t(30), podTimes=podTimes)
    spec = _sliceSpec(_t(5), _t(15))
    sliceDir, meta = fetch.materializeNightSlice(nightDir, spec)

    assert meta["cacheReuse"] == "night-slice"
    assert meta["fetchComplete"] is True
    assert meta["sliceSource"] == str(nightDir)
    # Half-open [5, 15): sfm gets 12:05 and 12:10 but not 12:15.
    sfm = (sliceDir / "pods" / "sfm-runner-1.jsonl").read_bytes()
    assert sfm == _lokiLine(_t(5)) + _lokiLine(_t(10))
    # head-node's 12:06 line is in; quiet-pod has nothing in range.
    assert (sliceDir / "pods" / "head-node.jsonl").read_bytes() == _lokiLine(_t(6))
    assert not (sliceDir / "pods" / "quiet-pod.jsonl").exists()
    assert meta["pod_lines"] == {"sfm-runner-1": 2, "head-node": 1}
    assert (sliceDir / "pods.txt").read_text().split() == ["head-node", "sfm-runner-1"]
    # The result is a first-class cache: a second identical request is an
    # exact hit served without touching the night dir (or the network).
    againDir, againMeta = fetch.fetchAll(spec)
    assert againDir == sliceDir
    assert againMeta["cacheReuse"] == "exact"


def test_fetchAll_serves_slice_without_network(tmpCacheRoot: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _makeNightDir(tmpCacheRoot, watermark=_t(30), podTimes={"sfm-runner-1": [_t(7)]})

    def boom(*args: Any, **kwargs: Any) -> bytes:
        raise AssertionError("fetchAll should not have run logcli")

    monkeypatch.setattr(fetch, "_run_logcli", boom)
    cacheDir, meta = fetch.fetchAll(_sliceSpec(_t(5), _t(10)))
    assert meta["cacheReuse"] == "night-slice"
    assert (cacheDir / "pods" / "sfm-runner-1.jsonl").read_bytes() == _lokiLine(_t(7))


def test_slice_inherits_night_fallshort_maps(tmpCacheRoot: Path) -> None:
    nightDir = _makeNightDir(tmpCacheRoot, watermark=_t(30), podTimes={"sfm-runner-1": [_t(7)]})
    sidecar = fetch.readLiveSidecar(nightDir)
    assert sidecar is not None
    sidecar["errors"] = {"dead-pod": "logcli timed out"}
    fetch.writeLiveSidecar(nightDir, sidecar)
    _, meta = fetch.materializeNightSlice(nightDir, _sliceSpec(_t(5), _t(10)))
    assert meta["fetchComplete"] is False
    assert "dead-pod" in meta["errors"]


def test_materializeNightSlice_honours_podRegex(tmpCacheRoot: Path) -> None:
    """A night-mode (AOS-filtered) request slices only matching pods and
    lands in the nested pods= dir a real filtered fetch would use."""
    podTimes = {
        "s-lsstcam-run-aos-worker-1": [_t(6)],
        "s-lsstcam-run-sfm-runner-1": [_t(7)],
    }
    nightDir = _makeNightDir(tmpCacheRoot, watermark=_t(30), podTimes=podTimes)
    sidecar = fetch.readLiveSidecar(nightDir)
    assert sidecar is not None
    sidecar["errors"] = {"s-lsstcam-run-sfm-runner-9": "logcli timed out"}
    fetch.writeLiveSidecar(nightDir, sidecar)

    spec = replace(_sliceSpec(_t(5), _t(10)), podRegex=".*aos.*")
    sliceDir, meta = fetch.materializeNightSlice(nightDir, spec)
    assert sliceDir.name.startswith("pods=")
    assert meta["pod_lines"] == {"s-lsstcam-run-aos-worker-1": 1}
    assert not (sliceDir / "pods" / "s-lsstcam-run-sfm-runner-1.jsonl").exists()
    # The SFM pod's error can't affect an AOS-only slice; it must not
    # spuriously flag this window incomplete.
    assert meta["errors"] == {}
    assert meta["fetchComplete"] is True


def test_fetchAll_serves_inprogress_night_clamped_to_watermark(
    tmpCacheRoot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Night mode on the current night: the request is the night's own
    (future-ending) window, served as "the night so far" with no Loki
    round trip."""
    podTimes = {
        "s-lsstcam-run-aos-worker-1": [_t(6), _t(40)],
        "s-lsstcam-run-sfm-runner-1": [_t(7)],
    }
    _makeNightDir(tmpCacheRoot, watermark=_t(30), podTimes=podTimes)

    def boom(*args: Any, **kwargs: Any) -> bytes:
        raise AssertionError("in-progress night should be sliced, not fetched")

    monkeypatch.setattr(fetch, "_run_logcli", boom)
    # The night's own window, exactly as the night-mode fetch path
    # builds it. The clamping logic keys off "request == the night's own
    # window, watermark short of its end" and never consults the clock,
    # so this exercises the in-progress-night path as production runs it.
    spec = replace(
        _sliceSpec(dayObsStartUtc(20260711), dayObsEndUtc(20260711)),
        podRegex=".*aos.*",
    )
    cacheDir, meta = fetch.fetchAll(spec)
    assert meta["cacheReuse"] == "night-slice"
    # Clamped to the watermark: the 12:40 line is not there yet.
    assert meta["spec"]["toIso"] == fetch._fmtLogcliTime(_t(30))
    assert meta["pod_lines"] == {"s-lsstcam-run-aos-worker-1": 1}
    # Re-opening against an unchanged watermark reuses the same slice.
    again, meta2 = fetch.fetchAll(spec)
    assert again == cacheDir
    assert meta2["cacheReuse"] == "exact"


def test_fetchAll_does_not_clamp_arbitrary_future_windows(
    tmpCacheRoot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user window that merely *extends past* the watermark is not
    silently clamped — it falls through to a real fetch."""
    _makeNightDir(tmpCacheRoot, watermark=_t(30), podTimes={"s-lsstcam-run-aos-worker-1": [_t(6)]})
    calls: list[str] = []

    def fakeRunLogcli(_spec: FetchSpec, extraArgs: list[str], **_kw: Any) -> bytes:
        calls.append(extraArgs[0])
        return b""

    monkeypatch.setattr(fetch, "_run_logcli", fakeRunLogcli)
    spec = _sliceSpec(_t(10), _t(45))  # ends past the 12:30 watermark
    _, meta = fetch.fetchAll(spec)
    assert meta["cacheReuse"] == "none"
    assert calls  # it really went to Loki


def test_findSupersetCache_skips_live_dirs(tmpCacheRoot: Path) -> None:
    """A live-built night dir must never be handed over as a superset —
    that path parses whole directories, and a night is minutes of parse."""
    nightDir = _makeNightDir(tmpCacheRoot, watermark=None, podTimes={"sfm-runner-1": [_t(7)]})
    # Give it the _meta.json a finalised night carries, so only the
    # sidecar check can exclude it.
    (nightDir / fetch.META_NAME).write_text(
        json.dumps(
            {
                "spec": {
                    "lokiAddr": "x",
                    "username": "u",
                    "cluster": "yagan",
                    "namespace": "rapid-analysis",
                    "fromIso": fetch._fmtLogcliTime(dayObsStartUtc(20260711)),
                    "toIso": fetch._fmtLogcliTime(dayObsEndUtc(20260711)),
                    "workers": 8,
                    "podRegex": None,
                },
                "fetchSchemaVersion": fetch.CACHE_SCHEMA_VERSION,
            }
        )
    )
    found = fetch.findSupersetCache(
        "yagan",
        "rapid-analysis",
        fetch._fmtLogcliTime(_t(5)),
        fetch._fmtLogcliTime(_t(10)),
    )
    assert found is None


def test_slice_carries_pod_lifecycle_events(tmpCacheRoot: Path) -> None:
    """The k8s/events stream is sliced by its own recorded byte count, and
    only for pods that emitted app logs in the window — matching a real
    fetch, which enumerates pods from the app-log series listing."""
    nightDir = _makeNightDir(
        tmpCacheRoot,
        watermark=_t(30),
        podTimes={"sfm-runner-1": [_t(6)], "quiet-pod": [_t(29)]},
        podEventTimes={
            "sfm-runner-1": [_t(4), _t(7), _t(20)],
            "quiet-pod": [_t(7)],
            "some-replicaset-abc12": [_t(7)],
        },
    )
    sliceDir, meta = fetch.materializeNightSlice(nightDir, _sliceSpec(_t(5), _t(10)))
    assert meta["pod_event_lines"] == {"sfm-runner-1": 1}
    events = (sliceDir / fetch.PODS_EVENTS_DIR_NAME / "sfm-runner-1.jsonl").read_bytes()
    assert events == _lokiLine(_t(7))  # [5, 10): not 12:04, not 12:20
    # No app logs in the window => no lifecycle file either, for a pod or
    # for an object that never had app logs at all.
    assert not (sliceDir / fetch.PODS_EVENTS_DIR_NAME / "quiet-pod.jsonl").exists()
    assert not (sliceDir / fetch.PODS_EVENTS_DIR_NAME / "some-replicaset-abc12.jsonl").exists()


def test_sliceFileByTime_refuses_to_slice_a_file_into_itself(tmp_path: Path) -> None:
    """The destination is opened "wb", which truncates — so src == dst
    would empty the file before a byte was read."""
    f = tmp_path / "pod.jsonl"
    _writeLines(f, [_t(1), _t(2)])
    before = f.read_bytes()
    with pytest.raises(fetch.FetchError, match="into itself"):
        fetch._sliceFileByTime(f, f, _t(0), _t(59), len(before))
    assert f.read_bytes() == before


def test_fetchAll_serves_the_nights_own_window_without_self_slicing(
    tmpCacheRoot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between the watermark reaching night end and finalisation writing
    _meta.json — where --live-day-obs parks permanently — a request for
    the night's own all-pods window used to slice the night dir into
    itself, deleting every pod file. It gets handed the night dir."""
    nightDir = _makeNightDir(
        tmpCacheRoot, watermark=dayObsEndUtc(20260711), podTimes={"pod-a": [_t(3), _t(7)]}
    )
    (nightDir / fetch.META_NAME).unlink(missing_ok=True)
    podFile = nightDir / fetch.PODS_DIR_NAME / "pod-a.jsonl"
    before = podFile.read_bytes()

    def boom(*a: Any, **k: Any) -> bytes:
        raise AssertionError("no Loki round trip expected")

    monkeypatch.setattr(fetch, "_run_logcli", boom)
    spec = _sliceSpec(dayObsStartUtc(20260711), dayObsEndUtc(20260711))
    cacheDir, meta = fetch.fetchAll(spec)
    assert cacheDir == nightDir
    assert podFile.read_bytes() == before
    assert meta["pod_lines"] == {"pod-a": 2}
    assert meta["liveBuilt"] is True


def test_fetchAll_refuses_to_fetch_over_a_live_night_dir(
    tmpCacheRoot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh fetch opens every pods/<pod>.jsonl "wb". Against a dir the
    poller is appending to that is silent corruption, so it is refused."""
    _makeNightDir(tmpCacheRoot, watermark=_t(30), podTimes={"pod-a": [_t(3)]})
    monkeypatch.setattr(fetch, "_run_logcli", lambda *a, **k: b"")
    spec = _sliceSpec(dayObsStartUtc(20260711), dayObsEndUtc(20260711))
    with pytest.raises(fetch.FetchError, match="live night directory"):
        fetch.fetchAll(spec, forceRefresh=True)


def test_fetchAll_prefers_an_exact_cache_over_slicing(
    tmpCacheRoot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A window already fetched in full is served as-is: the slice path
    must not overwrite it with a copy out of the night dir."""
    _makeNightDir(tmpCacheRoot, watermark=_t(30), podTimes={"pod-a": [_t(6), _t(7)]})
    spec = _sliceSpec(_t(5), _t(10))
    exactDir, _ = fetch.materializeNightSlice(  # any real window will do
        fetch.findNightDirCovering("yagan", "rapid-analysis", _t(5), _t(10)), spec  # type: ignore[arg-type]
    )
    # Mark it so we can tell it apart from a fresh slice.
    marker = exactDir / fetch.PODS_DIR_NAME / "hand-fetched.jsonl"
    marker.write_bytes(_lokiLine(_t(6)))

    def boom(*a: Any, **k: Any) -> bytes:
        raise AssertionError("no Loki round trip expected")

    monkeypatch.setattr(fetch, "_run_logcli", boom)
    again, meta = fetch.fetchAll(spec)
    assert again == exactDir
    assert meta["cacheReuse"] == "exact"
    assert marker.exists()  # untouched: no re-slice ran


def test_night_slice_prunes_files_a_failed_attempt_left_behind(
    tmpCacheRoot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """summarizeAll enumerates pods/ rather than pods.txt, so a pod file
    stranded by an earlier attempt on this window would be parsed as part
    of it. A slice that rewrites the window clears them."""
    nightDir = _makeNightDir(tmpCacheRoot, watermark=_t(30), podTimes={"pod-a": [_t(6)]})
    spec = _sliceSpec(_t(5), _t(10))
    sliceDir = windowCachePath("yagan", "rapid-analysis", spec.fromIso, spec.toIso)
    (sliceDir / fetch.PODS_DIR_NAME).mkdir(parents=True)
    (sliceDir / fetch.PODS_DIR_NAME / "ghost-pod.jsonl").write_bytes(_lokiLine(_t(6)))

    _, meta = fetch.materializeNightSlice(nightDir, spec)
    assert not (sliceDir / fetch.PODS_DIR_NAME / "ghost-pod.jsonl").exists()
    assert meta["pod_lines"] == {"pod-a": 1}


def test_materializeNightSlice_leaves_partial_flag_on_failure(
    tmpCacheRoot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-copied window must never look like a cache hit."""
    nightDir = _makeNightDir(tmpCacheRoot, watermark=_t(30), podTimes={"pod-a": [_t(6)]})
    monkeypatch.setattr(
        fetch, "_sliceFileByTime", lambda *a, **k: (_ for _ in ()).throw(OSError("disk gone"))
    )
    spec = _sliceSpec(_t(5), _t(10))
    with pytest.raises(OSError):
        fetch.materializeNightSlice(nightDir, spec)
    sliceDir = windowCachePath("yagan", "rapid-analysis", spec.fromIso, spec.toIso)
    assert (sliceDir / fetch.PARTIAL_FLAG).exists()


def test_concurrent_fetchAll_for_one_window_is_serialised(
    tmpCacheRoot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two people clicking the same ready exposure at once used to have
    both threads writing the same pods/<pod>.jsonl. The window lock makes
    the loser wait and then take the winner's result as a cache hit."""
    import threading

    _makeNightDir(tmpCacheRoot, watermark=_t(30), podTimes={"pod-a": [_t(6), _t(7), _t(8)]})
    spec = _sliceSpec(_t(5), _t(10))
    inFlight = {"n": 0, "maxConcurrent": 0}
    guard = threading.Lock()
    realSlice = fetch._sliceNightInto

    def observedSlice(*a: Any, **k: Any) -> Any:
        with guard:
            inFlight["n"] += 1
            inFlight["maxConcurrent"] = max(inFlight["maxConcurrent"], inFlight["n"])
        try:
            time.sleep(0.05)
            return realSlice(*a, **k)
        finally:
            with guard:
                inFlight["n"] -= 1

    monkeypatch.setattr(fetch, "_sliceNightInto", observedSlice)
    monkeypatch.setattr(fetch, "_run_logcli", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no net")))

    results: list[tuple[Path, dict]] = []
    errors: list[Exception] = []

    def run() -> None:
        try:
            results.append(fetch.fetchAll(spec))
        except Exception as e:  # noqa: BLE001 — collected, then asserted on below
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not errors
    assert inFlight["maxConcurrent"] == 1
    assert len({str(d) for d, _ in results}) == 1
    # Exactly one thread did the copying; the others exact-hit its result.
    assert sorted(m["cacheReuse"] for _, m in results) == ["exact", "exact", "exact", "night-slice"]
    sliceDir = results[0][0]
    assert (sliceDir / fetch.PODS_DIR_NAME / "pod-a.jsonl").read_bytes() == b"".join(
        _lokiLine(t) for t in (_t(6), _t(7), _t(8))
    )


def test_clamped_slice_and_direct_window_fetch_are_serialised(
    tmpCacheRoot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fetchAll's lock is on the *requested* window path, and the clamped
    branch writes somewhere else: night mode on the current dayObs asks
    for the night's own window but slices into [nightStart, watermark].
    A direct request for exactly that window locks it as its own
    requested path — so the two used to be able to interleave bytes in
    the same pod files. _tryNightSlice now locks the slice's real
    destination too."""
    import threading

    _makeNightDir(tmpCacheRoot, watermark=_t(30), podTimes={"pod-a": [_t(6), _t(7), _t(8)]})
    nightOwn = _sliceSpec(dayObsStartUtc(20260711), dayObsEndUtc(20260711))  # clamps to the watermark
    direct = _sliceSpec(dayObsStartUtc(20260711), _t(30))  # the clamped target, asked for directly
    inFlight = {"n": 0, "maxConcurrent": 0}
    guard = threading.Lock()
    realSlice = fetch._sliceNightInto

    def observedSlice(*a: Any, **k: Any) -> Any:
        with guard:
            inFlight["n"] += 1
            inFlight["maxConcurrent"] = max(inFlight["maxConcurrent"], inFlight["n"])
        try:
            time.sleep(0.05)
            return realSlice(*a, **k)
        finally:
            with guard:
                inFlight["n"] -= 1

    monkeypatch.setattr(fetch, "_sliceNightInto", observedSlice)
    monkeypatch.setattr(fetch, "_run_logcli", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no net")))

    results: list[tuple[Path, dict]] = []
    errors: list[Exception] = []

    def run(spec: FetchSpec) -> None:
        try:
            results.append(fetch.fetchAll(spec))
        except Exception as e:  # noqa: BLE001 — collected, then asserted on below
            errors.append(e)

    threads = [threading.Thread(target=run, args=(s,)) for s in (nightOwn, direct, nightOwn, direct)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not errors
    assert inFlight["maxConcurrent"] == 1
    # Both request shapes land on the one materialized window, whole.
    assert len({str(d) for d, _ in results}) == 1
    sliceDir = results[0][0]
    assert (sliceDir / fetch.PODS_DIR_NAME / "pod-a.jsonl").read_bytes() == b"".join(
        _lokiLine(t) for t in (_t(6), _t(7), _t(8))
    )


# ----- LiveNightManager -----------------------------------------------------


def _site() -> sites.Site:
    return sites.Site(
        name="summit",
        cluster="yagan",
        namespace="rapid-analysis",
        lokiAddr="https://loki.example",
        consdbUrl="https://consdb.example/query",
        consdbTokenFile=None,
    )


@pytest.fixture
def manager(tmpCacheRoot: Path, monkeypatch: pytest.MonkeyPatch) -> live.LiveNightManager:
    """A manager whose network edges are stubbed to safe defaults.

    Individual tests override the stubs they care about. lag is zero so
    tick targets are exactly the ``now`` passed in.
    """
    monkeypatch.setattr(live, "listPods", lambda spec: [])
    monkeypatch.setattr(live, "fetchEventsWindowInto", lambda spec, a, b, fh: (0, True, ""))
    monkeypatch.setattr(
        live.exposureTimes, "queryExposureRecordsForDayObs", lambda dayObs, token, *, consdbUrl: []
    )
    return live.LiveNightManager(site=_site(), username="u", workers=2, pollS=60.0, lagS=0.0)


def _podFetchStub(times: list[dt.datetime], calls: list[Any] | None = None) -> Any:
    """A fetchPodWindowInto stand-in emitting ``times`` clipped to the window.

    Note the stub applies half-open ``[fromT, toT)`` semantics itself, so
    it cannot on its own reveal a boundary bug in the caller — hence the
    optional ``calls`` log, which lets a test assert that consecutive
    windows tile rather than trusting the stub to normalise them.
    """

    def stub(
        spec: FetchSpec, pod: str, fromT: dt.datetime, toT: dt.datetime, fh: BinaryIO
    ) -> tuple[int, bool, str]:
        if calls is not None:
            calls.append((pod, fromT, toT))
        n = 0
        for ts in times:
            if fromT <= ts < toT:
                fh.write(_lokiLine(ts))
                n += 1
        return n, True, ""

    return stub


def test_tick_appends_and_advances_watermark(
    manager: live.LiveNightManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(live, "listPods", lambda spec: ["pod-a"])
    # 12:10 sits exactly on the first tick's boundary: it must land in the
    # second window, exactly once. A closed upper bound would duplicate
    # it, and a fetch starting one tick-width late would drop it.
    monkeypatch.setattr(live, "fetchPodWindowInto", _podFetchStub([_t(3), _t(7), _t(10), _t(40)], calls))

    manager.tick(now=_t(10))
    snap = manager.snapshot()
    assert snap["dayObs"] == 20260711
    assert fetch._parseIso(snap["watermark"]) == _t(10)
    assert snap["totalLines"] == 2  # 12:10 and 12:40 are beyond the first target

    manager.tick(now=_t(50))
    snap = manager.snapshot()
    assert fetch._parseIso(snap["watermark"]) == _t(50)
    assert snap["totalLines"] == 4

    # The windows the poller *asked* for tile exactly: each starts where
    # the previous ended, with no overlap and no gap. Asserting on the
    # requests rather than the bytes is what makes this an off-by-one
    # test — the stub would silently absorb a half-open violation.
    assert [(c[1], c[2]) for c in calls] == [
        (dayObsStartUtc(20260711), _t(10)),
        (_t(10), _t(50)),
    ]
    nightDir = fetch.findNightDirCovering("yagan", "rapid-analysis", _t(1), _t(45))
    assert nightDir is not None
    podFile = nightDir / fetch.PODS_DIR_NAME / "pod-a.jsonl"
    # Ticks tile: four lines, once each, in order.
    assert podFile.read_bytes() == b"".join(_lokiLine(t) for t in (_t(3), _t(7), _t(10), _t(40)))


def test_failed_pod_holds_watermark_then_retries(
    manager: live.LiveNightManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, dt.datetime, dt.datetime]] = []

    def failingFetch(
        spec: FetchSpec, pod: str, fromT: dt.datetime, toT: dt.datetime, fh: BinaryIO
    ) -> tuple[int, bool, str]:
        calls.append((pod, fromT, toT))
        if pod == "pod-bad" and len([c for c in calls if c[0] == "pod-bad"]) == 1:
            raise fetch.FetchError("transient 502")
        fh.write(_lokiLine(fromT))
        return 1, True, ""

    monkeypatch.setattr(live, "listPods", lambda spec: ["pod-good", "pod-bad"])
    monkeypatch.setattr(live, "fetchPodWindowInto", failingFetch)

    manager.tick(now=_t(10))
    snap = manager.snapshot()
    assert "pod-bad" in snap["errors"]
    # Global watermark pinned at the failed pod's coverage (night start).
    assert fetch._parseIso(snap["watermark"]) == dayObsStartUtc(20260711)

    manager.tick(now=_t(20))
    snap = manager.snapshot()
    assert snap["errors"] == {}
    assert fetch._parseIso(snap["watermark"]) == _t(20)
    # The retry re-fetched pod-bad's whole missed span from night start.
    badCalls = [c for c in calls if c[0] == "pod-bad"]
    assert badCalls[1][1] == dayObsStartUtc(20260711)
    assert badCalls[1][2] == _t(20)


def test_pod_absent_from_listing_advances_free(
    manager: live.LiveNightManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(live, "listPods", lambda spec: ["pod-a"])
    monkeypatch.setattr(live, "fetchPodWindowInto", _podFetchStub([_t(3)]))
    manager.tick(now=_t(10))

    # pod-a stops emitting; the watermark must not stick at its death.
    monkeypatch.setattr(live, "listPods", lambda spec: [])
    manager.tick(now=_t(30))
    snap = manager.snapshot()
    assert fetch._parseIso(snap["watermark"]) == _t(30)


def _consdbStub(records: list[dict[str, Any]]) -> Any:
    return lambda dayObs, token, *, consdbUrl: records


def test_readiness_follows_watermark(manager: live.LiveNightManager, monkeypatch: pytest.MonkeyPatch) -> None:
    # obs_end is TAI = UTC + 37 s. Shutter closes 12:10:00 and 12:29:00 UTC.
    records = [
        {"exposure_id": 2026071100001, "obs_end": "2026-07-11T12:10:37", "instrument": "lsstcam"},
        {"exposure_id": 2026071100002, "obs_end": "2026-07-11T12:29:37", "instrument": "lsstcam"},
    ]
    monkeypatch.setattr(live.exposureTimes, "queryExposureRecordsForDayObs", _consdbStub(records))
    manager.tick(now=_t(30))
    rows = {r["dataId"]: r for r in manager.snapshot()["exposures"]}
    # windowAfter (300 s) past 12:10 is 12:15 <= watermark 12:30: ready.
    assert rows[2026071100001]["ready"] is True
    # 12:29 + 300 s = 12:34 > 12:30: not yet.
    assert rows[2026071100002]["ready"] is False
    # Newest first.
    assert [r["dataId"] for r in manager.snapshot()["exposures"]] == [2026071100002, 2026071100001]
    # Records were persisted to the per-site exposure-time cache, so the
    # UI's /api/exposure-time lookup resolves instantly.
    cached = exposureTimes.lookupCachedRecord(2026071100001, siteName="summit")
    assert cached is not None and cached["obs_end"] == "2026-07-11T12:10:37"


def test_restart_recovery_truncates_unrecorded_bytes(
    tmpCacheRoot: Path, manager: live.LiveNightManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(live, "listPods", lambda spec: ["pod-a"])
    monkeypatch.setattr(live, "fetchPodWindowInto", _podFetchStub([_t(3)]))
    manager.tick(now=_t(10))
    nightDir = fetch.findNightDirCovering("yagan", "rapid-analysis", _t(1), _t(9))
    assert nightDir is not None
    podFile = nightDir / fetch.PODS_DIR_NAME / "pod-a.jsonl"
    recorded = podFile.stat().st_size
    # Simulate a crash mid-append: bytes on disk the sidecar never heard of.
    with open(podFile, "ab") as fh:
        fh.write(b'{"torn": ')

    fresh = live.LiveNightManager(site=_site(), username="u", workers=2, pollS=60.0, lagS=0.0)
    monkeypatch.setattr(live, "listPods", lambda spec: [])
    fresh.tick(now=_t(20))
    assert podFile.stat().st_size == recorded


def test_rollover_finalises_night(manager: live.LiveNightManager, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live, "listPods", lambda spec: ["pod-a"])
    monkeypatch.setattr(live, "fetchPodWindowInto", _podFetchStub([_t(3)]))
    # Verification pass agrees with what was appended.
    monkeypatch.setattr(live, "countPodWindow", lambda spec, pod, a, b: 1)
    manager.tick(now=_t(10))

    monkeypatch.setattr(live, "listPods", lambda spec: [])
    manager.tick(now=dt.datetime(2026, 7, 12, 12, 1, tzinfo=UTC))  # next dayObs

    oldNight = windowCachePath(
        "yagan",
        "rapid-analysis",
        fetch._fmtLogcliTime(dayObsStartUtc(20260711)),
        fetch._fmtLogcliTime(dayObsEndUtc(20260711)),
    )
    meta = json.loads((oldNight / fetch.META_NAME).read_text())
    assert meta["fetchComplete"] is True
    assert meta["liveBuilt"] is True
    assert meta["pod_lines"] == {"pod-a": 1}
    sidecar = fetch.readLiveSidecar(oldNight)
    assert sidecar is not None and sidecar["finalised"] is True
    # The manager has moved on to the new night.
    assert manager.snapshot()["dayObs"] == 20260712


def test_rollover_verification_refetches_mismatched_pod(
    manager: live.LiveNightManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(live, "listPods", lambda spec: ["pod-a"])
    monkeypatch.setattr(live, "fetchPodWindowInto", _podFetchStub([_t(3)]))
    manager.tick(now=_t(10))

    # The oracle says far more lines existed than were appended (e.g.
    # lines ingested later than the lag allowed for) — well past the
    # dedup-slack tolerance, so the pod must be refetched.
    refetchTimes = [_t(3) + dt.timedelta(seconds=i) for i in range(150)]
    monkeypatch.setattr(live, "fetchPodWindowInto", _podFetchStub(refetchTimes))
    monkeypatch.setattr(live, "countPodWindow", lambda spec, pod, a, b: 150)
    monkeypatch.setattr(live, "listPods", lambda spec: [])
    manager.tick(now=dt.datetime(2026, 7, 12, 12, 1, tzinfo=UTC))

    oldNight = windowCachePath(
        "yagan",
        "rapid-analysis",
        fetch._fmtLogcliTime(dayObsStartUtc(20260711)),
        fetch._fmtLogcliTime(dayObsEndUtc(20260711)),
    )
    meta = json.loads((oldNight / fetch.META_NAME).read_text())
    assert meta["pod_lines"] == {"pod-a": 150}
    assert meta["fetchComplete"] is True
    podFile = oldNight / fetch.PODS_DIR_NAME / "pod-a.jsonl"
    assert podFile.stat().st_size == sum(len(_lokiLine(ts)) for ts in refetchTimes)


def test_rollover_verification_tolerates_oracle_dedup_slack(
    manager: live.LiveNightManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """count_over_time counts duplicate storage-chunk entries the log
    path dedups (~tens of lines/pod/night, measured). A shortfall inside
    the tolerance must NOT trigger a whole-pod refetch — with strict
    equality, finalisation would re-pull nearly the entire night daily."""
    monkeypatch.setattr(live, "listPods", lambda spec: ["pod-a"])
    monkeypatch.setattr(live, "fetchPodWindowInto", _podFetchStub([_t(3)]))
    manager.tick(now=_t(10))

    def refetchForbidden(*a: Any, **k: Any) -> tuple[int, bool, str]:
        raise AssertionError("within-tolerance shortfall must not refetch")

    monkeypatch.setattr(live, "fetchPodWindowInto", refetchForbidden)
    # 1 line on disk, oracle claims 25: shortfall 24 <= tolerance floor.
    monkeypatch.setattr(live, "countPodWindow", lambda spec, pod, a, b: 25)
    monkeypatch.setattr(live, "listPods", lambda spec: [])
    manager.tick(now=dt.datetime(2026, 7, 12, 12, 1, tzinfo=UTC))

    oldNight = windowCachePath(
        "yagan",
        "rapid-analysis",
        fetch._fmtLogcliTime(dayObsStartUtc(20260711)),
        fetch._fmtLogcliTime(dayObsEndUtc(20260711)),
    )
    meta = json.loads((oldNight / fetch.META_NAME).read_text())
    assert meta["pod_lines"] == {"pod-a": 1}
    assert meta["pod_expected"] == {"pod-a": 25}  # recorded for the audit trail
    assert meta["fetchComplete"] is True


def test_fixed_dayobs_pins_the_night(tmpCacheRoot: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--live-day-obs: a staged historical night plays 'tonight' — the
    poller opens that night and the clock-driven rollover never fires."""
    monkeypatch.setattr(live, "listPods", lambda spec: [])
    monkeypatch.setattr(live, "fetchEventsWindowInto", lambda spec, a, b, fh: (0, True, ""))
    monkeypatch.setattr(
        live.exposureTimes, "queryExposureRecordsForDayObs", lambda dayObs, token, *, consdbUrl: []
    )
    mgr = live.LiveNightManager(
        site=_site(), username="u", workers=2, pollS=60.0, lagS=0.0, fixedDayObs=20260711
    )
    mgr.tick(now=dt.datetime(2026, 8, 10, 15, 0, tzinfo=UTC))  # "today" is a month later
    snap = mgr.snapshot()
    assert snap["dayObs"] == 20260711
    assert snap["nightStart"] == fetch._fmtLogcliTime(dayObsStartUtc(20260711))
    # A second tick on yet another day still doesn't roll the night over.
    mgr.tick(now=dt.datetime(2026, 8, 11, 15, 0, tzinfo=UTC))
    assert mgr.snapshot()["dayObs"] == 20260711


def test_snapshot_disabled_shape_from_server() -> None:
    """The /api/live contract when live mode is off is a static shape."""
    from ra_log_explorer.jobs import JobManager
    from ra_log_explorer.server import ServerContext

    ctx = ServerContext(jobs=JobManager())
    assert ctx.live is None


# ----- k8s/events demux -----------------------------------------------------


def _eventsStub(lines: list[bytes]) -> Any:
    """A fetchEventsWindowInto stand-in emitting ``lines`` verbatim."""

    def stub(spec: FetchSpec, fromT: dt.datetime, toT: dt.datetime, fh: BinaryIO) -> tuple[int, bool, str]:
        for raw in lines:
            fh.write(raw)
        return len(lines), True, ""

    return stub


def test_events_only_name_does_not_hold_the_watermark_back(
    manager: live.LiveNightManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The namespace-wide events query returns names that never emit app
    logs — rescheduled pods, and ReplicaSet/Job objects. They are kept,
    but they make no claim about app-log coverage, so one appearing must
    not drag the global watermark back to night start (which would strip
    every exposure of its "ready" state and stop every slice)."""
    monkeypatch.setattr(live, "listPods", lambda spec: ["pod-a"])
    monkeypatch.setattr(live, "fetchPodWindowInto", _podFetchStub([_t(3)]))
    manager.tick(now=_t(10))
    assert fetch._parseIso(manager.snapshot()["watermark"]) == _t(10)

    monkeypatch.setattr(
        live,
        "fetchEventsWindowInto",
        _eventsStub([_eventLine(_t(12), "some-replicaset-abc12"), _eventLine(_t(13), "pod-a")]),
    )
    manager.tick(now=_t(20))
    assert fetch._parseIso(manager.snapshot()["watermark"]) == _t(20)

    nightDir = fetch.findNightDirCovering("yagan", "rapid-analysis", _t(1), _t(20))
    assert nightDir is not None
    # Kept on disk, both of them — breadth is deliberate.
    assert (nightDir / fetch.PODS_EVENTS_DIR_NAME / "some-replicaset-abc12.jsonl").exists()
    assert (nightDir / fetch.PODS_EVENTS_DIR_NAME / "pod-a.jsonl").exists()
    sidecar = fetch.readLiveSidecar(nightDir)
    assert sidecar is not None
    # ...but filed apart from the pods, and with no watermark of its own.
    assert "some-replicaset-abc12" not in sidecar["pods"]
    assert sidecar["eventPods"]["some-replicaset-abc12"]["eventLines"] == 1
    assert "watermarkIso" not in sidecar["eventPods"]["some-replicaset-abc12"]
    # An app-log pod's events go on its own record, as before.
    assert sidecar["pods"]["pod-a"]["eventLines"] == 1
    # And pods.txt stays the app-log pod list a batch fetch would write.
    assert (nightDir / fetch.PODS_LIST_NAME).read_text().split() == ["pod-a"]


def test_event_only_name_promotes_when_it_starts_logging(
    manager: live.LiveNightManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pod seen in events before its first app log gets one record when
    it finally shows up in a listing, carrying its event counters over."""
    monkeypatch.setattr(live, "listPods", lambda spec: [])
    monkeypatch.setattr(live, "fetchEventsWindowInto", _eventsStub([_eventLine(_t(5), "pod-late")]))
    manager.tick(now=_t(10))

    monkeypatch.setattr(live, "listPods", lambda spec: ["pod-late"])
    monkeypatch.setattr(live, "fetchPodWindowInto", _podFetchStub([_t(12)]))
    monkeypatch.setattr(live, "fetchEventsWindowInto", _eventsStub([]))
    manager.tick(now=_t(20))

    nightDir = fetch.findNightDirCovering("yagan", "rapid-analysis", _t(1), _t(20))
    assert nightDir is not None
    sidecar = fetch.readLiveSidecar(nightDir)
    assert sidecar is not None
    assert sidecar["eventPods"] == {}
    record = sidecar["pods"]["pod-late"]
    assert record["lines"] == 1 and record["eventLines"] == 1
    assert fetch._parseIso(manager.snapshot()["watermark"]) == _t(20)


def test_events_fetch_failure_leaves_span_retryable(
    manager: live.LiveNightManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed events query must not advance the events watermark (the
    span is retried), must not block the app-log watermark, and must
    surface in the snapshot."""
    monkeypatch.setattr(live, "listPods", lambda spec: ["pod-a"])
    monkeypatch.setattr(live, "fetchPodWindowInto", _podFetchStub([_t(3)]))

    def boom(*a: Any, **k: Any) -> tuple[int, bool, str]:
        raise fetch.FetchError("logcli timed out")

    monkeypatch.setattr(live, "fetchEventsWindowInto", boom)
    manager.tick(now=_t(10))
    snap = manager.snapshot()
    assert "logcli timed out" in (snap["eventsError"] or "")
    assert fetch._parseIso(snap["watermark"]) == _t(10)  # app logs unaffected

    seen: list[tuple[dt.datetime, dt.datetime]] = []

    def record(spec: FetchSpec, fromT: dt.datetime, toT: dt.datetime, fh: BinaryIO) -> tuple[int, bool, str]:
        seen.append((fromT, toT))
        return 0, True, ""

    monkeypatch.setattr(live, "fetchEventsWindowInto", record)
    manager.tick(now=_t(20))
    # The retry covers the whole missed span, from night start.
    assert seen == [(dayObsStartUtc(20260711), _t(20))]
    assert manager.snapshot()["eventsError"] is None


def test_events_append_failure_rolls_back(
    manager: live.LiveNightManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Events are demuxed into many files with a single shared watermark,
    so a failure part-way has to roll back — otherwise the retry appends
    the already-written lines a second time and every POD_* marker in
    that span doubles for the rest of the night."""
    monkeypatch.setattr(live, "listPods", lambda spec: ["pod-a"])
    monkeypatch.setattr(live, "fetchPodWindowInto", _podFetchStub([_t(3)]))
    manager.tick(now=_t(10))
    nightDir = fetch.findNightDirCovering("yagan", "rapid-analysis", _t(1), _t(9))
    assert nightDir is not None
    eventsDir = nightDir / fetch.PODS_EVENTS_DIR_NAME

    # Make the *second* name's append fail for real: a directory where the
    # events file should be, so open(..., "ab") raises IsADirectoryError.
    # Names are appended in sorted order, so aaa-pod lands first.
    (eventsDir / "zzz-pod.jsonl").mkdir()
    monkeypatch.setattr(
        live,
        "fetchEventsWindowInto",
        _eventsStub([_eventLine(_t(15), "aaa-pod"), _eventLine(_t(16), "zzz-pod")]),
    )
    manager.tick(now=_t(20))
    # aaa-pod's append is undone along with the failed one.
    assert not (eventsDir / "aaa-pod.jsonl").exists()
    sidecar = fetch.readLiveSidecar(nightDir)
    assert sidecar is not None
    assert fetch._parseIso(sidecar["eventsWatermarkIso"]) == _t(10)
    assert sidecar["eventPods"]["aaa-pod"] == {"eventBytes": 0, "eventLines": 0}
    assert "events append failed" in (manager.snapshot()["eventsError"] or "")
    # The app-log side is unaffected and keeps advancing.
    assert fetch._parseIso(manager.snapshot()["watermark"]) == _t(20)

    # The retry replays the same span exactly once — no duplicates.
    (eventsDir / "zzz-pod.jsonl").rmdir()
    manager.tick(now=_t(30))
    assert (eventsDir / "aaa-pod.jsonl").read_bytes() == _eventLine(_t(15), "aaa-pod")
    assert (eventsDir / "zzz-pod.jsonl").read_bytes() == _eventLine(_t(16), "zzz-pod")
    assert manager.snapshot()["eventsError"] is None


def test_non_pod_events_are_inert_to_the_parser(tmp_path: Path) -> None:
    """Keeping ReplicaSet events is only safe because nothing downstream
    trips over them: summarizeAll enumerates pods from pods/, so a
    lifecycle file with no app-log sibling is never opened, and
    classifyK8sEvent drops any event whose object isn't a Pod."""
    from ra_log_explorer import parse

    (tmp_path / "pods").mkdir()
    (tmp_path / "pods_events").mkdir()
    (tmp_path / "pods" / "pod-a.jsonl").write_bytes(_lokiLine(_t(1)))
    (tmp_path / "pods_events" / "some-replicaset-abc12.jsonl").write_bytes(
        _eventLine(_t(2), "some-replicaset-abc12", "reason=SuccessfulCreate kind=ReplicaSet count=1")
    )
    summaries = parse.summarizeAll(tmp_path)
    assert [s.pod for s in summaries] == ["pod-a"]
    # And even if such a line were handed to the classifier, it declines.
    obj = json.loads(_eventLine(_t(2), "rs", "reason=SuccessfulCreate kind=ReplicaSet count=1"))
    assert parse.classifyK8sEvent("rs", obj) is None


# ----- recovery, orphans, and the deleted-cache case ------------------------


def test_recoverNight_wipes_a_dir_with_no_sidecar(
    tmpCacheRoot: Path, manager: live.LiveNightManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The poller owns the current night's path. A dir left there by
    something else (an aborted batch fetch) can't be trusted to line up
    with a watermark we never wrote, so it starts over."""
    nightDir = windowCachePath(
        "yagan",
        "rapid-analysis",
        fetch._fmtLogcliTime(dayObsStartUtc(20260711)),
        fetch._fmtLogcliTime(dayObsEndUtc(20260711)),
    )
    (nightDir / fetch.PODS_DIR_NAME).mkdir(parents=True)
    (nightDir / fetch.PODS_DIR_NAME / "stale-pod.jsonl").write_bytes(_lokiLine(_t(1)))
    (nightDir / fetch.META_NAME).write_text("{}")

    monkeypatch.setattr(live, "listPods", lambda spec: [])
    manager.tick(now=_t(10))
    assert not (nightDir / fetch.PODS_DIR_NAME / "stale-pod.jsonl").exists()
    assert not (nightDir / fetch.META_NAME).exists()
    assert fetch.readLiveSidecar(nightDir) is not None


def test_recoverNight_clears_stranded_temp_files(
    manager: live.LiveNightManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Increments stage through temp files in the night dir; a crash mid
    tick strands them where nothing else would ever clean them up."""
    monkeypatch.setattr(live, "listPods", lambda spec: ["pod-a"])
    monkeypatch.setattr(live, "fetchPodWindowInto", _podFetchStub([_t(3)]))
    manager.tick(now=_t(10))
    nightDir = fetch.findNightDirCovering("yagan", "rapid-analysis", _t(1), _t(9))
    assert nightDir is not None
    (nightDir / "live-inc-xyz.jsonl").write_bytes(b"half a fetch")
    (nightDir / "live-events-xyz.jsonl").write_bytes(b"half a fetch")

    fresh = live.LiveNightManager(site=_site(), username="u", workers=2, pollS=60.0, lagS=0.0)
    monkeypatch.setattr(live, "listPods", lambda spec: [])
    fresh.tick(now=_t(20))
    assert not (nightDir / "live-inc-xyz.jsonl").exists()
    assert not (nightDir / "live-events-xyz.jsonl").exists()


def test_openNight_adopts_a_finalised_night_without_fetching(
    tmpCacheRoot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--live-day-obs pointed at an already-finalised night: nothing left
    to fetch, so the poller must adopt it rather than re-pull the night."""
    _makeNightDir(tmpCacheRoot, watermark=None, podTimes={"pod-a": [_t(3)]})  # watermark=None => finalised

    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("a finalised night must not be re-fetched")

    monkeypatch.setattr(live, "listPods", boom)
    monkeypatch.setattr(live, "fetchPodWindowInto", boom)
    monkeypatch.setattr(live, "fetchEventsWindowInto", boom)
    monkeypatch.setattr(
        live.exposureTimes, "queryExposureRecordsForDayObs", lambda dayObs, token, *, consdbUrl: []
    )
    mgr = live.LiveNightManager(
        site=_site(), username="u", workers=2, pollS=60.0, lagS=0.0, fixedDayObs=20260711
    )
    mgr.tick(now=dt.datetime(2026, 8, 10, 15, 0, tzinfo=UTC))
    snap = mgr.snapshot()
    assert snap["finalised"] is True
    assert snap["totalLines"] == 1


def test_cache_wipe_under_the_poller_recovers(
    tmpCacheRoot: Path, manager: live.LiveNightManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELETE /api/cache is one click on the home page and takes the
    active night dir with it. Before, every later tick died writing to a
    directory that no longer existed, and live mode stayed dead until the
    next noon rollover."""
    import shutil

    monkeypatch.setattr(live, "listPods", lambda spec: ["pod-a"])
    monkeypatch.setattr(live, "fetchPodWindowInto", _podFetchStub([_t(3), _t(20)]))
    manager.tick(now=_t(10))

    shutil.rmtree(tmpCacheRoot)  # what server._deleteCacheRoot does
    tmpCacheRoot.mkdir(parents=True, exist_ok=True)

    manager.tick(now=_t(30))
    snap = manager.snapshot()
    assert fetch._parseIso(snap["watermark"]) == _t(30)
    nightDir = fetch.findNightDirCovering("yagan", "rapid-analysis", _t(1), _t(30))
    assert nightDir is not None
    # Re-fetched from night start, so nothing is missing from the rebuild.
    assert (nightDir / fetch.PODS_DIR_NAME / "pod-a.jsonl").read_bytes() == _lokiLine(_t(3)) + _lokiLine(
        _t(20)
    )


def test_orphaned_night_is_finalised_on_a_later_tick(
    tmpCacheRoot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart across noon leaves the previous night unfinalised: no
    _meta.json, so the cache listing skips it and LRU eviction can't
    reclaim it, while du still counts its ~9 GiB against the cap. The
    poller sweeps for those when it opens a new night."""
    orphan = _makeNightDir(
        tmpCacheRoot,
        dayObs=20260710,
        watermark=_t(30) - dt.timedelta(days=1),
        podTimes={"pod-a": [dt.datetime(2026, 7, 10, 13, 0, tzinfo=UTC)]},
    )
    assert not (orphan / fetch.META_NAME).exists()
    assert fetch._iterCacheDirs(tmpCacheRoot) == []  # invisible to eviction

    monkeypatch.setattr(live, "listPods", lambda spec: [])
    monkeypatch.setattr(live, "fetchEventsWindowInto", lambda spec, a, b, fh: (0, True, ""))
    monkeypatch.setattr(live, "countPodWindow", lambda spec, pod, a, b: 1)
    monkeypatch.setattr(
        live.exposureTimes, "queryExposureRecordsForDayObs", lambda dayObs, token, *, consdbUrl: []
    )
    mgr = live.LiveNightManager(site=_site(), username="u", workers=2, pollS=60.0, lagS=0.0)
    mgr.tick(now=_t(10))  # opens 20260711, sweeps, finalises the orphan

    meta = json.loads((orphan / fetch.META_NAME).read_text())
    assert meta["liveBuilt"] is True
    assert meta["pod_lines"] == {"pod-a": 1}
    sidecar = fetch.readLiveSidecar(orphan)
    assert sidecar is not None and sidecar["finalised"] is True
    # Now a first-class window: listed, and reclaimable by LRU eviction.
    assert orphan in fetch._iterCacheDirs(tmpCacheRoot)
    assert mgr.snapshot()["orphanError"] is None
    assert mgr.snapshot()["dayObs"] == 20260711


def test_orphan_finalisation_failure_never_breaks_the_tick(
    tmpCacheRoot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _makeNightDir(
        tmpCacheRoot,
        dayObs=20260710,
        watermark=_t(30) - dt.timedelta(days=1),
        podTimes={"pod-a": [dt.datetime(2026, 7, 10, 13, 0, tzinfo=UTC)]},
    )

    def listPods(spec: FetchSpec) -> list[str]:
        if spec.fromIso.startswith("2026-07-10"):
            raise fetch.FetchError("loki is down for that night")
        return []

    monkeypatch.setattr(live, "listPods", listPods)
    monkeypatch.setattr(live, "fetchEventsWindowInto", lambda spec, a, b, fh: (0, True, ""))
    monkeypatch.setattr(
        live.exposureTimes, "queryExposureRecordsForDayObs", lambda dayObs, token, *, consdbUrl: []
    )
    mgr = live.LiveNightManager(site=_site(), username="u", workers=2, pollS=60.0, lagS=0.0)
    mgr.tick(now=_t(10))
    snap = mgr.snapshot()
    assert "loki is down" in (snap["orphanError"] or "")
    # The current night's own tick still completed.
    assert fetch._parseIso(snap["watermark"]) == _t(10)
    # It is re-queued, not dropped: a night nobody finalises is the whole
    # problem, so it keeps being retried (one aborted listing per tick).
    mgr.tick(now=_t(20))
    assert "loki is down" in (mgr.snapshot()["orphanError"] or "")


def test_runLoop_survives_a_failing_tick(
    manager: live.LiveNightManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The poll loop is the only thing keeping live mode alive; one bad
    cycle must be reported, not fatal."""
    calls = {"n": 0}

    def flakyTick(now: dt.datetime | None = None) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("kaboom")
        manager.stop()

    monkeypatch.setattr(manager, "tick", flakyTick)
    monkeypatch.setattr(manager, "_pollS", 30.0)
    manager._runLoop()
    assert calls["n"] >= 2
    assert "kaboom" in (manager.snapshot()["lastError"] or "")


# ----- finalisation ---------------------------------------------------------


def test_rollover_refetch_keeps_its_own_incomplete_flag(
    manager: live.LiveNightManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end-of-night refetch can itself come back unreconcilable (the
    split floor). Clearing the pod's old flags after that call erased the
    new one, publishing the night as complete while a pod was short."""
    monkeypatch.setattr(live, "listPods", lambda spec: ["pod-a"])
    monkeypatch.setattr(live, "fetchPodWindowInto", _podFetchStub([_t(3)]))
    manager.tick(now=_t(10))

    def incompleteRefetch(
        spec: FetchSpec, pod: str, fromT: dt.datetime, toT: dt.datetime, fh: BinaryIO
    ) -> tuple[int, bool, str]:
        fh.write(_lokiLine(_t(3)))
        return 1, False, "burst below the split floor"

    monkeypatch.setattr(live, "fetchPodWindowInto", incompleteRefetch)
    monkeypatch.setattr(live, "countPodWindow", lambda spec, pod, a, b: 9999)
    monkeypatch.setattr(live, "listPods", lambda spec: [])
    manager.tick(now=dt.datetime(2026, 7, 12, 12, 1, tzinfo=UTC))

    oldNight = windowCachePath(
        "yagan",
        "rapid-analysis",
        fetch._fmtLogcliTime(dayObsStartUtc(20260711)),
        fetch._fmtLogcliTime(dayObsEndUtc(20260711)),
    )
    meta = json.loads((oldNight / fetch.META_NAME).read_text())
    assert meta["incomplete_pods"] == {"pod-a": "burst below the split floor"}
    assert meta["fetchComplete"] is False


def test_refetch_publishes_zero_bytes_before_swapping_the_file(
    manager: live.LiveNightManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request thread slicing this pod reads the sidecar, then the file.
    Across the refetch's os.replace it could pair the old byte count with
    the new inode and copy a prefix of it; publishing zero first makes
    the worst case "nothing yet" instead of "a truncated window"."""
    monkeypatch.setattr(live, "listPods", lambda spec: ["pod-a"])
    monkeypatch.setattr(live, "fetchPodWindowInto", _podFetchStub([_t(3)]))
    manager.tick(now=_t(10))
    nightDir = fetch.findNightDirCovering("yagan", "rapid-analysis", _t(1), _t(9))
    assert nightDir is not None

    seen: list[int] = []

    def observingRefetch(
        spec: FetchSpec, pod: str, fromT: dt.datetime, toT: dt.datetime, fh: BinaryIO
    ) -> tuple[int, bool, str]:
        # What a concurrent reader would see mid-swap.
        sidecar = fetch.readLiveSidecar(nightDir)
        assert sidecar is not None
        seen.append(int(sidecar["pods"]["pod-a"]["bytes"]))
        fh.write(_lokiLine(_t(3)))
        return 1, True, ""

    monkeypatch.setattr(live, "fetchPodWindowInto", observingRefetch)
    monkeypatch.setattr(live, "countPodWindow", lambda spec, pod, a, b: 9999)
    monkeypatch.setattr(live, "listPods", lambda spec: [])
    manager.tick(now=dt.datetime(2026, 7, 12, 12, 1, tzinfo=UTC))
    assert seen == [0]
    sidecar = fetch.readLiveSidecar(nightDir)
    assert sidecar is not None and sidecar["pods"]["pod-a"]["bytes"] > 0


# ----- instruments ----------------------------------------------------------


def test_colliding_ids_across_instruments_both_appear(
    manager: live.LiveNightManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LSSTCam and LATISS both number from 1 each night, so on any night
    they co-observe the same id names a different exposure on each. Both
    have to show up, each linkable to its own instrument."""
    records = [
        {"exposure_id": 2026071100001, "obs_end": "2026-07-11T12:05:37", "instrument": "lsstcam"},
        {"exposure_id": 2026071100001, "obs_end": "2026-07-11T12:06:37", "instrument": "latiss"},
    ]
    monkeypatch.setattr(live.exposureTimes, "queryExposureRecordsForDayObs", _consdbStub(records))
    manager.tick(now=_t(30))
    rows = manager.snapshot()["exposures"]
    assert [(r["dataId"], r["instrument"]) for r in rows] == [
        (2026071100001, "latiss"),
        (2026071100001, "lsstcam"),
    ]
    assert all(r["ready"] for r in rows)
    # Each is cached under its own key; the bare key holds the probe-order
    # winner, which is what a lookup with no instrument resolves to.
    latiss = exposureTimes.lookupCachedRecord(2026071100001, siteName="summit", instrument="latiss")
    assert latiss is not None and latiss["obs_end"] == "2026-07-11T12:06:37"
    bare = exposureTimes.lookupCachedRecord(2026071100001, siteName="summit")
    assert bare is not None and bare["instrument"] == "lsstcam"


def test_consdb_records_are_persisted_once_and_as_a_whole(
    manager: live.LiveNightManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Records are immutable, so re-writing the per-site cache file every
    tick is pure churn on the cache volume. But when something *is* new
    the whole list has to go in, not just the delta: the bare-id keys
    hold the probe-order winner across instruments, which can't be
    decided from a subset."""
    cam = {"exposure_id": 2026071100001, "obs_end": "2026-07-11T12:05:37", "instrument": "lsstcam"}
    latiss = {"exposure_id": 2026071100001, "obs_end": "2026-07-11T12:06:37", "instrument": "latiss"}
    stored: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(
        live.exposureTimes,
        "storeCachedRecordList",
        lambda records, *, siteName: stored.append(list(records)),
    )
    monkeypatch.setattr(live.exposureTimes, "queryExposureRecordsForDayObs", _consdbStub([cam]))
    manager.tick(now=_t(10))
    manager.tick(now=_t(20))  # nothing new
    assert len(stored) == 1

    # The LATISS exposure turns up late. The whole list is re-offered, so
    # the bare key stays the probe-order (LSSTCam) record rather than
    # being overwritten by the only record in the delta.
    monkeypatch.setattr(live.exposureTimes, "queryExposureRecordsForDayObs", _consdbStub([cam, latiss]))
    manager.tick(now=_t(30))
    assert len(stored) == 2
    assert stored[1] == [cam, latiss]


def test_orphan_sweep_ignores_a_night_that_has_not_ended(
    tmpCacheRoot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--live-day-obs pins the poller to a historical night, which makes
    the *real* current night's dir "not the one we're tracking" without
    it being finished. Topping that up to an end-time in the future would
    mark a night complete that isn't."""
    _makeNightDir(
        tmpCacheRoot,
        dayObs=20260810,
        watermark=dt.datetime(2026, 8, 10, 14, 0, tzinfo=UTC),
        podTimes={"pod-a": [dt.datetime(2026, 8, 10, 13, 0, tzinfo=UTC)]},
    )
    inProgress = windowCachePath(
        "yagan",
        "rapid-analysis",
        fetch._fmtLogcliTime(dayObsStartUtc(20260810)),
        fetch._fmtLogcliTime(dayObsEndUtc(20260810)),
    )
    monkeypatch.setattr(live, "listPods", lambda spec: [])
    monkeypatch.setattr(live, "fetchEventsWindowInto", lambda spec, a, b, fh: (0, True, ""))
    monkeypatch.setattr(
        live.exposureTimes, "queryExposureRecordsForDayObs", lambda dayObs, token, *, consdbUrl: []
    )
    mgr = live.LiveNightManager(
        site=_site(), username="u", workers=2, pollS=60.0, lagS=0.0, fixedDayObs=20260711
    )
    mgr.tick(now=dt.datetime(2026, 8, 10, 15, 0, tzinfo=UTC))
    assert not (inProgress / fetch.META_NAME).exists()
    assert mgr.snapshot()["orphanError"] is None


def test_fresh_fetch_prunes_files_a_failed_attempt_left_behind(
    tmpCacheRoot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same hazard as the slice path: summarizeAll reads the directory,
    not pods.txt, so a pod file stranded by an earlier attempt on this
    window would be parsed as part of it."""
    spec = _sliceSpec(_t(5), _t(10))
    windowDir = windowCachePath("yagan", "rapid-analysis", spec.fromIso, spec.toIso)
    (windowDir / fetch.PODS_DIR_NAME).mkdir(parents=True)
    (windowDir / fetch.PODS_DIR_NAME / "ghost-pod.jsonl").write_bytes(_lokiLine(_t(6)))
    (windowDir / fetch.PODS_EVENTS_DIR_NAME).mkdir(parents=True)
    (windowDir / fetch.PODS_EVENTS_DIR_NAME / "ghost-pod.jsonl").write_bytes(_lokiLine(_t(6)))

    monkeypatch.setattr(fetch, "listPods", lambda spec: ["real-pod"])
    monkeypatch.setattr(fetch, "_fetchOnePod", lambda spec, pod, outPath: _writeOnePod(outPath, [_t(6)]))
    monkeypatch.setattr(fetch, "_fetchOnePodEvents", lambda spec, pod, outPath: 0)
    cacheDir, meta = fetch.fetchAll(spec)
    assert not (cacheDir / fetch.PODS_DIR_NAME / "ghost-pod.jsonl").exists()
    assert not (cacheDir / fetch.PODS_EVENTS_DIR_NAME / "ghost-pod.jsonl").exists()
    assert sorted(p.stem for p in (cacheDir / fetch.PODS_DIR_NAME).glob("*.jsonl")) == ["real-pod"]


def _writeOnePod(outPath: Path, times: list[dt.datetime]) -> Any:
    outPath.write_bytes(b"".join(_lokiLine(t) for t in times))
    return fetch._PodFetch(
        pod=outPath.stem,
        nbytes=outPath.stat().st_size,
        lines=len(times),
        complete=True,
        reason="",
        expected=len(times),
    )


def test_a_failed_rollover_does_not_block_the_new_night(
    manager: live.LiveNightManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finalisation does a real top-up, so Loki being unwell can fail it.
    Opening tonight is the urgent half — the old night is left to the
    orphan sweep rather than retried in front of everything else."""
    monkeypatch.setattr(live, "listPods", lambda spec: ["pod-a"])
    monkeypatch.setattr(live, "fetchPodWindowInto", _podFetchStub([_t(3)]))
    manager.tick(now=_t(10))

    def listPods(spec: FetchSpec) -> list[str]:
        if spec.fromIso.startswith("2026-07-11"):
            raise fetch.FetchError("loki is down")
        return []

    monkeypatch.setattr(live, "listPods", listPods)
    manager.tick(now=dt.datetime(2026, 7, 12, 12, 1, tzinfo=UTC))
    snap = manager.snapshot()
    assert snap["dayObs"] == 20260712  # tonight opened regardless
    assert "loki is down" in (snap["orphanError"] or "")

    # Once Loki recovers, the sweep finishes the job.
    monkeypatch.setattr(live, "listPods", lambda spec: [])
    monkeypatch.setattr(live, "countPodWindow", lambda spec, pod, a, b: 1)
    manager.tick(now=dt.datetime(2026, 7, 12, 12, 5, tzinfo=UTC))
    oldNight = windowCachePath(
        "yagan",
        "rapid-analysis",
        fetch._fmtLogcliTime(dayObsStartUtc(20260711)),
        fetch._fmtLogcliTime(dayObsEndUtc(20260711)),
    )
    assert json.loads((oldNight / fetch.META_NAME).read_text())["liveBuilt"] is True
    assert manager.snapshot()["orphanError"] is None


def test_an_unreconcilable_increment_flags_the_pod(
    manager: live.LiveNightManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chunk the fetcher couldn't prove lossless (the split floor) is a
    soft fall-short: the lines that did arrive are kept and the watermark
    advances, but the pod is flagged so the night can't pass for
    complete. It stays flagged for the rest of the night — a pod short
    somewhere may be short anywhere, including in a later slice."""

    def unreconcilable(
        spec: FetchSpec, pod: str, fromT: dt.datetime, toT: dt.datetime, fh: BinaryIO
    ) -> tuple[int, bool, str]:
        fh.write(_lokiLine(fromT))
        return 1, False, "5000-line burst in under a second"

    monkeypatch.setattr(live, "listPods", lambda spec: ["pod-a"])
    monkeypatch.setattr(live, "fetchPodWindowInto", unreconcilable)
    manager.tick(now=_t(10))
    snap = manager.snapshot()
    assert snap["incompletePods"] == {"pod-a": "5000-line burst in under a second"}
    assert fetch._parseIso(snap["watermark"]) == _t(10)  # not a hard failure

    # And it reaches any window sliced out of the night.
    nightDir = fetch.findNightDirCovering("yagan", "rapid-analysis", _t(1), _t(9))
    assert nightDir is not None
    _, meta = fetch.materializeNightSlice(nightDir, _sliceSpec(_t(1), _t(9)))
    assert meta["fetchComplete"] is False
    assert "pod-a" in meta["incomplete_pods"]


def test_a_failed_end_of_night_refetch_is_reported_not_swallowed(
    manager: live.LiveNightManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refetch is the last chance to be right about a short pod. If
    it can't run, the night must say so rather than ship as complete."""
    monkeypatch.setattr(live, "listPods", lambda spec: ["pod-a"])
    monkeypatch.setattr(live, "fetchPodWindowInto", _podFetchStub([_t(3)]))
    manager.tick(now=_t(10))

    def failingRefetch(*a: Any, **k: Any) -> tuple[int, bool, str]:
        raise fetch.FetchError("logcli timed out")

    monkeypatch.setattr(live, "fetchPodWindowInto", failingRefetch)
    monkeypatch.setattr(live, "countPodWindow", lambda spec, pod, a, b: 9999)
    monkeypatch.setattr(live, "listPods", lambda spec: [])
    manager.tick(now=dt.datetime(2026, 7, 12, 12, 1, tzinfo=UTC))

    oldNight = windowCachePath(
        "yagan",
        "rapid-analysis",
        fetch._fmtLogcliTime(dayObsStartUtc(20260711)),
        fetch._fmtLogcliTime(dayObsEndUtc(20260711)),
    )
    meta = json.loads((oldNight / fetch.META_NAME).read_text())
    assert "end-of-night refetch failed" in meta["errors"]["pod-a"]
    assert meta["fetchComplete"] is False


def test_events_rollback_truncates_rather_than_deleting_prior_content(
    manager: live.LiveNightManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name that already had events keeps them: the rollback undoes
    this span's append, not the file."""
    monkeypatch.setattr(live, "listPods", lambda spec: ["pod-a"])
    monkeypatch.setattr(live, "fetchPodWindowInto", _podFetchStub([_t(3)]))
    monkeypatch.setattr(live, "fetchEventsWindowInto", _eventsStub([_eventLine(_t(5), "aaa-pod")]))
    manager.tick(now=_t(10))
    nightDir = fetch.findNightDirCovering("yagan", "rapid-analysis", _t(1), _t(9))
    assert nightDir is not None
    eventsDir = nightDir / fetch.PODS_EVENTS_DIR_NAME
    firstSpan = (eventsDir / "aaa-pod.jsonl").read_bytes()
    assert firstSpan == _eventLine(_t(5), "aaa-pod")

    (eventsDir / "zzz-pod.jsonl").mkdir()
    monkeypatch.setattr(
        live,
        "fetchEventsWindowInto",
        _eventsStub([_eventLine(_t(15), "aaa-pod"), _eventLine(_t(16), "zzz-pod")]),
    )
    manager.tick(now=_t(20))
    assert (eventsDir / "aaa-pod.jsonl").read_bytes() == firstSpan
    sidecar = fetch.readLiveSidecar(nightDir)
    assert sidecar is not None
    assert sidecar["eventPods"]["aaa-pod"]["eventLines"] == 1


def test_recoverNight_drops_event_files_the_sidecar_never_heard_of(
    tmpCacheRoot: Path, manager: live.LiveNightManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bytes nothing vouches for are bytes we can't place in time."""
    nightDir = _makeNightDir(tmpCacheRoot, watermark=_t(10), podTimes={"pod-a": [_t(3)]})
    (nightDir / fetch.PODS_EVENTS_DIR_NAME / "ghost.jsonl").write_bytes(_eventLine(_t(4), "ghost"))
    monkeypatch.setattr(live, "listPods", lambda spec: [])
    manager.tick(now=_t(20))
    assert not (nightDir / fetch.PODS_EVENTS_DIR_NAME / "ghost.jsonl").exists()


def test_consdb_failure_is_surfaced_without_stopping_the_night(
    manager: live.LiveNightManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ConsDB and Loki fail independently; losing the exposure list must
    not cost us the logs (or vice versa)."""
    monkeypatch.setattr(live, "listPods", lambda spec: ["pod-a"])
    monkeypatch.setattr(live, "fetchPodWindowInto", _podFetchStub([_t(3)]))

    def boom(*a: Any, **k: Any) -> Any:
        raise exposureTimes.ConsDbError("ConsDB HTTP 503: Service Unavailable")

    monkeypatch.setattr(live.exposureTimes, "queryExposureRecordsForDayObs", boom)
    manager.tick(now=_t(10))
    snap = manager.snapshot()
    assert "503" in (snap["consdbError"] or "")
    assert snap["exposures"] == []
    assert fetch._parseIso(snap["watermark"]) == _t(10)
    assert snap["totalLines"] == 1
