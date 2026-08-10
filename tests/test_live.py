"""Tests for the live night cache: slicing in `fetch`, polling in `live`."""

from __future__ import annotations

import datetime as dt
import json
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
        live.exposureTimes, "queryExposureRecordsForDayObs", lambda dayObs, token, *, consdbUrl: {}
    )
    return live.LiveNightManager(site=_site(), username="u", workers=2, pollS=60.0, lagS=0.0)


def _podFetchStub(times: list[dt.datetime]) -> Any:
    """A fetchPodWindowInto stand-in emitting ``times`` clipped to the window."""

    def stub(
        spec: FetchSpec, pod: str, fromT: dt.datetime, toT: dt.datetime, fh: BinaryIO
    ) -> tuple[int, bool, str]:
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
    monkeypatch.setattr(live, "listPods", lambda spec: ["pod-a"])
    monkeypatch.setattr(live, "fetchPodWindowInto", _podFetchStub([_t(3), _t(7), _t(40)]))

    manager.tick(now=_t(10))
    snap = manager.snapshot()
    assert snap["dayObs"] == 20260711
    assert fetch._parseIso(snap["watermark"]) == _t(10)
    assert snap["totalLines"] == 2  # 12:40 is beyond the first target

    manager.tick(now=_t(50))
    snap = manager.snapshot()
    assert fetch._parseIso(snap["watermark"]) == _t(50)
    assert snap["totalLines"] == 3

    nightDir = fetch.findNightDirCovering("yagan", "rapid-analysis", _t(1), _t(45))
    assert nightDir is not None
    podFile = nightDir / fetch.PODS_DIR_NAME / "pod-a.jsonl"
    # Ticks tile: three lines, once each, in order.
    assert podFile.read_bytes() == _lokiLine(_t(3)) + _lokiLine(_t(7)) + _lokiLine(_t(40))


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


def test_readiness_follows_watermark(manager: live.LiveNightManager, monkeypatch: pytest.MonkeyPatch) -> None:
    # obs_end is TAI = UTC + 37 s. Shutter closes 12:10:00 and 12:29:00 UTC.
    records = {
        2026071100001: {"exposure_id": 2026071100001, "obs_end": "2026-07-11T12:10:37"},
        2026071100002: {"exposure_id": 2026071100002, "obs_end": "2026-07-11T12:29:37"},
    }
    monkeypatch.setattr(
        live.exposureTimes,
        "queryExposureRecordsForDayObs",
        lambda dayObs, token, *, consdbUrl: records,
    )
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
        live.exposureTimes, "queryExposureRecordsForDayObs", lambda dayObs, token, *, consdbUrl: {}
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
