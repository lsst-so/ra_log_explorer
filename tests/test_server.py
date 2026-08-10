"""Tests for `ra_log_explorer.server`."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ra_log_explorer import config, exposureTimes
from ra_log_explorer import fetch as fetchModule
from ra_log_explorer import parse, server, sites
from ra_log_explorer.jobs import FetchJob, JobManager
from ra_log_explorer.server import ServerContext, _loadExposureFromCache

from .conftest import FakeSiteCatalog


def _ctxWithSites(siteCatalog: FakeSiteCatalog) -> server.ServerContext:
    """Build a ServerContext seeded with the test catalog. Match what
    `cli.cmdRun` does at startup so the unit-level tests of the request
    builders exercise the same plumbing.
    """
    return server.ServerContext(
        jobs=JobManager(),
        sites=siteCatalog.catalog,
        siteName=siteCatalog.defaultName,
    )


# ----- task palette -------------------------------------------------------


def test_assignTaskColors_pinned_isr_and_calibrate() -> None:
    mapping = server._assignTaskColors(["isr", "calibrateImage"])
    assert mapping["isr"] == server._TASK_COLOR_PINNED["isr"]
    assert mapping["calibrateImage"] == server._TASK_COLOR_PINNED["calibrateImage"]


def test_assignTaskColors_is_collision_free_up_to_palette_size() -> None:
    # The palette currently has 24 entries; sorted-index assignment must
    # not produce duplicate colours up to that count.
    n = len(server._TASK_PALETTE)
    tasks = [f"task{i:02d}" for i in range(n)]
    mapping = server._assignTaskColors(tasks)
    assert len(set(mapping.values())) == n


def test_assignTaskColors_is_collision_free_for_real_pipeline_tasks() -> None:
    # The full LSSTCam SFM + AOS pipeline graph today emits ~16 distinct
    # task labels. They must all get distinct colours.
    real = [
        "isr",
        "calibrateImage",
        "consolidateVisitSummary",
        "generateDonutDirectDetectTask",
        "cutOutDonutsCwfsPairTask",
        "reassignCwfsCutoutsPairTask",
        "calcZernikesTask",
        "aggregateZernikeTablesTask",
        "aggregateDonutStampsTask",
        "aggregateDonutTablesCwfsTask",
        "aggregateAOSVisitTableCwfsTask",
        "plotAOSTask",
        "plotDonutCwfsTask",
        "plotDonutFitsTask",
        "plotPairingTask",
        "plotPsfZernTask",
    ]
    mapping = server._assignTaskColors(real)
    assert len(mapping) == len(real)
    assert len(set(mapping.values())) == len(real)


def test_assignTaskColors_cycles_beyond_palette() -> None:
    # When tasks exceed palette size we accept collisions (a documented limit).
    # Verify that nothing crashes and that pinned tasks still get their
    # designated colours.
    n = len(server._TASK_PALETTE) + 4
    tasks = [f"task{i:02d}" for i in range(n)] + ["isr"]
    mapping = server._assignTaskColors(tasks)
    assert mapping["isr"] == server._TASK_COLOR_PINNED["isr"]
    assert len(mapping) == len(set(tasks))


def test_assignTaskColors_is_stable_under_input_reorder() -> None:
    # Sort-by-name means the output is invariant to input order.
    a = server._assignTaskColors(["b", "a", "c"])
    b = server._assignTaskColors(["c", "a", "b"])
    assert a == b


# ----- _toJsonable --------------------------------------------------------


def test_toJsonable_datetime_becomes_isoformat() -> None:
    t = dt.datetime(2026, 5, 20, 8, 45, 39, tzinfo=dt.timezone.utc)
    assert server._toJsonable(t) == t.isoformat()


def test_toJsonable_set_becomes_sorted_list() -> None:
    assert server._toJsonable({"b", "a", "c"}) == ["a", "b", "c"]


def test_toJsonable_dataclass_recurses() -> None:
    @dataclass
    class Inner:
        x: int

    @dataclass
    class Outer:
        a: int
        b: Inner

    out = server._toJsonable(Outer(a=1, b=Inner(x=2)))
    assert out == {"a": 1, "b": {"x": 2}}


def test_toJsonable_passes_primitives_through() -> None:
    assert server._toJsonable(42) == 42
    assert server._toJsonable("hi") == "hi"
    assert server._toJsonable(None) is None
    assert server._toJsonable([1, 2, 3]) == [1, 2, 3]
    assert server._toJsonable({"a": 1, "b": 2}) == {"a": 1, "b": 2}


def test_toJsonable_handles_nested_containers() -> None:
    payload = {"list": [{"set": {"a", "b"}}, dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)]}
    out = server._toJsonable(payload)
    assert out == {"list": [{"set": ["a", "b"]}, "2026-01-01T00:00:00+00:00"]}


# ----- _summaryToDict relevance window ------------------------------------


def _stubSummary(events: list[parse.Event], pod: str = "p", nLines: int = 0) -> parse.PodSummary:
    return parse.PodSummary(
        pod=pod,
        group="other",
        instrument=None,
        ordinal=None,
        nLines=nLines or len(events),
        nWarn=0,
        nError=0,
        nTraceback=0,
        firstTs=None,
        lastTs=None,
        expIdsSeen=set(),
        events=events,
    )


def _ev(t: dt.datetime, kind: str, expId: int | None = None, level: str = "info") -> parse.Event:
    return parse.Event(pod="p", t=t, kind=kind, level=level, expId=expId, raw="x")


def test_summaryToDict_includes_only_target_expId() -> None:
    tZero = dt.datetime(2026, 5, 20, 8, 45, 39, tzinfo=dt.timezone.utc)
    events = [
        _ev(tZero, "WORKER_PICKUP", expId=2026051900722),
        _ev(tZero, "WORKER_PICKUP", expId=2026051900721),  # different exposure
    ]
    out = server._summaryToDict(_stubSummary(events), tZero, 2026051900722)
    kinds = [e["kind"] for e in out["events"]]
    assert kinds == ["WORKER_PICKUP"]
    assert out["events"][0]["expId"] == 2026051900722


def test_summaryToDict_keeps_untagged_warn_only_in_work_window() -> None:
    tZero = dt.datetime(2026, 5, 20, 8, 45, 39, tzinfo=dt.timezone.utc)
    # Two relevant events bracketing the work window.
    relevantStart = tZero
    relevantEnd = tZero + dt.timedelta(seconds=10)
    events = [
        # Untagged WARN, well before the work window: should be filtered out.
        _ev(tZero - dt.timedelta(seconds=60), "WARN", level="warn"),
        # The two relevant events.
        _ev(relevantStart, "WORKER_PICKUP", expId=2026051900722),
        _ev(relevantStart + dt.timedelta(seconds=1), "WARN", level="warn"),  # in-window WARN -> kept
        _ev(relevantEnd, "WORKER_BINNED_PRELIMINARY_VISIT_IMAGE", expId=2026051900722),
        # Untagged WARN, well after: filtered out.
        _ev(relevantEnd + dt.timedelta(seconds=60), "WARN", level="warn"),
    ]
    out = server._summaryToDict(_stubSummary(events), tZero, 2026051900722)
    kinds = [e["kind"] for e in out["events"]]
    # 2 targeted + 1 in-window untagged warn = 3
    assert kinds == ["WORKER_PICKUP", "WARN", "WORKER_BINNED_PRELIMINARY_VISIT_IMAGE"]


def test_summaryToDict_anchors_untagged_window_on_tZero_when_no_targeted_events() -> None:
    # A pod with no explicitly-tagged events for this dataId still surfaces
    # its untagged warnings, but scoped to a t₀-anchored window rather than
    # the whole (possibly very wide) fetch. This matters for range mode and
    # superset-reuse exposure views, where "keep all untagged" would pull
    # the entire span's warnings into a single dataId's timeline. The
    # fallback window is (t₀ - DEFAULT_WINDOW_BEFORE_S, t₀ +
    # DEFAULT_WINDOW_AFTER_S) = (t₀ - 5 s, t₀ + 300 s).
    tZero = dt.datetime(2026, 5, 20, 8, 45, 39, tzinfo=dt.timezone.utc)
    events = [
        _ev(tZero - dt.timedelta(seconds=60), "WARN", level="warn"),  # before window -> dropped
        _ev(tZero + dt.timedelta(seconds=60), "WARN", level="warn"),  # inside window -> kept
        _ev(tZero + dt.timedelta(seconds=3600), "WARN", level="warn"),  # far after -> dropped
    ]
    out = server._summaryToDict(_stubSummary(events), tZero, 2026051900722)
    assert len(out["events"]) == 1


# ----- _buildSummaryPayload reference points ------------------------------


def test_buildSummaryPayload_derives_head_define_visit_reference(headNodeJsonl: Path) -> None:
    headSummary = parse.summarizePod(headNodeJsonl)
    # The fixture's pod name slug doesn't classify as 'head' on its own, so
    # we manually re-group for the reference-point derivation to fire.
    headSummary.group = "head"
    headSummary.expIdsSeen.add(2026051900722)
    state = server.ServerState(
        cacheDir=headNodeJsonl.parent,
        cacheBytes=0,
        meta={},
        summaries=[headSummary],
        expId=2026051900722,
        tZero=dt.datetime(2026, 5, 20, 8, 45, 39, 267000, tzinfo=dt.timezone.utc),
        referencePoints=[
            {"label": "shutter close", "t": "", "offsetS": 0.0, "source": "shutter close"},
        ],
    )
    payload = server._buildSummaryPayload(state)
    refLabels = [r["label"] for r in payload["referencePoints"]]
    assert "shutter close" in refLabels
    assert "head node first defined visit" in refLabels


def test_buildSummaryPayload_taskColors_collision_free_over_fixtures(
    sfmWorkerJsonl: Path, aosWorkerJsonl: Path
) -> None:
    sfm = parse.summarizePod(sfmWorkerJsonl)
    sfm.expIdsSeen.add(2026051900722)
    aos = parse.summarizePod(aosWorkerJsonl)
    aos.expIdsSeen.add(2026051900722)
    state = server.ServerState(
        cacheDir=sfmWorkerJsonl.parent,
        cacheBytes=0,
        meta={},
        summaries=[sfm, aos],
        expId=2026051900722,
        tZero=dt.datetime(2026, 5, 20, 8, 45, 39, 267000, tzinfo=dt.timezone.utc),
    )
    payload = server._buildSummaryPayload(state)
    tc = payload["taskColors"]
    # Every distinct task in the fixtures must have a unique hex colour.
    assert len(tc) > 0
    assert len(set(tc.values())) == len(tc)


def test_summaryToDict_emits_per_pod_stats() -> None:
    """The per-pod payload includes start / duration / QG-build / wait
    stats that the UI's hover-tooltip consumes.
    """
    tZero = dt.datetime(2026, 5, 20, 8, 45, 39, tzinfo=dt.timezone.utc)
    firstT = tZero + dt.timedelta(seconds=40)
    lastT = tZero + dt.timedelta(seconds=72)
    qgBuiltEv = parse.Event(
        pod="p",
        t=firstT + dt.timedelta(seconds=2),
        kind="WORKER_QG_BUILT",
        level="info",
        expId=2026051900722,
        durationS=1.4,
    )
    finishEv = parse.Event(
        pod="p",
        t=lastT,
        kind="QUANTUM_DONE",
        level="info",
        expId=2026051900722,
        durationS=2.0,
    )
    summary = parse.PodSummary(
        pod="s-lsstcam-run-sfm-runner-workerset-5",
        group="sfm",
        instrument="LSSTCam",
        ordinal=5,
        nLines=100,
        nWarn=0,
        nError=0,
        nTraceback=0,
        firstTs=firstT - dt.timedelta(seconds=5),
        lastTs=lastT + dt.timedelta(seconds=10),
        expIdsSeen={2026051900722},
        events=[qgBuiltEv, finishEv],
        expIdFirstLast={2026051900722: (firstT, lastT)},
        expIdWaitSeconds={2026051900722: 0.36},
    )
    out = server._summaryToDict(summary, tZero, 2026051900722)
    assert out["firstRelevantOffsetS"] == pytest.approx(40.0)
    assert out["lastRelevantOffsetS"] == pytest.approx(72.0)
    assert out["relevantDurationS"] == pytest.approx(32.0)
    assert out["qgBuildSeconds"] == pytest.approx(1.4)
    assert out["waitSeconds"] == pytest.approx(0.36)
    # Clean SFM run: a finish event is present, so not flagged. The
    # 'looksTruncatedStart' field is intentionally absent — processing
    # cannot start before shutter close, which is inside the fetch
    # window by construction.
    assert "looksTruncatedStart" not in out
    assert out["looksTruncatedEnd"] is False


def test_summaryToDict_flags_truncated_end_when_worker_has_no_finish_event() -> None:
    """For sfm/aos/step1b/step1b-aos/backlog workers, absence of any
    canonical finish event (QUANTUM_DONE / WORKER_REPORT_* /
    WORKER_BINNED_*) for this expId means we likely cut off before the
    pod finished. A clean run with a finish event must NOT be flagged.
    """
    tZero = dt.datetime(2026, 5, 20, 8, 45, 39, tzinfo=dt.timezone.utc)
    firstT = tZero + dt.timedelta(seconds=40)
    lastT = tZero + dt.timedelta(seconds=72)
    summaryNoFinish = parse.PodSummary(
        pod="s-lsstcam-run-sfm-runner-workerset-9",
        group="sfm",
        instrument="LSSTCam",
        ordinal=9,
        nLines=10,
        nWarn=0,
        nError=0,
        nTraceback=0,
        firstTs=firstT,
        lastTs=lastT,
        expIdsSeen={2026051900722},
        events=[],
        expIdFirstLast={2026051900722: (firstT, lastT)},
    )
    out = server._summaryToDict(summaryNoFinish, tZero, 2026051900722)
    assert out["looksTruncatedEnd"] is True

    # A QUANTUM_DONE for this expId is enough to clear the flag — even
    # if it's at the very last line. (The previous timestamp-edge
    # heuristic would have false-positived here.)
    finish = parse.Event(
        pod="p", t=lastT, kind="QUANTUM_DONE", level="info", expId=2026051900722, durationS=1.2
    )
    summaryClean = parse.PodSummary(
        pod="s-lsstcam-run-sfm-runner-workerset-9",
        group="sfm",
        instrument="LSSTCam",
        ordinal=9,
        nLines=10,
        nWarn=0,
        nError=0,
        nTraceback=0,
        firstTs=firstT,
        lastTs=lastT,
        expIdsSeen={2026051900722},
        events=[finish],
        expIdFirstLast={2026051900722: (firstT, lastT)},
    )
    out = server._summaryToDict(summaryClean, tZero, 2026051900722)
    assert out["looksTruncatedEnd"] is False


def test_summaryToDict_does_not_flag_truncation_outside_worker_groups() -> None:
    """Head / metadata-server / cluster-mgr / plotter / one-off pods are
    NEVER flagged truncated — there's no canonical finish event we can
    expect from them, so we don't guess.
    """
    tZero = dt.datetime(2026, 5, 20, 8, 45, 39, tzinfo=dt.timezone.utc)
    firstT = tZero + dt.timedelta(seconds=40)
    lastT = tZero + dt.timedelta(seconds=72)
    for group, pod in [
        ("head", "s-lsstcam-run-head-node-abc-xyz"),
        ("metadata-server", "s-lsstcam-run-metadata-server-abc"),
        ("plotter", "s-lsstcam-run-plotter-abc"),
        ("psf-plot", "s-lsstcam-run-psf-plotting-abc"),
        ("one-off-postisr", "s-lsstcam-run-one-off-post-isr-abc"),
        ("other", "redis-0"),
    ]:
        summary = parse.PodSummary(
            pod=pod,
            group=group,
            instrument=None,
            ordinal=None,
            nLines=10,
            nWarn=0,
            nError=0,
            nTraceback=0,
            firstTs=firstT,
            lastTs=lastT,
            expIdsSeen={2026051900722},
            events=[],
            expIdFirstLast={2026051900722: (firstT, lastT)},
        )
        out = server._summaryToDict(summary, tZero, 2026051900722)
        assert out["looksTruncatedEnd"] is False, f"{group} mis-flagged"


def test_buildSummaryPayload_surfaces_other_pods_even_without_expId(sfmWorkerJsonl: Path) -> None:
    """Pods classified as 'other' must show up in the timeline even if their
    logs never mention the target dataId. This is the safety net so an
    unknown / mis-classified role still surfaces its warnings instead of
    being silently dropped.
    """
    sfm = parse.summarizePod(sfmWorkerJsonl)
    sfm.expIdsSeen.add(2026051900722)
    mystery = parse.PodSummary(
        pod="something-we-dont-classify",
        group="other",
        instrument=None,
        ordinal=None,
        nLines=1,
        nWarn=1,
        nError=0,
        nTraceback=0,
        firstTs=None,
        lastTs=None,
        expIdsSeen=set(),  # deliberately empty
    )
    state = server.ServerState(
        cacheDir=sfmWorkerJsonl.parent,
        cacheBytes=0,
        meta={},
        summaries=[sfm, mystery],
        expId=2026051900722,
        tZero=dt.datetime(2026, 5, 20, 8, 45, 39, 267000, tzinfo=dt.timezone.utc),
    )
    payload = server._buildSummaryPayload(state)
    podsInTimeline = {p["pod"] for p in payload["pods"]}
    assert "something-we-dont-classify" in podsInTimeline


def test_buildSummaryPayload_includes_groupLabels(sfmWorkerJsonl: Path) -> None:
    """The frontend strips the role prefix from each pod name; for that it
    needs the label → needle map from `parse.groupLabels()` in the payload."""
    sfm = parse.summarizePod(sfmWorkerJsonl)
    sfm.expIdsSeen.add(2026051900722)
    state = server.ServerState(
        cacheDir=sfmWorkerJsonl.parent,
        cacheBytes=0,
        meta={},
        summaries=[sfm],
        expId=2026051900722,
        tZero=dt.datetime(2026, 5, 20, 8, 45, 39, 267000, tzinfo=dt.timezone.utc),
    )
    payload = server._buildSummaryPayload(state)
    assert payload["groupLabels"] == parse.groupLabels()
    assert payload["groupLabels"]["sfm"] == "sfm-runner"


# ----- _eventToDict -------------------------------------------------------


def test_eventToDict_emits_offsetS_relative_to_tZero() -> None:
    tZero = dt.datetime(2026, 5, 20, 8, 45, 39, tzinfo=dt.timezone.utc)
    ev = parse.Event(
        pod="p",
        t=tZero + dt.timedelta(seconds=5.25),
        kind="WORKER_PICKUP",
        level="info",
    )
    d = server._eventToDict(ev, tZero)
    assert d["offsetS"] == pytest.approx(5.25)
    assert d["kind"] == "WORKER_PICKUP"
    assert d["pod"] == "p"


# ----- ServerContext keyed-state helpers ----------------------------------


def _makeExposureState(expId: int, *, cacheDir: Path | None = None) -> server.ServerState:
    """Minimal ServerState for keyed-storage tests — content doesn't matter."""
    return server.ServerState(
        cacheDir=cacheDir or Path(f"/tmp/cache-{expId}"),
        cacheBytes=0,
        meta={},
        summaries=[],
        expId=expId,
        tZero=dt.datetime(2026, 5, 21, 12, 0, tzinfo=dt.timezone.utc),
    )


