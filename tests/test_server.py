"""Tests for `ra_log_explorer.server`."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import pytest

from ra_log_explorer import parse, server

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


def test_summaryToDict_keeps_all_untagged_when_no_targeted_events() -> None:
    # A pod with no explicitly-tagged events still gets its untagged
    # warnings shown (e.g. head-node lines that don't mention an expId).
    tZero = dt.datetime(2026, 5, 20, 8, 45, 39, tzinfo=dt.timezone.utc)
    events = [
        _ev(tZero - dt.timedelta(seconds=60), "WARN", level="warn"),
        _ev(tZero + dt.timedelta(seconds=60), "WARN", level="warn"),
    ]
    out = server._summaryToDict(_stubSummary(events), tZero, 2026051900722)
    assert len(out["events"]) == 2


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


def test_buildNightSpecFromRequest_happy_path() -> None:
    """A minimal valid body produces a FetchSpec with the AOS pod-regex
    pinned and the window set to the dayObs's noon-UTC bounds."""
    spec, dayObs, password = server._buildNightSpecFromRequest({"dayObs": 20260521})
    assert dayObs == 20260521
    assert password is None
    assert spec.podRegex == server.NIGHT_AOS_POD_REGEX
    # Window: noon UTC dayObs → noon UTC dayObs+1.
    assert spec.fromIso.startswith("2026-05-21T12:00:00")
    assert spec.toIso.startswith("2026-05-22T12:00:00")


def test_buildNightSpecFromRequest_rejects_missing_dayObs() -> None:
    with pytest.raises(ValueError, match="dayObs"):
        server._buildNightSpecFromRequest({})


def test_buildNightSpecFromRequest_rejects_non_integer_dayObs() -> None:
    with pytest.raises(ValueError, match="YYYYMMDD"):
        server._buildNightSpecFromRequest({"dayObs": "tomorrow"})


def test_buildNightSpecFromRequest_rejects_out_of_range_dayObs() -> None:
    """A YYYYMMDD outside the [1900, 3000] year band almost certainly
    means the caller passed something that isn't a dayObs — surface
    that as a 400 rather than letting a nonsense window go to Loki."""
    with pytest.raises(ValueError, match="YYYYMMDD"):
        server._buildNightSpecFromRequest({"dayObs": 12345})


def test_buildNightSpecFromRequest_password_passthrough() -> None:
    _, _, password = server._buildNightSpecFromRequest({"dayObs": 20260521, "password": "hunter2"})
    assert password == "hunter2"


# ----- _maybeSetLokiPassword ----------------------------------------------


def test_maybeSetLokiPassword_sets_env_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-empty password lands in ``LOKI_PASSWORD`` so the next
    subprocess.run inherits it."""
    monkeypatch.delenv("LOKI_PASSWORD", raising=False)
    server._maybeSetLokiPassword("hunter2")
    import os as _os

    assert _os.environ["LOKI_PASSWORD"] == "hunter2"


def test_maybeSetLokiPassword_noop_when_empty_or_None(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty/None password must NOT clobber an existing ``LOKI_PASSWORD``
    — otherwise the home page's optional credentials card would silently
    blow away a working env var on every fetch.
    """
    import os as _os

    monkeypatch.setenv("LOKI_PASSWORD", "preserve-me")
    server._maybeSetLokiPassword(None)
    assert _os.environ["LOKI_PASSWORD"] == "preserve-me"
    server._maybeSetLokiPassword("")
    assert _os.environ["LOKI_PASSWORD"] == "preserve-me"


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
