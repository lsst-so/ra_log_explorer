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