def _makeNightState(dayObs: int, *, cacheDir: Path | None = None) -> server.NightState:
    return server.NightState(
        cacheDir=cacheDir or Path(f"/tmp/night-{dayObs}"),
        cacheBytes=0,
        meta={},
        summaries=[],
        dayObs=dayObs,
        startTime=dt.datetime(2026, 5, 21, 12, 0, tzinfo=dt.timezone.utc),
        endTime=dt.datetime(2026, 5, 22, 12, 0, tzinfo=dt.timezone.utc),
    )


def _makeRangeState(startId: int, stopId: int, *, cacheDir: Path | None = None) -> server.RangeState:
    return server.RangeState(
        cacheDir=cacheDir or Path(f"/tmp/range-{startId}-{stopId}"),
        cacheBytes=0,
        meta={},
        summaries=[],
        startId=startId,
        stopId=stopId,
        fromTime=dt.datetime(2026, 5, 20, 8, 45, tzinfo=dt.timezone.utc),
        toTime=dt.datetime(2026, 5, 20, 8, 51, tzinfo=dt.timezone.utc),
    )


def _emptyCtx() -> server.ServerContext:
    from ra_log_explorer.jobs import JobManager

    return server.ServerContext(jobs=JobManager())


def test_put_then_get_exposure_state_roundtrips() -> None:
    ctx = _emptyCtx()
    s = _makeExposureState(2026051900001)
    ctx.putExposureState(s)
    assert ctx.getExposureState(2026051900001) is s


