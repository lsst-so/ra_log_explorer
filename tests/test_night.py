"""Tests for `ra_log_explorer.night` analyses and the dayObs helpers."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from ra_log_explorer import config, night, parse

# ----- dayObs helpers -------------------------------------------------------


def test_dayObsStartUtc_uses_UTC_minus_12_rollover() -> None:
    # dayObs 20260521 starts at 2026-05-21 12:00:00 UTC (i.e. midnight in
    # the observatory's UTC-12 'observation day' frame).
    start = config.dayObsStartUtc(20260521)
    assert start == dt.datetime(2026, 5, 21, 12, 0, 0, tzinfo=dt.timezone.utc)


def test_dayObsEndUtc_is_24h_after_start() -> None:
    end = config.dayObsEndUtc(20260521)
    assert end == dt.datetime(2026, 5, 22, 12, 0, 0, tzinfo=dt.timezone.utc)


# ----- traceback capture (parse.summarizePod) ------------------------------


def _writePodLog(path: Path, lines: list[tuple[str, str, str]]) -> None:
    """Write a per-pod JSONL where each tuple is (timestamp, level, raw)."""
    import json as _json

    with open(path, "w") as fh:
        for ts, level, raw in lines:
            fh.write(
                _json.dumps(
                    {
                        "timestamp": ts,
                        "labels": {"detected_level": level},
                        "line": raw + "\n",
                    }
                )
                + "\n"
            )


def test_summarizePod_captures_traceback_with_class_and_message(tmp_path: Path) -> None:
    p = tmp_path / "s-lsstcam-run-aos-worker-aosworkerset-3.jsonl"
    _writePodLog(
        p,
        [
            (
                "2026-05-21T13:00:00.000+00:00",
                "info",
                "2026-05-21 13:00:00,000 lsst.pipe.base run INFO   "
                "Running pipeline for 2026052100012 on detector 5",
            ),
            (
                "2026-05-21T13:00:05.000+00:00",
                "error",
                "2026-05-21 13:00:05,000 lsst.pipe.base run ERROR   " "Traceback (most recent call last):",
            ),
            (
                "2026-05-21T13:00:05.001+00:00",
                "error",
                '  File "/x/pipeline.py", line 42, in run',
            ),
            (
                "2026-05-21T13:00:05.002+00:00",
                "error",
                "    result = compute()",
            ),
            (
                "2026-05-21T13:00:05.003+00:00",
                "error",
                "RuntimeError: butler raw image not found",
            ),
        ],
    )
    s = parse.summarizePod(p)
    assert s.group == "aos"
    assert s.nTraceback == 1
    assert len(s.tracebacks) == 1
    tb = s.tracebacks[0]
    assert tb.excClass == "RuntimeError"
    assert tb.excMessage == "butler raw image not found"
    assert tb.expId == 2026052100012  # carryover-attributed from the pickup line
    assert "Traceback (most recent call last):" in tb.body
    assert 'File "/x/pipeline.py"' in tb.body


def test_summarizePod_captures_multiple_tracebacks(tmp_path: Path) -> None:
    p = tmp_path / "s-lsstcam-run-aos-worker-aosworkerset-7.jsonl"
    _writePodLog(
        p,
        [
            (
                "2026-05-21T13:00:00.000+00:00",
                "info",
                "2026-05-21 13:00:00,000 logger fn INFO   Running pipeline for 2026052100100 detector 1",
            ),
            ("2026-05-21T13:00:01.000+00:00", "error", "Traceback (most recent call last):"),
            ("2026-05-21T13:00:01.001+00:00", "error", "ValueError: bad donut count"),
            (
                "2026-05-21T13:00:10.000+00:00",
                "info",
                "2026-05-21 13:00:10,000 logger fn INFO   Running pipeline for 2026052100101 detector 1",
            ),
            ("2026-05-21T13:00:11.000+00:00", "error", "Traceback (most recent call last):"),
            ("2026-05-21T13:00:11.001+00:00", "error", "RuntimeError: oh no"),
        ],
    )
    s = parse.summarizePod(p)
    assert len(s.tracebacks) == 2
    classes = [tb.excClass for tb in s.tracebacks]
    assert classes == ["ValueError", "RuntimeError"]
    # Carryover correctly attributes each traceback to its own pickup.
    assert s.tracebacks[0].expId == 2026052100100
    assert s.tracebacks[1].expId == 2026052100101


# ----- night.py analyses ---------------------------------------------------


def _stubSummary(
    pod: str,
    group: str,
    *,
    tracebacks: list[parse.TracebackRecord] | None = None,
    events: list[parse.Event] | None = None,
    expIdsSeen: set[int] | None = None,
) -> parse.PodSummary:
    return parse.PodSummary(
        pod=pod,
        group=group,
        instrument=None,
        ordinal=None,
        nLines=10,
        nWarn=0,
        nError=0,
        nTraceback=len(tracebacks or []),
        firstTs=None,
        lastTs=None,
        expIdsSeen=expIdsSeen or set(),
        events=events or [],
        tracebacks=tracebacks or [],
    )


def _tb(pod: str, when: dt.datetime, expId: int | None, cls: str, msg: str = "") -> parse.TracebackRecord:
    return parse.TracebackRecord(
        pod=pod, t=when, expId=expId, excClass=cls, excMessage=msg, body=f"Traceback…\n{cls}: {msg}"
    )


def _ev(when: dt.datetime, expId: int | None, kind: str, taskLabel: str | None = None) -> parse.Event:
    return parse.Event(pod="p", t=when, kind=kind, level="info", expId=expId, taskLabel=taskLabel)


def test_computeTopStats_counts_distinct_things() -> None:
    t = dt.datetime(2026, 5, 21, 13, 0, 0, tzinfo=dt.timezone.utc)
    s1 = _stubSummary(
        "podA",
        "aos",
        tracebacks=[_tb("podA", t, 1, "RuntimeError"), _tb("podA", t, 1, "RuntimeError")],
        expIdsSeen={1, 2},
    )
    s2 = _stubSummary("podB", "aos", tracebacks=[_tb("podB", t, 2, "ValueError")], expIdsSeen={2, 3})
    s3 = _stubSummary("podC", "aos", expIdsSeen={4})  # no tracebacks
    stats = night.computeTopStats([s1, s2, s3])
    assert stats.nVisitsSeen == 4
    assert stats.nPods == 3
    assert stats.nTracebacks == 3
    assert stats.nDataIdsWithTraceback == 2  # 1 and 2 — not 3 or 4
    assert stats.nPodsWithTraceback == 2
    assert stats.nDistinctExceptionClasses == 2


def test_errorsByType_sorts_by_count_desc_and_keeps_sample_message() -> None:
    t = dt.datetime(2026, 5, 21, 13, 0, 0, tzinfo=dt.timezone.utc)
    s = _stubSummary(
        "podA",
        "aos",
        tracebacks=[
            _tb("podA", t, 1, "RuntimeError", "first message"),
            _tb("podA", t, 2, "RuntimeError", "second"),
            _tb("podA", t, 3, "ValueError", "vmsg"),
        ],
    )
    rows = night.errorsByType([s])
    assert [r.excClass for r in rows] == ["RuntimeError", "ValueError"]
    assert rows[0].count == 2
    assert rows[0].sampleMessage == "first message"  # first instance wins
    assert rows[1].sampleMessage == "vmsg"


def test_errorsByPod_skips_pods_with_zero_tracebacks() -> None:
    t = dt.datetime(2026, 5, 21, 13, 0, 0, tzinfo=dt.timezone.utc)
    s1 = _stubSummary("podA", "aos", tracebacks=[_tb("podA", t, 1, "RuntimeError")])
    s2 = _stubSummary("podB", "aos")  # no tracebacks
    rows = night.errorsByPod([s1, s2])
    assert [r.pod for r in rows] == ["podA"]


def test_firstTaskStartByDataId_picks_earliest_across_pods() -> None:
    t1 = dt.datetime(2026, 5, 21, 13, 0, 0, tzinfo=dt.timezone.utc)
    t2 = t1 + dt.timedelta(seconds=2)
    t3 = t1 - dt.timedelta(seconds=1)  # earlier
    sA = _stubSummary(
        "podA",
        "aos",
        events=[_ev(t1, 100, "QUANTUM_PREP", "isr"), _ev(t2, 100, "QUANTUM_DONE", "isr")],
    )
    sB = _stubSummary("podB", "aos", events=[_ev(t3, 100, "WORKER_PICKUP")])
    starts = night.firstTaskStartByDataId([sA, sB])
    assert starts[100] == t3


def test_calcZernikesEndByDataId_matches_case_insensitive_substring() -> None:
    t1 = dt.datetime(2026, 5, 21, 13, 0, 0, tzinfo=dt.timezone.utc)
    t2 = t1 + dt.timedelta(seconds=3)
    sA = _stubSummary(
        "podA",
        "aos",
        events=[
            _ev(t1, 100, "QUANTUM_DONE", "calcZernikesTask"),
            _ev(t2, 100, "QUANTUM_DONE", "calcZernikesUnrolledTask"),  # also matches
            _ev(t1, 100, "QUANTUM_DONE", "isr"),  # doesn't match
        ],
    )
    ends = night.calcZernikesEndByDataId([sA])
    assert ends[100] == t2  # the latest matching one wins


def test_buildHistogram_bins_values_and_reports_drops() -> None:
    h = night.buildHistogram("first", "s", [1.0, 1.1, 5.0, 9.9], nDroppedNoTZero=3, nBins=10)
    assert h.nValues == 4
    assert h.nDropped == 3
    assert sum(h.counts) == 4
    assert h.xMin == 1.0
    assert h.xMax == 9.9
    assert len(h.counts) == 10
    # Without dataIds, the per-bin dataId list is empty.
    assert h.dataIdsByBin == []


def test_buildHistogram_empty_input_yields_empty_histogram() -> None:
    h = night.buildHistogram("zero", "s", [], nDroppedNoTZero=5)
    assert h.counts == []
    assert h.nValues == 0
    assert h.nDropped == 5
    assert h.dataIdsByBin == []


def test_buildHistogram_attributes_dataIds_to_their_bins() -> None:
    # Span 0..9 across 10 equal-width bins. Each dataId lands in the
    # bin matching its integer offset.
    offsets = [0.0, 0.5, 1.5, 5.5, 9.0]
    dataIds = [100, 101, 102, 103, 104]
    h = night.buildHistogram("first", "s", offsets, dataIds=dataIds, nBins=10)
    assert len(h.dataIdsByBin) == 10
    assert sum(len(b) for b in h.dataIdsByBin) == 5
    # 0..1s bin contains the two earliest, sorted by offset.
    assert h.dataIdsByBin[0] == [100, 101]
    # 9..10s bin (rightmost) contains the latest dataId — right-edge
    # rule lands x==xMax in the last bin.
    assert h.dataIdsByBin[-1] == [104]
    # All bins collectively cover all 5 dataIds without duplicates.
    flat = [d for b in h.dataIdsByBin for d in b]
    assert sorted(flat) == sorted(dataIds)


def test_buildHistogram_single_value_yields_single_bin_with_id() -> None:
    h = night.buildHistogram("only", "s", [3.0], dataIds=[42], nBins=10)
    assert h.counts == [1]
    assert h.dataIdsByBin == [[42]]


def test_buildHistogram_raises_when_parallel_lists_differ() -> None:
    with pytest.raises(ValueError):
        night.buildHistogram("bad", "s", [1.0, 2.0], dataIds=[1])


def test_computeDeltaShutterOffsets_returns_parallel_lists() -> None:
    t = dt.datetime(2026, 5, 21, 13, 0, 0, tzinfo=dt.timezone.utc)
    times = {100: t + dt.timedelta(seconds=5), 200: t + dt.timedelta(seconds=10)}
    closes = {100: t, 200: t}
    offsets, ids, nDropped = night.computeDeltaShutterOffsets(times, closes)
    assert nDropped == 0
    # Pair up so the test doesn't depend on dict ordering.
    pairs = sorted(zip(ids, offsets))
    assert pairs == [(100, 5.0), (200, 10.0)]


def test_computeDeltaShutterOffsets_counts_drops_without_emitting_id() -> None:
    t = dt.datetime(2026, 5, 21, 13, 0, 0, tzinfo=dt.timezone.utc)
    times = {100: t + dt.timedelta(seconds=5), 200: t + dt.timedelta(seconds=10)}
    closes = {100: t}  # 200 missing
    offsets, ids, nDropped = night.computeDeltaShutterOffsets(times, closes)
    assert nDropped == 1
    assert ids == [100]
    assert offsets == [5.0]


def test_failureRows_carry_offsetS_when_shutter_close_known() -> None:
    t = dt.datetime(2026, 5, 21, 13, 0, 0, tzinfo=dt.timezone.utc)
    close = t - dt.timedelta(seconds=42)
    s = _stubSummary(
        "podA",
        "aos",
        tracebacks=[_tb("podA", t, 100, "RuntimeError"), _tb("podA", t, 200, "RuntimeError")],
    )
    rows = night.failureRows([s], shutterCloseByExpId={100: close})
    by = {r.dataId: r for r in rows}
    assert by[100].offsetS == pytest.approx(42.0)
    assert by[200].offsetS is None  # no shutter close for 200 -> drop the offset


def test_tracebackBody_round_trips_via_bodyKey() -> None:
    t = dt.datetime(2026, 5, 21, 13, 0, 0, tzinfo=dt.timezone.utc)
    s = _stubSummary("podA", "aos", tracebacks=[_tb("podA", t, 100, "RuntimeError", "boom")])
    rows = night.failureRows([s])
    body = night.tracebackBody([s], rows[0].bodyKey)
    assert body is not None
    assert "Traceback…" in body
    assert "RuntimeError: boom" in body


def test_tracebackBody_returns_None_for_unknown_key() -> None:
    s = _stubSummary("podA", "aos")
    assert night.tracebackBody([s], "no-such-key") is None