def test_get_exposure_state_returns_None_when_unknown() -> None:
    ctx = _emptyCtx()
    assert ctx.getExposureState(2026051900001) is None


def test_put_then_get_night_state_roundtrips() -> None:
    ctx = _emptyCtx()
    n = _makeNightState(20260521)
    ctx.putNightState(n)
    assert ctx.getNightState(20260521) is n


def test_put_then_get_range_state_roundtrips() -> None:
    ctx = _emptyCtx()
    r = _makeRangeState(2026051900722, 2026051900750)
    ctx.putRangeState(r)
    assert ctx.getRangeState(server.rangeKey(2026051900722, 2026051900750)) is r


def test_get_range_state_returns_None_when_unknown() -> None:
    ctx = _emptyCtx()
    assert ctx.getRangeState(server.rangeKey(1, 2)) is None


def test_evictByCacheDir_drops_matching_range_state(tmp_path: Path) -> None:
    ctx = _emptyCtx()
    r = _makeRangeState(2026051900722, 2026051900750, cacheDir=tmp_path)
    ctx.putRangeState(r)
    ctx.evictByCacheDir(tmp_path)
    assert ctx.getRangeState(server.rangeKey(2026051900722, 2026051900750)) is None


def test_two_exposures_coexist_independently() -> None:
    """The multi-tab promise: loading exposure A doesn't evict exposure B."""
    ctx = _emptyCtx()
    a = _makeExposureState(2026051900001)
    b = _makeExposureState(2026051900002)
    ctx.putExposureState(a)
    ctx.putExposureState(b)
    assert ctx.getExposureState(2026051900001) is a
    assert ctx.getExposureState(2026051900002) is b


def test_lru_eviction_at_max_states(monkeypatch: pytest.MonkeyPatch) -> None:
    """Putting more than MAX states evicts the oldest by last-access."""
    monkeypatch.setattr(server, "_MAX_LOADED_STATES", 3)
    ctx = _emptyCtx()
    for eid in (1, 2, 3):
        ctx.putExposureState(_makeExposureState(eid))
    # Touch 1 so it becomes most-recently-used; 2 is oldest.
    ctx.getExposureState(1)
    ctx.putExposureState(_makeExposureState(4))
    assert ctx.getExposureState(2) is None  # evicted
    assert ctx.getExposureState(1) is not None
    assert ctx.getExposureState(3) is not None
    assert ctx.getExposureState(4) is not None


def test_evictByCacheDir_drops_matching_states(tmp_path: Path) -> None:
    """Deleting a cache window drops any state pointed at it."""
    ctx = _emptyCtx()
    cdA = tmp_path / "cache-A"
    cdA.mkdir()
    cdB = tmp_path / "cache-B"
    cdB.mkdir()
    ctx.putExposureState(_makeExposureState(1, cacheDir=cdA))
    ctx.putExposureState(_makeExposureState(2, cacheDir=cdB))
    ctx.putNightState(_makeNightState(20260521, cacheDir=cdA))
    ctx.evictByCacheDir(cdA)
    assert ctx.getExposureState(1) is None  # matched, evicted
    assert ctx.getExposureState(2) is not None  # unrelated, kept
    assert ctx.getNightState(20260521) is None  # matched, evicted


# ----- _taiIsoToUtc --------------------------------------------------------


def test_taiIsoToUtc_applies_TAI_minus_UTC_offset() -> None:
    """ConsDB obs_end is in TAI without a timezone marker; the helper
    must (1) treat it as TAI and (2) subtract 37s to land at UTC. Both
    halves matter — getting either wrong silently shifts every
    histogram bar.
    """
    out = server._taiIsoToUtc("2026-05-20T08:46:16.267000")
    expected = dt.datetime(2026, 5, 20, 8, 45, 39, 267000, tzinfo=dt.timezone.utc)
    assert out == expected


def test_utcToTaiIso_inverts_taiIsoToUtc() -> None:
    """Persisting a hand-entered shutter close depends on this inverse: a
    UTC tZero must serialise back to the exact TAI ``obs_end`` string it
    came from, so the manual value round-trips through the per-site cache.
    """
    taiIso = "2026-06-24T14:38:41.380663"
    utc = server._taiIsoToUtc(taiIso)
    assert server._utcToTaiIso(utc) == taiIso


# ----- _buildNightPayload --------------------------------------------------


def test_buildNightPayload_carries_histograms_and_stats() -> None:
    """A minimal NightState end-to-end through ``_buildNightPayload``:
    summaries with one task-pickup + one calcZernikes-done land in the
    two histograms; tracebacks land in the failures table.
    """
    tShutter = dt.datetime(2026, 5, 21, 13, 0, 0, tzinfo=dt.timezone.utc)
    tPickup = tShutter + dt.timedelta(seconds=5)
    tCzEnd = tShutter + dt.timedelta(seconds=90)
    pickupEv = parse.Event(pod="p", t=tPickup, kind="QUANTUM_PREP", level="info", expId=100, taskLabel="isr")
    czDoneEv = parse.Event(
        pod="p",
        t=tCzEnd,
        kind="QUANTUM_DONE",
        level="info",
        expId=100,
        taskLabel="calcZernikesTask",
        durationS=2.0,
    )
    tbRecord = parse.TracebackRecord(
        pod="p",
        t=tCzEnd + dt.timedelta(seconds=1),
        expId=100,
        excClass="RuntimeError",
        excMessage="oops",
        body="",
    )
    summary = parse.PodSummary(
        pod="p",
        group="aos",
        instrument=None,
        ordinal=None,
        nLines=10,
        nWarn=0,
        nError=1,
        nTraceback=1,
        firstTs=tPickup,
        lastTs=tCzEnd,
        expIdsSeen={100},
        events=[pickupEv, czDoneEv],
        tracebacks=[tbRecord],
    )
    state = server.NightState(
        cacheDir=Path("/tmp/dummy"),
        cacheBytes=0,
        meta={},
        summaries=[summary],
        dayObs=20260521,
        startTime=dt.datetime(2026, 5, 21, 12, 0, tzinfo=dt.timezone.utc),
        endTime=dt.datetime(2026, 5, 22, 12, 0, tzinfo=dt.timezone.utc),
        shutterCloseByExpId={100: tShutter},
    )
    payload = server._buildNightPayload(state)
    assert payload["mode"] == "night"
    assert payload["dayObs"] == 20260521
    assert payload["stats"]["nTracebacks"] == 1
    assert payload["stats"]["nDataIdsWithTraceback"] == 1
    # The first-task histogram has one value (Δshutter ≈ 5s).
    assert payload["histograms"]["firstTaskStart"]["nValues"] == 1
    assert payload["histograms"]["calcZernikesEnd"]["nValues"] == 1
    # Failures table carries our one record.
    assert len(payload["failures"]) == 1
    assert payload["failures"][0]["excClass"] == "RuntimeError"


def test_buildNightPayload_carries_pod_restarts() -> None:
    """A POD_RESTARTED lifecycle event lands in the night payload's
    ``restarts`` list and ``stats.nPodRestarts``, attributed to the dataId
    the pod was processing at the time."""
    tShutter = dt.datetime(2026, 6, 5, 2, 17, 30, tzinfo=dt.timezone.utc)
    tPickup = tShutter + dt.timedelta(seconds=30)
    tRestart = tShutter + dt.timedelta(seconds=99)
    pickupEv = parse.Event(pod="p", t=tPickup, kind="WORKER_PICKUP", level="info", expId=2026060400222)
    restartEv = parse.Event(
        pod="p",
        t=tRestart,
        kind="POD_RESTARTED",
        level="warn",
        flavor="Started",
        message="Started container run-aos-worker (restart #2)  ·  on yagan01",
    )
    summary = parse.PodSummary(
        pod="p",
        group="aos",
        instrument=None,
        ordinal=None,
        nLines=10,
        nWarn=0,
        nError=0,
        nTraceback=0,
        firstTs=tPickup,
        lastTs=tRestart,
        expIdsSeen={2026060400222},
        events=[pickupEv, restartEv],
        expIdFirstLast={2026060400222: (tPickup, tRestart)},
    )
    state = server.NightState(
        cacheDir=Path("/tmp/dummy"),
        cacheBytes=0,
        meta={},
        summaries=[summary],
        dayObs=20260604,
        startTime=dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc),
        endTime=dt.datetime(2026, 6, 5, 12, 0, tzinfo=dt.timezone.utc),
        shutterCloseByExpId={2026060400222: tShutter},
    )
    payload = server._buildNightPayload(state)
    assert payload["stats"]["nPodRestarts"] == 1
    assert len(payload["restarts"]) == 1
    row = payload["restarts"][0]
    assert row["kind"] == "POD_RESTARTED"
    assert row["dataId"] == 2026060400222
    assert row["offsetS"] == pytest.approx(99.0)


def test_buildNightPayload_counts_missing_shutter_closes() -> None:
    """``stats.nMissingShutterClose`` surfaces how many dataIds need a
    ConsDB resolve we don't have yet — the UI uses this for a "still
    resolving …" notice."""
    tPickup = dt.datetime(2026, 5, 21, 13, 0, 0, tzinfo=dt.timezone.utc)
    pickupEv = parse.Event(pod="p", t=tPickup, kind="QUANTUM_PREP", level="info", expId=999, taskLabel="isr")
    summary = parse.PodSummary(
        pod="p",
        group="aos",
        instrument=None,
        ordinal=None,
        nLines=1,
        nWarn=0,
        nError=0,
        nTraceback=0,
        firstTs=tPickup,
        lastTs=tPickup,
        expIdsSeen={999},
        events=[pickupEv],
    )
    state = server.NightState(
        cacheDir=Path("/tmp/dummy"),
        cacheBytes=0,
        meta={},
        summaries=[summary],
        dayObs=20260521,
        startTime=dt.datetime(2026, 5, 21, 12, 0, tzinfo=dt.timezone.utc),
        endTime=dt.datetime(2026, 5, 22, 12, 0, tzinfo=dt.timezone.utc),
        shutterCloseByExpId={},  # nothing resolved
    )
    payload = server._buildNightPayload(state)
    assert payload["stats"]["nMissingShutterClose"] == 1


# ----- _podDetailForNight --------------------------------------------------


def test_podDetailForNight_offsetS_is_relative_to_night_start(tmp_path: Path) -> None:
    """In night mode there's no per-pod shutter close, so the per-line
    `offsetS` is measured from noon UTC of the dayObs. A line at 13:30
    on the right day should land at 1.5h × 3600 = 5400s.
    """
    import json as _json

    cacheDir = tmp_path / "night-cache"
    podsDir = cacheDir / "pods"
    podsDir.mkdir(parents=True)
    podName = "s-lsstcam-run-aos-worker-0"
    (podsDir / f"{podName}.jsonl").write_text(
        _json.dumps(
            {
                "timestamp": "2026-05-21T13:30:00.000+00:00",
                "labels": {"detected_level": "info"},
                "line": "2026-05-21 13:30:00,000 logger fn INFO   Running pipeline for 2026052100050\n",
            }
        )
        + "\n"
    )
    state = server.NightState(
        cacheDir=cacheDir,
        cacheBytes=0,
        meta={},
        summaries=[],
        dayObs=20260521,
        startTime=dt.datetime(2026, 5, 21, 12, 0, tzinfo=dt.timezone.utc),
        endTime=dt.datetime(2026, 5, 22, 12, 0, tzinfo=dt.timezone.utc),
    )
    out = server._podDetailForNight(state, podName)
    assert out["pod"] == podName
    assert len(out["lines"]) == 1
    assert out["lines"][0]["offsetS"] == 5400.0
    # dataId attribution still works in night mode (carryover-aware).
    assert out["lines"][0]["expId"] == 2026052100050


# ----- _tracebackContextForNight error paths -----------------------------


def test_tracebackContextForNight_returns_None_for_unknown_key(tmp_path: Path) -> None:
    """A bogus bodyKey must yield None so the endpoint can return 404
    rather than crashing or returning a misleading empty body."""
    state = server.NightState(
        cacheDir=tmp_path,
        cacheBytes=0,
        meta={},
        summaries=[],
        dayObs=20260521,
        startTime=dt.datetime(2026, 5, 21, 12, 0, tzinfo=dt.timezone.utc),
        endTime=dt.datetime(2026, 5, 22, 12, 0, tzinfo=dt.timezone.utc),
    )
    assert server._tracebackContextForNight(state, "nope") is None


# ----- _resolveCacheWindow -------------------------------------------------


def test_resolveCacheWindow_rejects_components_with_path_separators(tmpCacheRoot: Path) -> None:
    """A URL segment carrying a slash or other unsafe char must fail the
    safety check at the path-component allowlist, not surface as a
    "directory not found" further down."""
    assert server._resolveCacheWindow("a/b", "ns", "slug") is None
    assert server._resolveCacheWindow("a", "..", "slug") is None
    assert server._resolveCacheWindow("a", "b", "..") is None


def test_resolveCacheWindow_rejects_podsSub_without_pods_prefix(tmpCacheRoot: Path) -> None:
    """The optional 4th segment must start with ``pods=`` so the URL
    space stays unambiguous — anything else short-circuits to None
    before any filesystem lookup."""
    # Plant a real window so the cluster/ns/slug branch is otherwise valid.
    d = tmpCacheRoot / "yagan" / "rapid-analysis" / "window-x" / "wrongprefix=__aos__"
    d.mkdir(parents=True)
    assert server._resolveCacheWindow("yagan", "rapid-analysis", "window-x", "wrongprefix=__aos__") is None


def test_resolveCacheWindow_returns_existing_dir(tmpCacheRoot: Path) -> None:
    d = tmpCacheRoot / "yagan" / "rapid-analysis" / "window-x"
    d.mkdir(parents=True)
    out = server._resolveCacheWindow("yagan", "rapid-analysis", "window-x")
    assert out == d


def test_resolveCacheWindow_returns_None_for_missing_dir(tmpCacheRoot: Path) -> None:
    """Valid component shape but the directory doesn't exist."""
    assert server._resolveCacheWindow("yagan", "rapid-analysis", "nope") is None


# ----- evictByCacheDir no-op path -----------------------------------------


def test_evictByCacheDir_no_match_is_noop(tmp_path: Path) -> None:
    """``evictByCacheDir`` must leave unrelated loaded states alone —
    we only ever evict the entries whose cacheDir matches the deleted
    target, not "everything in the dict".
    """
    ctx = _emptyCtx()
    cdA = tmp_path / "cache-A"
    cdA.mkdir()
    cdB = tmp_path / "cache-B"
    cdB.mkdir()
    cdGhost = tmp_path / "ghost"
    cdGhost.mkdir()
    ctx.putExposureState(_makeExposureState(1, cacheDir=cdA))
    ctx.putExposureState(_makeExposureState(2, cacheDir=cdB))
    ctx.evictByCacheDir(cdGhost)  # no state matches
    assert ctx.getExposureState(1) is not None
    assert ctx.getExposureState(2) is not None


# ----- _buildNightSpecFromRequest -----------------------------------------


def test_buildNightSpecFromRequest_happy_path(siteCatalog: FakeSiteCatalog) -> None:
    """A minimal valid body produces a FetchSpec with the AOS pod-regex
    pinned and the window set to the dayObs's noon-UTC bounds."""
    ctx = _ctxWithSites(siteCatalog)
    spec, site, dayObs = server._buildNightSpecFromRequest(ctx, {"dayObs": 20260521})
    assert dayObs == 20260521
    assert site.name == "summit"  # the site this server serves
    assert spec.podRegex == server.NIGHT_AOS_POD_REGEX
    # Window: noon UTC dayObs → noon UTC dayObs+1.
    assert spec.fromIso.startswith("2026-05-21T12:00:00")
    assert spec.toIso.startswith("2026-05-22T12:00:00")


def test_buildNightSpecFromRequest_ignores_a_site_in_the_body(siteCatalog: FakeSiteCatalog) -> None:
    """Which cluster gets queried is the server's, decided by where it
    runs. A body naming another site must not redirect the fetch — a
    summit deployment answering with BTS logs would be worse than an
    error, because it would look plausible."""
    ctx = _ctxWithSites(siteCatalog)
    spec, site, _ = server._buildNightSpecFromRequest(ctx, {"dayObs": 20260521, "site": "bts"})
    assert site.name == "summit"
    assert spec.cluster == "yagan"


def test_buildNightSpecFromRequest_rejects_missing_dayObs(siteCatalog: FakeSiteCatalog) -> None:
    ctx = _ctxWithSites(siteCatalog)
    with pytest.raises(ValueError, match="dayObs"):
        server._buildNightSpecFromRequest(ctx, {})


def test_buildNightSpecFromRequest_rejects_non_integer_dayObs(siteCatalog: FakeSiteCatalog) -> None:
    ctx = _ctxWithSites(siteCatalog)
    with pytest.raises(ValueError, match="YYYYMMDD"):
        server._buildNightSpecFromRequest(ctx, {"dayObs": "tomorrow"})


def test_buildNightSpecFromRequest_rejects_out_of_range_dayObs(siteCatalog: FakeSiteCatalog) -> None:
    """A YYYYMMDD outside the [1900, 3000] year band almost certainly
    means the caller passed something that isn't a dayObs — surface
    that as a 400 rather than letting a nonsense window go to Loki."""
    ctx = _ctxWithSites(siteCatalog)
    with pytest.raises(ValueError, match="YYYYMMDD"):
        server._buildNightSpecFromRequest(ctx, {"dayObs": 12345})


def test_buildNightSpecFromRequest_ignores_credentials_in_the_body(siteCatalog: FakeSiteCatalog) -> None:
    """Credentials are the service's, from its environment. A body that
    supplies its own must be ignored rather than honoured: the process
    env is shared, so one browser's wrong password would otherwise break
    fetches for everyone using the deployment."""
    ctx = _ctxWithSites(siteCatalog)
    spec, _, _ = server._buildNightSpecFromRequest(
        ctx, {"dayObs": 20260521, "password": "hunter2", "username": "someone-else", "workers": 999}
    )
    assert spec.username == server.DEFAULT_USERNAME
    assert spec.workers == server.DEFAULT_WORKERS


# ----- _buildRangeSpecFromRequest -----------------------------------------


def _rangeBody(**overrides: object) -> dict:
    body: dict = {
        "rangeStart": 2026051900722,
        "rangeStop": 2026051900750,
        "tZeroStart": "2026-05-20T08:46:16.267",
        "tZeroStop": "2026-05-20T08:51:09.512",
    }
    body.update(overrides)
    return body


def test_buildRangeSpecFromRequest_happy_path(siteCatalog: FakeSiteCatalog) -> None:
    """A valid body produces an all-pods spec whose window spans from the
    start anchor (minus the before-buffer) to the stop anchor (plus the
    after-buffer), with the TAI→UTC conversion applied to both anchors."""
    ctx = _ctxWithSites(siteCatalog)
    spec, site, startId, stopId, tZeroStart, tZeroStop, _ = server._buildRangeSpecFromRequest(
        ctx, _rangeBody()
    )
    assert (startId, stopId) == (2026051900722, 2026051900750)
    assert site.name == "summit"
    assert spec.podRegex is None  # range is an all-pods fetch
    # TAI inputs minus 37 s; default windowBefore=5, windowAfter=300.
    # start 08:46:16.267 TAI -> 08:45:39.267 UTC -> minus 5 s = 08:45:34.267.
    assert spec.fromIso.startswith("2026-05-20T08:45:34.267")
    # stop 08:51:09.512 TAI -> 08:50:32.512 UTC -> plus 300 s = 08:55:32.512.
    assert spec.toIso.startswith("2026-05-20T08:55:32.512")
    # The returned anchors are the UTC shutter closes (window-defining).
    assert tZeroStart == dt.datetime(2026, 5, 20, 8, 45, 39, 267000, tzinfo=dt.timezone.utc)
    assert tZeroStop == dt.datetime(2026, 5, 20, 8, 50, 32, 512000, tzinfo=dt.timezone.utc)


def test_buildRangeSpecFromRequest_tZeroUtc_skips_conversion(siteCatalog: FakeSiteCatalog) -> None:
    ctx = _ctxWithSites(siteCatalog)
    _, _, _, _, tZeroStart, _, _ = server._buildRangeSpecFromRequest(ctx, _rangeBody(tZeroUtc=True))
    assert tZeroStart == dt.datetime(2026, 5, 20, 8, 46, 16, 267000, tzinfo=dt.timezone.utc)


def test_buildRangeSpecFromRequest_ignores_a_site_in_the_body(siteCatalog: FakeSiteCatalog) -> None:
    ctx = _ctxWithSites(siteCatalog)
    _, site, *_, _ = server._buildRangeSpecFromRequest(ctx, _rangeBody(site="bts"))
    assert site.name == "summit"


def test_buildRangeSpecFromRequest_rejects_reversed_range(siteCatalog: FakeSiteCatalog) -> None:
    ctx = _ctxWithSites(siteCatalog)
    with pytest.raises(ValueError, match="greater than"):
        server._buildRangeSpecFromRequest(ctx, _rangeBody(rangeStart=2026051900750, rangeStop=2026051900722))


def test_buildRangeSpecFromRequest_rejects_oversize_span(siteCatalog: FakeSiteCatalog) -> None:
    ctx = _ctxWithSites(siteCatalog)
    start = 2026051900722
    with pytest.raises(ValueError, match="exceeds"):
        server._buildRangeSpecFromRequest(
            ctx, _rangeBody(rangeStart=start, rangeStop=start + config.MAX_RANGE_SPAN + 1)
        )


def test_buildRangeSpecFromRequest_rejects_missing_anchor(siteCatalog: FakeSiteCatalog) -> None:
    ctx = _ctxWithSites(siteCatalog)
    body = _rangeBody()
    del body["tZeroStart"]
    with pytest.raises(ValueError, match="tZeroStart"):
        server._buildRangeSpecFromRequest(ctx, body)


def test_buildRangeSpecFromRequest_ignores_credentials_in_the_body(siteCatalog: FakeSiteCatalog) -> None:
    ctx = _ctxWithSites(siteCatalog)
    spec, *_, _ = server._buildRangeSpecFromRequest(ctx, _rangeBody(password="hunter2", workers=999))
    assert spec.username == server.DEFAULT_USERNAME
    assert spec.workers == server.DEFAULT_WORKERS


def test_buildRangeSpecFromRequest_null_window_falls_back_to_default(siteCatalog: FakeSiteCatalog) -> None:
    """An empty browser number input serializes to JSON null; it must fall
    back to the default window, not crash on ``float(None)`` (which the
    handler doesn't catch — it would drop the connection with no
    response)."""
    ctx = _ctxWithSites(siteCatalog)
    spec, *_, _ = server._buildRangeSpecFromRequest(ctx, _rangeBody(windowBefore=None, windowAfter=None))
    # Defaults: start 08:45:39.267 − 5 s, stop 08:50:32.512 + 300 s.
    assert spec.fromIso.startswith("2026-05-20T08:45:34.267")
    assert spec.toIso.startswith("2026-05-20T08:55:32.512")


def test_buildRangeSpecFromRequest_zero_window_is_preserved(siteCatalog: FakeSiteCatalog) -> None:
    """``0`` is a legitimate window (start exactly at the shutter close)
    and must not be coerced to the default."""
    ctx = _ctxWithSites(siteCatalog)
    spec, *_, _ = server._buildRangeSpecFromRequest(ctx, _rangeBody(windowBefore=0))
    assert spec.fromIso.startswith("2026-05-20T08:45:39.267")


def test_buildSpecFromRequest_null_window_falls_back_to_default(siteCatalog: FakeSiteCatalog) -> None:
    """Same null-window robustness for the single-exposure endpoint, which
    shares the window-parsing helper."""
    ctx = _ctxWithSites(siteCatalog)
    spec, *_ = server._buildSpecFromRequest(
        ctx, {"exposureId": 1, "tZero": "2026-05-20T08:46:16.267", "windowBefore": None}
    )
    # 08:46:16.267 TAI − 37 s − default 5 s = 08:45:34.267.
    assert spec.fromIso.startswith("2026-05-20T08:45:34.267")


# ----- range payloads -----------------------------------------------------


def _rangeStateForPayload() -> server.RangeState:
    """A RangeState over [722, 725] where 724 is a skipped integer, 725
    resolved a shutter close but produced no logs, and 723 has a traceback."""
    tb = parse.TracebackRecord(
        pod="p",
        t=dt.datetime(2026, 5, 20, 8, 46, tzinfo=dt.timezone.utc),
        expId=2026051900723,
        excClass="RuntimeError",
        excMessage="boom",
        body="Traceback ...",
    )
    summary = parse.PodSummary(
        pod="p",
        group="aos",
        instrument=None,
        ordinal=None,
        nLines=1,
        nWarn=0,
        nError=1,
        nTraceback=1,
        firstTs=None,
        lastTs=None,
        expIdsSeen={2026051900722, 2026051900723},
        events=[],
        tracebacks=[tb],
    )
    state = server.RangeState(
        cacheDir=Path("/tmp/range"),
        cacheBytes=0,
        meta={},
        summaries=[summary],
        startId=2026051900722,
        stopId=2026051900725,
        fromTime=dt.datetime(2026, 5, 20, 8, 45, tzinfo=dt.timezone.utc),
        toTime=dt.datetime(2026, 5, 20, 8, 51, tzinfo=dt.timezone.utc),
    )
    base = dt.datetime(2026, 5, 20, 8, 45, 39, tzinfo=dt.timezone.utc)
    state.shutterCloseByExpId = {
        2026051900722: base,
        2026051900723: base + dt.timedelta(seconds=30),
        2026051900725: base + dt.timedelta(seconds=90),  # resolved but no logs
    }
    state.exposureInfoByExpId = {
        2026051900722: {"obs_end": base.isoformat(), "img_type": "science", "physical_filter": "z_20"},
    }
    return state


def test_buildRangePayload_lists_resolved_dataIds_with_overview() -> None:
    payload = server._buildRangePayload(_rangeStateForPayload())
    assert payload["mode"] == "range"
    assert payload["startId"] == 2026051900722 and payload["stopId"] == 2026051900725
    # 4 candidate ids (722..725); 724 has no resolved shutter close.
    assert payload["nMissing"] == 1
    ids = payload["dataIds"]
    assert [d["expId"] for d in ids] == [2026051900722, 2026051900723, 2026051900725]
    byId = {d["expId"]: d for d in ids}
    assert byId[2026051900723]["nTraceback"] == 1
    assert byId[2026051900722]["hasLogs"] is True
    assert byId[2026051900725]["hasLogs"] is False  # resolved but produced no logs
    # The curated ConsDB record rides along per dataId for the chip tooltip
    # (only 722 was resolved here; the others carry None).
    assert byId[2026051900722]["exposure"]["img_type"] == "science"
    assert byId[2026051900723]["exposure"] is None


def test_buildRangeExposurePayload_carries_exposure_record() -> None:
    state = _rangeStateForPayload()
    payload = server._buildRangeExposurePayload(state, 2026051900722)
    assert payload is not None
    assert payload["exposure"]["physical_filter"] == "z_20"


def test_buildRangeExposurePayload_reuses_exposure_shape_with_query() -> None:
    state = _rangeStateForPayload()
    payload = server._buildRangeExposurePayload(state, 2026051900722)
    assert payload is not None
    assert payload["mode"] == "range-exposure"
    assert payload["expId"] == 2026051900722
    assert payload["podDetailQuery"] == (
        "rangeStart=2026051900722&rangeStop=2026051900725&dataId=2026051900722"
    )
    assert "pods" in payload  # the reused exposure-payload body


def test_buildRangeExposurePayload_returns_None_for_skipped_id() -> None:
    state = _rangeStateForPayload()
    assert server._buildRangeExposurePayload(state, 2026051900724) is None


def test_buildSummaryPayload_carries_exposure_record() -> None:
    rec = {"obs_end": "2026-05-20T08:45:39.000", "img_type": "science", "physical_filter": "z_20"}
    state = server.ServerState(
        cacheDir=Path("/tmp/x"),
        cacheBytes=0,
        meta={},
        summaries=[],
        expId=2026051900722,
        tZero=dt.datetime(2026, 5, 20, 8, 45, 39, tzinfo=dt.timezone.utc),
        exposureInfo=rec,
    )
    payload = server._buildSummaryPayload(state)
    assert payload["exposure"] == rec


def test_buildSummaryPayload_exposure_is_None_when_unresolved() -> None:
    state = server.ServerState(
        cacheDir=Path("/tmp/x"),
        cacheBytes=0,
        meta={},
        summaries=[],
        expId=2026051900722,
        tZero=dt.datetime(2026, 5, 20, 8, 45, 39, tzinfo=dt.timezone.utc),
    )
    assert server._buildSummaryPayload(state)["exposure"] is None


def test_buildNightPayload_exposes_exposureInfo_map_keyed_by_string() -> None:
    rec = {"obs_end": "2026-05-21T13:00:00.000", "img_type": "science"}
    state = server.NightState(
        cacheDir=Path("/tmp/n"),
        cacheBytes=0,
        meta={},
        summaries=[],
        dayObs=20260521,
        startTime=dt.datetime(2026, 5, 21, 12, 0, tzinfo=dt.timezone.utc),
        endTime=dt.datetime(2026, 5, 22, 12, 0, tzinfo=dt.timezone.utc),
    )
    state.exposureInfoByExpId = {2026052100051: rec}
    payload = server._buildNightPayload(state)
    # JSON object keys must be strings, so the map is keyed by str(expId).
    assert payload["exposureInfo"] == {"2026052100051": rec}


# ----- _parseClientIso ----------------------------------------------------


def test_parseClientIso_accepts_Z_suffix() -> None:
    out = server._parseClientIso("2026-05-20T08:45:39Z")
    assert out == dt.datetime(2026, 5, 20, 8, 45, 39, tzinfo=dt.timezone.utc)


def test_parseClientIso_assumes_utc_when_no_offset() -> None:
    out = server._parseClientIso("2026-05-20T08:45:39")
    assert out == dt.datetime(2026, 5, 20, 8, 45, 39, tzinfo=dt.timezone.utc)


def test_parseClientIso_honours_explicit_negative_offset() -> None:
    """Negative offset path — important because the regex check uses
    ``"-" in s[10:]`` to detect tz offsets after the YYYY-MM-DD head."""
    out = server._parseClientIso("2026-05-20T05:45:39-03:00")
    # 05:45 UTC-3 == 08:45 UTC
    assert out == dt.datetime(2026, 5, 20, 8, 45, 39, tzinfo=dt.timezone.utc)


def test_parseClientIso_raises_ValueError_on_garbage() -> None:
    with pytest.raises(ValueError):
        server._parseClientIso("not a date")


# ----- _isoForLogcli (server.py copy) -------------------------------------


def test_isoForLogcli_emits_Z_suffix_and_utc() -> None:
    """logcli wants RFC3339Nano UTC; the helper must (a) end with Z
    (not ``+00:00``) and (b) convert any non-UTC input to UTC."""
    t = dt.datetime(2026, 5, 20, 9, 45, 39, tzinfo=dt.timezone(dt.timedelta(hours=1)))
    s = server._isoForLogcli(t)
    assert s.endswith("Z")
    assert "08:45:39" in s  # the +01:00 input projected to UTC


# ----- _resolveShutterClosesInto (night / range prefetch) -----------------


def _prefetchJob(siteCatalog: FakeSiteCatalog) -> tuple[FetchJob, sites.Site]:
    """A night FetchJob + the summit Site, for driving the prefetch path."""
    spec = config.FetchSpec(
        cluster="yagan",
        namespace="rapid-analysis",
        fromIso="2026-05-20T08:00:00Z",
        toIso="2026-05-20T09:00:00Z",
        username="u",
        lokiAddr="https://loki.example",
    )
    job = JobManager().createNightJob(spec, 20260520, siteName="summit")
    site = next(s for s in siteCatalog.catalog if s.name == "summit")
    return job, site


def _phase(job: FetchJob, name: str) -> dict[str, Any]:
    """The single progress event with ``phase == name``."""
    return next(e for e in job.events if e.get("phase") == name)


def test_resolveShutterCloses_requeries_manual_standins(
    tmpCacheRoot: Path, siteCatalog: FakeSiteCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `_manual` stand-in anchors its id *and* still joins the ConsDB
    batch, so a real row supersedes it. Treating it as an ordinary cache
    hit would freeze one hand-typed value into every future night/range
    view of that dataId — silently biasing every Δshutter derived from it.
    """
    exposureTimes.storeCachedRecord(
        2026052000001, exposureTimes.manualRecord("2026-05-20T00:00:00.000000"), siteName="summit"
    )
    siteCatalog.writeSummitToken()
    queried: list[list[int]] = []

    def fakeBatch(
        dataIds: Iterable[int],
        token: str,
        *,
        consdbUrl: str,
        chunkSize: int = 500,
        instrument: str | None = None,
    ) -> dict[int, exposureTimes.ExposureRecord]:
        queried.append(sorted(dataIds))
        return {2026052000001: {"obs_end": "2026-05-20T08:46:16.267000", "physical_filter": "r"}}

    monkeypatch.setattr(exposureTimes, "queryExposureRecordBatch", fakeBatch)
    job, site = _prefetchJob(siteCatalog)
    target: dict[int, dt.datetime] = {}
    info: dict[int, exposureTimes.ExposureRecord] = {}
    server._resolveShutterClosesInto({2026052000001}, target, info, job, site)

    assert queried == [[2026052000001]]  # the stand-in was re-queried
    # ConsDB's value won, in memory and on disk.
    assert target[2026052000001] == server._taiIsoToUtc("2026-05-20T08:46:16.267000")
    assert info[2026052000001]["physical_filter"] == "r"
    stored = exposureTimes.lookupCachedRecord(2026052000001, siteName="summit")
    assert exposureTimes.isManual(stored) is False
    checked = _phase(job, "cache-checked")
    assert checked["cacheHits"] == 0 and checked["manualStandins"] == 1
    assert checked["remaining"] == 0  # anchored provisionally, so not "missing"
    assert _phase(job, "done")["stillMissing"] == 0


def test_resolveShutterCloses_keeps_manual_when_consdb_still_cannot_answer(
    tmpCacheRoot: Path, siteCatalog: FakeSiteCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stand-in is a *fallback*: when the re-query comes back empty it
    stays put, and the id is reported anchored rather than missing."""
    exposureTimes.storeCachedRecord(
        2026052000001, exposureTimes.manualRecord("2026-06-24T14:38:41.380663"), siteName="summit"
    )
    siteCatalog.writeSummitToken()

    def emptyBatch(
        dataIds: Iterable[int],
        token: str,
        *,
        consdbUrl: str,
        chunkSize: int = 500,
        instrument: str | None = None,
    ) -> dict[int, exposureTimes.ExposureRecord]:
        return {}

    monkeypatch.setattr(exposureTimes, "queryExposureRecordBatch", emptyBatch)
    job, site = _prefetchJob(siteCatalog)
    target: dict[int, dt.datetime] = {}
    info: dict[int, exposureTimes.ExposureRecord] = {}
    server._resolveShutterClosesInto({2026052000001}, target, info, job, site)

    assert target[2026052000001] == server._taiIsoToUtc("2026-06-24T14:38:41.380663")
    stored = exposureTimes.lookupCachedRecord(2026052000001, siteName="summit")
    assert exposureTimes.isManual(stored) is True
    assert _phase(job, "done")["stillMissing"] == 0


def test_resolveShutterCloses_manual_survives_a_missing_token(
    tmpCacheRoot: Path, siteCatalog: FakeSiteCatalog
) -> None:
    """No token means no re-query at all — the stand-in still anchors the
    id, and `remaining` counts only the genuinely unresolvable one."""
    exposureTimes.storeCachedRecord(
        2026052000001, exposureTimes.manualRecord("2026-06-24T14:38:41.380663"), siteName="summit"
    )
    # Token deliberately absent.
    job, site = _prefetchJob(siteCatalog)
    target: dict[int, dt.datetime] = {}
    info: dict[int, exposureTimes.ExposureRecord] = {}
    server._resolveShutterClosesInto({2026052000001, 2026052000002}, target, info, job, site)

    assert target[2026052000001] == server._taiIsoToUtc("2026-06-24T14:38:41.380663")
    assert 2026052000002 not in target
    assert _phase(job, "no-token")["remaining"] == 1  # only the un-anchored id


def test_resolveShutterCloses_real_cache_hit_skips_consdb(
    tmpCacheRoot: Path, siteCatalog: FakeSiteCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flip side: a genuine ConsDB-sourced record *is* immutable truth,
    so it must still short-circuit the batch query entirely."""
    exposureTimes.storeCachedRecord(
        2026052000001, {"obs_end": "2026-05-20T08:46:16.267000"}, siteName="summit"
    )
    siteCatalog.writeSummitToken()

    def boom(
        dataIds: Iterable[int], token: str, *, consdbUrl: str, chunkSize: int = 500
    ) -> dict[int, exposureTimes.ExposureRecord]:
        raise AssertionError("a real cache hit must not hit ConsDB")

    monkeypatch.setattr(exposureTimes, "queryExposureRecordBatch", boom)
    job, site = _prefetchJob(siteCatalog)
    target: dict[int, dt.datetime] = {}
    info: dict[int, exposureTimes.ExposureRecord] = {}
    server._resolveShutterClosesInto({2026052000001}, target, info, job, site)

    assert target[2026052000001] == server._taiIsoToUtc("2026-05-20T08:46:16.267000")
    checked = _phase(job, "cache-checked")
    assert checked["cacheHits"] == 1 and checked["manualStandins"] == 0


# ----- the served site -----------------------------------------------------


def test_ctx_site_returns_the_configured_site(siteCatalog: FakeSiteCatalog) -> None:
    ctx = _ctxWithSites(siteCatalog)
    assert ctx.site().name == "summit"
    # A process serves whichever entry it was started with, not the
    # catalog's default: `--site bts` locally means BTS for the whole run.
    ctx.siteName = "bts"
    assert ctx.site().name == "bts"
    assert ctx.site().cluster == "manke"


def test_ctx_site_raises_for_a_name_not_in_the_catalog(siteCatalog: FakeSiteCatalog) -> None:
    """Fail loudly rather than falling back to an arbitrary entry. Serving
    the wrong observatory's logs is worse than serving none, because the
    same dataId exists at both and the answer would look plausible."""
    ctx = _ctxWithSites(siteCatalog)
    ctx.siteName = "ghost"
    with pytest.raises(sites.SitesConfigError):
        ctx.site()


def test_trimFloat_drops_a_pointless_decimal() -> None:
    """These land in an HTML number field. "300" reads as the default it
    is; "300.0" reads as a value somebody has already fiddled with."""
    assert server._trimFloat(300.0) == "300"
    assert server._trimFloat(5.0) == "5"
    assert server._trimFloat(0.0) == "0"
    assert server._trimFloat(12.5) == "12.5"
    assert server._trimFloat(0.1) == "0.1"


# ----- instrument threading -------------------------------------------------


def test_instrumentFromBody_defaults_to_lsstcam() -> None:
    """A body that doesn't say is an LSSTCam fetch — the default
    instrument is always LSSTCam."""
    assert server._instrumentFromBody({}) == "lsstcam"
    assert server._instrumentFromBody({"instrument": ""}) == "lsstcam"
    assert server._instrumentFromBody({"instrument": None}) == "lsstcam"


def test_instrumentFromBody_normalises_and_validates() -> None:
    assert server._instrumentFromBody({"instrument": "LATISS"}) == "latiss"
    with pytest.raises(ValueError):
        server._instrumentFromBody({"instrument": "hubble"})
    with pytest.raises(ValueError):
        server._instrumentFromBody({"instrument": 7})


def test_podBelongsToInstrument_rules() -> None:
    """Cross-instrument pods never belong; neutral pods always do; an
    unknown side keeps the pod rather than hiding work."""
    assert server._podBelongsToInstrument("LSSTCam", "lsstcam") is True
    assert server._podBelongsToInstrument("LATISS", "lsstcam") is False
    assert server._podBelongsToInstrument(None, "lsstcam") is True  # redis, squid, misc
    assert server._podBelongsToInstrument("LSSTCam", None) is True
    assert server._podBelongsToInstrument(None, None) is True


def _instrumentSummary(pod: str, instrument: str | None, expId: int) -> parse.PodSummary:
    return parse.PodSummary(
        pod=pod,
        group="sfm",
        instrument=instrument,
        ordinal=None,
        nLines=1,
        nWarn=0,
        nError=0,
        nTraceback=0,
        firstTs=None,
        lastTs=None,
        expIdsSeen={expId},
        events=[],
    )


def test_buildSummaryPayload_filters_pods_by_instrument(tmp_path: Path) -> None:
    """A LATISS view of id N must not attribute an LSSTCam pod's work —
    the same bare id names a different exposure on each instrument."""
    expId = 2026071100470
    tZero = dt.datetime(2026, 7, 12, 5, 42, 20, tzinfo=dt.timezone.utc)
    state = server.ServerState(
        cacheDir=tmp_path,
        cacheBytes=0,
        meta={},
        summaries=[
            _instrumentSummary("s-latiss-run-sfm-runner-1", "LATISS", expId),
            _instrumentSummary("s-lsstcam-run-sfm-runner-1", "LSSTCam", expId),
            _instrumentSummary("redis-0", None, expId),
        ],
        expId=expId,
        tZero=tZero,
        siteName="summit",
        instrument="latiss",
    )
    payload = server._buildSummaryPayload(state)
    assert payload["instrument"] == "latiss"
    assert {p["pod"] for p in payload["pods"]} == {"s-latiss-run-sfm-runner-1", "redis-0"}
    assert {p["pod"] for p in payload["podsAll"]} == {"s-latiss-run-sfm-runner-1", "redis-0"}


def test_buildSummaryPayload_without_instrument_keeps_every_pod(tmp_path: Path) -> None:
    """A state built before the instrument was known filters nothing —
    hiding work on an unknown pin would be worse than showing extra."""
    expId = 2026071100470
    state = server.ServerState(
        cacheDir=tmp_path,
        cacheBytes=0,
        meta={},
        summaries=[
            _instrumentSummary("s-latiss-run-sfm-runner-1", "LATISS", expId),
            _instrumentSummary("s-lsstcam-run-sfm-runner-1", "LSSTCam", expId),
        ],
        expId=expId,
        tZero=dt.datetime(2026, 7, 12, 5, 42, 20, tzinfo=dt.timezone.utc),
        siteName="summit",
    )
    payload = server._buildSummaryPayload(state)
    assert len(payload["pods"]) == 2


# ----- instrument pins through the resolution paths -------------------------


def _captureResolvePin(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace _resolveShutterClosesInto with a capture of its pin."""
    captured: dict = {}

    def fakeResolve(
        needIds: set[int],
        target: dict,
        infoTarget: dict,
        job: FetchJob,
        site: sites.Site,
        instrument: str | None = None,
    ) -> None:
        captured["needIds"] = set(needIds)
        captured["instrument"] = instrument

    monkeypatch.setattr(server, "_resolveShutterClosesInto", fakeResolve)
    return captured


def _tracebackSummary(expId: int) -> parse.PodSummary:
    s = _instrumentSummary("s-lsstcam-run-aos-worker-1", "LSSTCam", expId)
    s.tracebacks = [
        parse.TracebackRecord(
            pod=s.pod,
            t=dt.datetime(2026, 7, 12, 3, 0, tzinfo=dt.timezone.utc),
            expId=expId,
            excClass="RuntimeError",
            excMessage="boom",
            body="Traceback ...",
            reachedTerminator=True,
        )
    ]
    return s


def test_prefetchNightShutterCloses_pins_lsstcam(
    siteCatalog: FakeSiteCatalog, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AOS runs on LSSTCam only, so every dataId in a night's logs is an
    LSSTCam id — the resolution must never wander to another table."""
    captured = _captureResolvePin(monkeypatch)
    state = server.NightState(
        cacheDir=tmp_path,
        cacheBytes=0,
        meta={},
        summaries=[],
        dayObs=20260711,
        startTime=dt.datetime(2026, 7, 11, 12, tzinfo=dt.timezone.utc),
        endTime=dt.datetime(2026, 7, 12, 12, tzinfo=dt.timezone.utc),
    )
    job, site = _prefetchJob(siteCatalog)
    server._prefetchNightShutterCloses(state, [_tracebackSummary(2026071100050)], job, site)
    assert captured["needIds"] == {2026071100050}
    assert captured["instrument"] == "lsstcam"


def test_prefetchRangeShutterCloses_pins_the_jobs_instrument(
    siteCatalog: FakeSiteCatalog, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A range is a run of ONE instrument's exposures; its server-side
    batch must resolve against that instrument's table only."""
    captured = _captureResolvePin(monkeypatch)
    t0 = dt.datetime(2026, 7, 12, 3, 0, tzinfo=dt.timezone.utc)
    state = server.RangeState(
        cacheDir=tmp_path,
        cacheBytes=0,
        meta={},
        summaries=[],
        startId=2026071100010,
        stopId=2026071100012,
        fromTime=t0,
        toTime=t0,
        instrument="latiss",
    )
    nightJob, site = _prefetchJob(siteCatalog)
    job = JobManager().createRangeJob(
        nightJob.spec, 2026071100010, 2026071100012, t0, t0, siteName="summit", instrument="latiss"
    )
    server._prefetchRangeShutterCloses(state, job, site)
    assert captured["instrument"] == "latiss"
    assert captured["needIds"] == {2026071100010, 2026071100011, 2026071100012}


def test_resolveShutterCloses_pin_reaches_cache_and_batch(
    siteCatalog: FakeSiteCatalog, tmpCacheRoot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pin governs BOTH halves of resolution: a colliding id already
    cached for both instruments must anchor to the pinned instrument's
    shutter close, and the ConsDB batch for misses must carry the pin."""
    collidingId = 2026071100408
    exposureTimes.storeCachedRecords(
        {collidingId: {"obs_end": "2026-07-12T03:58:52.091000", "instrument": "lsstcam"}},
        siteName="summit",
    )
    exposureTimes.storeCachedRecords(
        {collidingId: {"obs_end": "2026-07-12T04:58:03.354000", "instrument": "latiss"}},
        siteName="summit",
    )
    batchPins: list[str | None] = []

    def fakeBatch(
        dataIds: Iterable[int],
        token: str,
        *,
        consdbUrl: str,
        chunkSize: int = 500,
        instrument: str | None = None,
    ) -> dict[int, exposureTimes.ExposureRecord]:
        batchPins.append(instrument)
        return {}

    monkeypatch.setattr(exposureTimes, "queryExposureRecordBatch", fakeBatch)
    siteCatalog.writeSummitToken()

    target: dict[int, dt.datetime] = {}
    info: dict[int, exposureTimes.ExposureRecord] = {}
    job, site = _prefetchJob(siteCatalog)
    missId = 2026071100999  # not cached -> goes to the batch
    server._resolveShutterClosesInto({collidingId, missId}, target, info, job, site, instrument="latiss")

    # The cached hit anchored to the LATISS shutter close (TAI -37 s).
    assert target[collidingId] == dt.datetime(2026, 7, 12, 4, 57, 26, 354000, tzinfo=dt.timezone.utc)
    assert info[collidingId]["instrument"] == "latiss"
    assert batchPins == ["latiss"]


# ----- instrument on the cache-rebuild path ---------------------------------


def _plantExposureCache(root: Path, expId: int, fromIso: str, toIso: str, cluster: str = "yagan") -> Path:
    import json as _json

    windowDir = root / cluster / "rapid-analysis" / "window-a"
    (windowDir / "pods").mkdir(parents=True)
    (windowDir / "_meta.json").write_text(
        _json.dumps(
            {
                "spec": {"fromIso": fromIso, "toIso": toIso},
                "fetched_at": "2026-07-12T06:00:00+00:00",
                "pod_count": 0,
            }
        )
    )
    (windowDir / "_exposure_ids.txt").write_text(f"{expId}\n")
    return windowDir


def test_loadExposureFromCache_refuses_a_window_that_misses_the_pinned_t0(
    siteCatalog: FakeSiteCatalog, tmpCacheRoot: Path
) -> None:
    """_exposure_ids.txt lists bare ids, so a colliding id can name the
    OTHER instrument's window. The pinned t0 must fall inside the found
    window, or the rebuild would dress the wrong logs up as this
    exposure."""
    collidingId = 2026071100408
    # An LSSTCam window around its shutter close in UTC (obs_end is
    # TAI; -37 s puts t0 at 03:58:15.091 UTC).
    _plantExposureCache(
        tmpCacheRoot, collidingId, "2026-07-12T03:58:10.091000Z", "2026-07-12T04:03:15.091000Z"
    )
    site = siteCatalog.catalog[0]
    exposureTimes.storeCachedRecords(
        {collidingId: {"obs_end": "2026-07-12T03:58:52.091000", "instrument": "lsstcam"}},
        siteName=site.name,
    )
    exposureTimes.storeCachedRecords(
        {collidingId: {"obs_end": "2026-07-12T04:58:03.354000", "instrument": "latiss"}},
        siteName=site.name,
    )
    ctx = _ctxWithSites(siteCatalog)
    # LATISS pin: its 04:58 t0 is outside the window on disk -> refuse.
    assert server._loadExposureFromCache(ctx, collidingId, instrument="latiss") is None
    # LSSTCam pin: t0 inside the window -> rebuilt, stamped with the pin.
    state = server._loadExposureFromCache(ctx, collidingId, instrument="lsstcam")
    assert state is not None
    assert state.instrument == "lsstcam"
    assert state.tZero == dt.datetime(2026, 7, 12, 3, 58, 15, 91000, tzinfo=dt.timezone.utc)


# ----- instrument through the range per-exposure view -----------------------


def test_rangeExposurePayload_filters_pods_by_the_ranges_instrument(tmp_path: Path) -> None:
    expId = 2026071100010
    t0 = dt.datetime(2026, 7, 12, 3, 0, tzinfo=dt.timezone.utc)
    state = server.RangeState(
        cacheDir=tmp_path,
        cacheBytes=0,
        meta={},
        summaries=[
            _instrumentSummary("s-latiss-run-sfm-runner-1", "LATISS", expId),
            _instrumentSummary("s-lsstcam-run-sfm-runner-1", "LSSTCam", expId),
        ],
        startId=expId,
        stopId=expId + 2,
        fromTime=t0,
        toTime=t0,
        instrument="latiss",
    )
    state.shutterCloseByExpId[expId] = t0
    payload = server._buildRangeExposurePayload(state, expId)
    assert payload is not None
    assert payload["instrument"] == "latiss"
    assert {p["pod"] for p in payload["pods"]} == {"s-latiss-run-sfm-runner-1"}


def test_exposure_cache_lookup_picks_the_window_holding_this_t_zero(
    tmpCacheRoot: Path, monkeypatch: pytest.MonkeyPatch, siteCatalog: FakeSiteCatalog
) -> None:
    """`_exposure_ids.txt` records bare ids, and a bare id is not unique:
    on a night both instruments observe, the same id names an exposure on
    each and both windows can be cached at once. Taking the newest and
    giving up if it doesn't fit made whichever deep link was opened
    second break the first — and re-fetching to repair it only swapped
    which one was broken."""
    ctx = ServerContext(jobs=JobManager(), sites=siteCatalog.catalog, siteName=siteCatalog.defaultName)
    expId = 2026071100445
    windows = {
        "lsstcam": (
            "2026-07-12T04:21:17.502000Z",
            "2026-07-12T04:26:22.502000Z",
            "2026-07-12T04:21:59.502000",
        ),
        "latiss": (
            "2026-07-12T05:24:48.895000Z",
            "2026-07-12T05:29:53.895000Z",
            "2026-07-12T05:25:30.895000",
        ),
    }
    for i, (instrument, (fromIso, toIso, obsEnd)) in enumerate(windows.items()):
        d = tmpCacheRoot / "yagan" / "rapid-analysis" / f"{fromIso}__{toIso}".replace(":", "")
        (d / "pods").mkdir(parents=True)
        (d / "_meta.json").write_text(
            json.dumps(
                {
                    "spec": {"fromIso": fromIso, "toIso": toIso, "podRegex": None},
                    "fetchSchemaVersion": fetchModule.CACHE_SCHEMA_VERSION,
                    # The LATISS window is the more recently fetched one.
                    "fetched_at": f"2026-07-12T0{i}:00:00+00:00",
                }
            )
        )
        (d / "_exposure_ids.txt").write_text(f"{expId}\n")
        exposureTimes.storeCachedRecord(
            expId,
            {"exposure_id": expId, "obs_end": obsEnd, "instrument": instrument},
            siteName="summit",
            bareKey=(instrument == "lsstcam"),
        )

    for instrument, (fromIso, _toIso, _obsEnd) in windows.items():
        state = _loadExposureFromCache(ctx, expId, instrument=instrument)
        assert state is not None, f"{instrument} deep link fell back to a re-fetch"
        assert state.cacheDir.name.startswith(fromIso.replace(":", "")[:17])
        assert state.instrument == instrument
