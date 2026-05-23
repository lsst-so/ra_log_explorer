"""Tests for `ra_log_explorer.parse`."""

from __future__ import annotations

import datetime as dt
import json
from collections import Counter
from pathlib import Path

import pytest

from ra_log_explorer import parse

# ----- LogLine ------------------------------------------------------------


def _makeJsonObj(line: str, timestamp: str, level: str | None = None) -> dict:
    labels: dict = {}
    if level is not None:
        labels["detected_level"] = level
    return {"line": line, "timestamp": timestamp, "labels": labels}


def test_parseLogLine_standard_format() -> None:
    obj = _makeJsonObj(
        "2026-05-20 08:45:46,216 lsst.rubintv.production.processControl.HeadProcessController "
        "doDetectorFanout INFO   Fanning LSSTCam-20260519-722 out to 189 detectors of 189 enabled \n",
        "2026-05-20T08:45:46.216887282+00:00",
        level="info",
    )
    parsed = parse.parseLogLine("head-pod", obj)
    assert parsed is not None
    assert parsed.pod == "head-pod"
    assert parsed.level == "info"
    assert parsed.logger == "lsst.rubintv.production.processControl.HeadProcessController"
    assert parsed.function == "doDetectorFanout"
    assert "Fanning LSSTCam-20260519-722" in parsed.message
    assert parsed.timestamp.tzinfo is dt.timezone.utc
    assert parsed.timestamp == dt.datetime(2026, 5, 20, 8, 45, 46, 216887, tzinfo=dt.timezone.utc)


def test_parseLogLine_fallback_for_third_party() -> None:
    obj = _makeJsonObj(
        "Traceback (most recent call last):\n",
        "2026-05-20T08:45:51.500100000+00:00",
    )
    parsed = parse.parseLogLine("worker-pod", obj)
    assert parsed is not None
    assert parsed.logger == ""
    assert parsed.function == ""
    assert parsed.raw == "Traceback (most recent call last):"
    assert parsed.level == "unknown"


def test_parseLogLine_returns_None_without_timestamp() -> None:
    assert parse.parseLogLine("p", {"line": "x"}) is None


def test_parseLogLine_label_level_overrides_inline_when_present() -> None:
    # detected_level=warn even though the inline level says INFO
    obj = _makeJsonObj(
        "2026-05-20 08:45:46,216 some.logger fn INFO   ok\n",
        "2026-05-20T08:45:46.216000+00:00",
        level="warn",
    )
    parsed = parse.parseLogLine("p", obj)
    assert parsed is not None
    assert parsed.level == "warn"


def test_parseTimestamp_trims_nanoseconds() -> None:
    # Microsecond precision only — sub-µs digits get dropped.
    t = parse._parseTimestamp("2026-05-20T08:45:46.216887282+01:00")
    assert t == dt.datetime(2026, 5, 20, 7, 45, 46, 216887, tzinfo=dt.timezone.utc)


# ----- _dataIdFields ------------------------------------------------------


def test_dataIdFields_exposure_visit_detector() -> None:
    exp, vis, det = parse._dataIdFields(
        "instrument: 'LSSTCam', detector: 7, exposure: 2026051900722, band: 'u'"
    )
    assert exp == 2026051900722
    assert vis is None
    assert det == 7


def test_dataIdFields_visit_only_falls_back_to_expId() -> None:
    # visit-keyed quanta (calibrateImage) only carry `visit:`. We want
    # the filter to still find the right exposure.
    exp, vis, det = parse._dataIdFields("instrument: 'LSSTCam', detector: 0, visit: 2026051900722")
    assert exp == 2026051900722
    assert vis == 2026051900722
    assert det == 0


# ----- classify -----------------------------------------------------------


def _classifyFixture(path: Path) -> list[parse.Event]:
    events: list[parse.Event] = []
    for ln in parse.iterPodLines(path):
        ev = parse.classify(ln)
        if ev is not None:
            events.append(ev)
    return events


def test_classify_head_fixture_covers_every_kind(headNodeJsonl: Path) -> None:
    events = _classifyFixture(headNodeJsonl)
    kinds = Counter(e.kind for e in events)
    expected = {
        "HEAD_DEFINE_VISIT",
        "HEAD_ONEOFF",
        "HEAD_FANOUT_DONE",
        "HEAD_PIPELINE_DECIDED",
        "HEAD_FANOUT_START",
        "HEAD_LOOP_SLOW",
        "HEAD_POSTISR_MOSAIC",
        "HEAD_GATHER_DISPATCH",
        "HEAD_VISITIMAGE_MOSAIC",
        "WARN",
    }
    missing = expected - kinds.keys()
    assert not missing, f"head fixture missed expected kinds: {missing}"


def test_classify_head_define_visit_extracts_expId(headNodeJsonl: Path) -> None:
    events = _classifyFixture(headNodeJsonl)
    define = [e for e in events if e.kind == "HEAD_DEFINE_VISIT"]
    assert len(define) == 1
    assert define[0].expId == 2026051900722


def test_classify_head_fanout_done_extracts_who(headNodeJsonl: Path) -> None:
    events = _classifyFixture(headNodeJsonl)
    fanout = [e for e in events if e.kind == "HEAD_FANOUT_DONE"]
    whos = {e.who for e in fanout}
    assert whos == {"AOS", "SFM"}


def test_classify_head_gather_dispatch_extracts_visit_and_who(headNodeJsonl: Path) -> None:
    events = _classifyFixture(headNodeJsonl)
    gathers = [e for e in events if e.kind == "HEAD_GATHER_DISPATCH"]
    assert len(gathers) >= 1
    g = gathers[0]
    assert g.expId == 2026051900722
    assert g.visit == 2026051900722
    assert g.who in {"SFM", "AOS_DANISH"}


def test_classify_head_loop_slow_extracts_wall_duration(headNodeJsonl: Path) -> None:
    events = _classifyFixture(headNodeJsonl)
    slow = [e for e in events if e.kind == "HEAD_LOOP_SLOW"]
    assert len(slow) == 1
    assert slow[0].durationS is not None
    assert slow[0].durationS > 0


def test_classify_sfm_fixture_full_chain(sfmWorkerJsonl: Path) -> None:
    events = _classifyFixture(sfmWorkerJsonl)
    kinds = [e.kind for e in events]
    assert "WORKER_PICKUP" in kinds
    assert "WORKER_WAIT_RAW" in kinds
    assert "WORKER_QG_START" in kinds
    assert "WORKER_QG_BUILT" in kinds
    assert "QUANTUM_PREP" in kinds
    assert "QUANTUM_DONE" in kinds
    assert "WORKER_BINNED_POST_ISR_IMAGE" in kinds
    assert "WORKER_BINNED_PRELIMINARY_VISIT_IMAGE" in kinds


def test_classify_sfm_isr_quantum_done_has_duration_and_task(sfmWorkerJsonl: Path) -> None:
    events = _classifyFixture(sfmWorkerJsonl)
    isrDones = [e for e in events if e.kind == "QUANTUM_DONE" and e.taskLabel == "isr"]
    assert len(isrDones) == 1
    e = isrDones[0]
    assert e.durationS is not None
    assert e.durationS > 0
    assert e.expId == 2026051900722
    assert e.detector == 0


def test_classify_sfm_calibrate_quantum_done_uses_visit_id(sfmWorkerJsonl: Path) -> None:
    # calibrateImage quanta are visit-keyed; _dataIdFields should still
    # populate expId so the filter doesn't lose them.
    events = _classifyFixture(sfmWorkerJsonl)
    calDones = [e for e in events if e.kind == "QUANTUM_DONE" and e.taskLabel == "calibrateImage"]
    assert len(calDones) == 1
    e = calDones[0]
    assert e.expId == 2026051900722
    assert e.visit == 2026051900722
    assert e.detector == 0


def test_classify_aos_fixture_has_all_task_labels(aosWorkerJsonl: Path) -> None:
    events = _classifyFixture(aosWorkerJsonl)
    tasks = {e.taskLabel for e in events if e.taskLabel}
    # We trimmed the fixture to cover these tasks specifically.
    assert tasks >= {
        "isr",
        "generateDonutDirectDetectTask",
        "cutOutDonutsCwfsPairTask",
        "calcZernikesTask",
    }


def test_classify_worker_qg_built_extracts_duration_and_detector(sfmWorkerJsonl: Path) -> None:
    events = _classifyFixture(sfmWorkerJsonl)
    qg = [e for e in events if e.kind == "WORKER_QG_BUILT"]
    assert len(qg) == 1
    assert qg[0].durationS is not None
    assert qg[0].expId == 2026051900722
    assert qg[0].detector == 0


def test_classify_no_event_for_unmatched_info_line() -> None:
    # A bare info line that doesn't match any pattern should classify to None.
    obj = _makeJsonObj(
        "2026-05-20 08:45:46,216 some.unrelated.logger doSomething INFO   no pattern here\n",
        "2026-05-20T08:45:46.216000+00:00",
        level="info",
    )
    ln = parse.parseLogLine("p", obj)
    assert ln is not None
    assert parse.classify(ln) is None


def test_classify_unrelated_warn_is_kept_with_bare_expid_when_visible() -> None:
    obj = _makeJsonObj(
        "2026-05-20 08:45:46,216 lsst.unrelated.thing whatever WARNING something bad for 2026051900722\n",
        "2026-05-20T08:45:46.216000+00:00",
        level="warn",
    )
    ln = parse.parseLogLine("p", obj)
    assert ln is not None
    ev = parse.classify(ln)
    assert ev is not None
    assert ev.kind == "WARN"
    assert ev.expId == 2026051900722


# ----- pod classification helpers -----------------------------------------


@pytest.mark.parametrize(
    "pod,expected",
    [
        ("s-lsstcam-run-head-node-684d89d6bb-44ctd", "head"),
        ("s-lsstcam-run-butler-watcher-58c5fd5777-ffdb8", "butler-watcher"),
        ("s-lsstcam-run-sfm-runner-workerset-0", "sfm"),
        ("s-lsstcam-run-aos-worker-aosworkerset-3", "aos"),
        ("s-lsstcam-run-step-1b-worker-gather1bset-0", "step1b"),
        # The critical one: step-1b-aos-worker must NOT be matched as plain aos.
        ("s-lsstcam-run-step-1b-aos-worker-gather1baosset-0", "step1b-aos"),
        ("s-lsstcam-run-backlog-worker-backlogset-18", "backlog"),
        ("s-lsstcam-run-mosaic-foo", "mosaic"),
        ("s-lsstcam-run-fwhm-plotting-66685b4bd-ddxml", "fwhm-plot"),
        ("s-lsstcam-run-radial-plotting-x", "radial-plot"),
        ("s-lsstcam-run-zernike-prediction-plotting-x", "zernike-plot"),
        ("s-lsstcam-run-psf-plotting-76cd86dcb7-699pb", "psf-plot"),
        ("s-lsstcam-run-guider-analysis-5757db446-bx9fb", "guider"),
        ("s-lsstcam-run-one-off-exp-record-6d678cc4fb-qp6gr", "one-off-exprecord"),
        ("s-lsstcam-run-one-off-post-isr-6fb45f7c87-65kkb", "one-off-postisr"),
        ("s-lsstcam-run-one-off-visit-image-8575c4c56b-86k9h", "one-off-visitimage"),
        ("s-lsstcam-run-metadata-server-6d9df4df6f-64gzz", "metadata-server"),
        # Longest-prefix-match: the more-specific metadata-server-* roles
        # must NOT collapse into the plain `metadata-server` group.
        ("s-lsstcam-run-metadata-server-aos-6f778fc678-lqsd9", "metadata-server-aos"),
        ("s-lsstcam-run-metadata-server-guiders-576d464744-hb67x", "metadata-server-guiders"),
        (
            "s-lsstcam-run-metadata-server-ra-performance-5d787b5c5b-5ds9n",
            "metadata-server-ra-performance",
        ),
        ("s-lsstcam-run-cluster-manager-7769cd6bd4-99jrs", "cluster-mgr"),
        ("s-lsstcam-run-cleanup-76577f69b4-qbjz6", "cleanup"),
        ("s-lsstcam-run-performance-monitor-6ff7c4dd9-2xfp9", "performance-monitor"),
        ("s-lsstcam-run-plotter-57d4c6d899-2cqmz", "plotter"),
        ("s-lsstcam-run-nightly-worker-gatherrollupset-0", "nightly-worker"),
        # 'misc' is a recognised instrument-like stem but no role prefix
        # matches the rest, so these land in 'other' (the safety-net group).
        ("s-misc-run-all-sky-68987c5f79-kt9cf", "other"),
        ("s-misc-run-tma-telemetry-54567bbb6c-c7f8k", "other"),
        # Infra pods with no `s-<inst>-run-` stem also fall to 'other'.
        ("rapid-analysis-squid-55c7c86f5-wjsct", "other"),
        ("redis-0", "other"),
        ("unrelated-pod", "other"),
    ],
)
def test_podGroup(pod: str, expected: str) -> None:
    assert parse.podGroup(pod) == expected


def test_podGroup_is_order_independent() -> None:
    """The classifier must give the same answer no matter how POD_GROUPS is
    iterated. This is the regression test for the substring-collision footgun
    that the old first-needle-wins design had.
    """
    pod = "s-lsstcam-run-metadata-server-aos-6f778fc678-lqsd9"
    saved = parse.POD_GROUPS.copy()
    try:
        # Try multiple iteration orders by replacing the dict with reverse
        # and an arbitrary shuffle. Longest-prefix-match should pin the
        # answer regardless.
        parse.POD_GROUPS.clear()
        parse.POD_GROUPS.update(dict(reversed(list(saved.items()))))
        assert parse.podGroup(pod) == "metadata-server-aos"
        parse.POD_GROUPS.clear()
        parse.POD_GROUPS.update(sorted(saved.items()))
        assert parse.podGroup(pod) == "metadata-server-aos"
    finally:
        parse.POD_GROUPS.clear()
        parse.POD_GROUPS.update(saved)


@pytest.mark.parametrize(
    "pod,expected",
    [
        ("s-lsstcam-run-sfm-runner-workerset-0", 0),
        ("s-lsstcam-run-sfm-runner-workerset-189", 189),
        ("s-lsstcam-run-aos-worker-aosworkerset-3", 3),
        ("s-lsstcam-run-head-node-684d89d6bb-44ctd", None),
        ("redis-0", None),  # numeric suffix exists but the needle doesn't match
    ],
)
def test_podOrdinal(pod: str, expected: int | None) -> None:
    assert parse.podOrdinal(pod) == expected


@pytest.mark.parametrize(
    "pod,expected",
    [
        ("s-lsstcam-run-head-node-x", "LSSTCam"),
        ("s-latiss-run-head-node-x", "LATISS"),
        ("s-lsstcomcam-run-x-y", "LSSTComCam"),
        ("s-lsstcomcamsim-run-x-y", "LSSTComCamSim"),
        ("unrelated-pod", None),
    ],
)
def test_podInstrument(pod: str, expected: str | None) -> None:
    assert parse.podInstrument(pod) == expected


# ----- extractExpId / tagLinesWithExpId -----------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Bare 13-digit form.
        ("Running pipeline for 2026051900722 on detector 5", 2026051900722),
        # Split form, snake_case.
        ("dataId={'day_obs': 20260519, 'seq_num': 722, 'detector': 5}", 2026051900722),
        # Split form, camelCase.
        ("processing dayObs=20260519 seqNum=722", 2026051900722),
        # Split form, squashed (no separator).
        ("dayobs:20260519 seqnum:722 ...", 2026051900722),
        # Mixed case with hyphen variants.
        ("Day-Obs: 20260519, Seq-Num: 722", 2026051900722),
        # Bare form wins outright when both are present.
        ("dataId: {2026051900722, day_obs: 20260520, seq_num: 999}", 2026051900722),
        # Neither form present.
        ("Connecting to redis at host=localhost port=6379", None),
        # Only dayObs (no seqNum) — not enough to form an id.
        ("Working on day_obs 20260519 across detectors", None),
        # Only seqNum.
        ("seq_num=722 detector=5", None),
        # Truncated dataId-like number (12 digits) — not matched.
        ("garbled 202605190072 in middle", None),
    ],
)
def test_extractExpId(raw: str, expected: int | None) -> None:
    assert parse.extractExpId(raw) == expected


def test_carryoverGroups_includes_workers_excludes_control_plane() -> None:
    co = parse.carryoverGroups()
    # Workers that process one dataId at a time.
    assert "sfm" in co
    assert "aos" in co
    assert "step1b" in co
    assert "step1b-aos" in co
    assert "backlog" in co
    assert "psf-plot" in co
    # Control-plane / cluster-wide pods must NOT carry over.
    for excluded in (
        "head",
        "butler-watcher",
        "metadata-server",
        "metadata-server-aos",
        "metadata-server-guiders",
        "metadata-server-ra-performance",
        "cluster-mgr",
        "cleanup",
        "performance-monitor",
        "other",
    ):
        assert excluded not in co, f"{excluded} must not be a carryover group"


def test_tagLinesWithExpId_carryover_for_worker_groups() -> None:
    lines = [
        "Running pipeline for 2026051900722 on detector 5",
        "Doing some sub-step (no id mentioned)",
        "Another mid-task log line",
        "Pipeline done; picking up day_obs=20260519 seq_num=723",
        "Continuing the new task",
    ]
    tagged = list(parse.tagLinesWithExpId(lines, "sfm"))
    assert tagged == [2026051900722, 2026051900722, 2026051900722, 2026051900723, 2026051900723]


def test_tagLinesWithExpId_no_carryover_for_head_node() -> None:
    lines = [
        "Defining visit for 2026051900722",
        "Event loop tick",
        "Fanning out to 189 detectors",
        "Sending signal for 2026051900723",
    ]
    tagged = list(parse.tagLinesWithExpId(lines, "head"))
    # Only lines that explicitly mention an id get one; carryover is off.
    assert tagged == [2026051900722, None, None, 2026051900723]


def test_tagLinesWithExpId_leading_orphan_lines_remain_None() -> None:
    """Lines before the first dataId mention can't be retroactively
    attributed — they yield ``None`` even in a carryover group.
    """
    lines = [
        "Pod started",
        "Initialising butler",
        "Running pipeline for 2026051900722",
        "Sub-step continues",
    ]
    tagged = list(parse.tagLinesWithExpId(lines, "sfm"))
    assert tagged == [None, None, 2026051900722, 2026051900722]


def test_summarizePod_records_per_expId_first_last_and_wait(tmp_path: Path) -> None:
    """A worker pod accumulates per-expId first/last timestamps (carryover
    aware) and a per-expId wait-seconds total.
    """
    p = tmp_path / "s-lsstcam-run-sfm-runner-workerset-7.jsonl"
    lines = [
        # Pickup: line 1 establishes 2026051900722. Carryover applies.
        (
            "2026-05-20T08:46:20.000+00:00",
            "2026-05-20 08:46:20,000 lsst.daf.butler some_func INFO   "
            "Running pipeline for 2026051900722 on detector 5",
        ),
        # Silent middle line — should still be attributed to 722.
        (
            "2026-05-20T08:46:25.000+00:00",
            "2026-05-20 08:46:25,000 lsst.pipe.base run INFO   Doing some work",
        ),
        # Wait pattern — counts toward 722's wait total.
        (
            "2026-05-20T08:46:27.500+00:00",
            "2026-05-20 08:46:27,500 lsst.daf.butler load INFO   "
            "Spent 0.36 seconds waiting for the raw exposure",
        ),
        # Last line attributable to 722.
        (
            "2026-05-20T08:46:30.000+00:00",
            "2026-05-20 08:46:30,000 lsst.pipe.base run INFO   Reporting SFM "
            "finished for detector 5 of exposure 2026051900722",
        ),
    ]
    with open(p, "w") as fh:
        for ts, raw in lines:
            fh.write(
                json.dumps({"timestamp": ts, "labels": {"detected_level": "info"}, "line": raw + "\n"}) + "\n"
            )
    s = parse.summarizePod(p)
    assert s.group == "sfm"
    assert 2026051900722 in s.expIdsSeen
    firstLast = s.expIdFirstLast[2026051900722]
    assert firstLast[0] == dt.datetime(2026, 5, 20, 8, 46, 20, tzinfo=dt.timezone.utc)
    assert firstLast[1] == dt.datetime(2026, 5, 20, 8, 46, 30, tzinfo=dt.timezone.utc)
    assert s.expIdWaitSeconds[2026051900722] == pytest.approx(0.36)


def test_summarizePod_no_carryover_for_head_node_first_last(tmp_path: Path) -> None:
    """For the head node, only lines that explicitly mention a dataId set
    the first/last range; the silent intervening lines don't extend it.
    """
    p = tmp_path / "s-lsstcam-run-head-node-abc-xyz.jsonl"
    lines = [
        (
            "2026-05-20T08:46:00.000+00:00",
            "2026-05-20 08:46:00,000 lsst.rubintv.production.processControl head_fn INFO   "
            "Defining visit for 2026051900722",
        ),
        (
            "2026-05-20T08:46:05.000+00:00",
            "2026-05-20 08:46:05,000 some.logger fn INFO   loop tick",
        ),
        (
            "2026-05-20T08:46:10.000+00:00",
            "2026-05-20 08:46:10,000 lsst.rubintv.production.processControl head_fn INFO   "
            "New exposure record for 2026051900722",
        ),
        (
            "2026-05-20T08:46:15.000+00:00",
            "2026-05-20 08:46:15,000 some.logger fn INFO   another tick",
        ),
    ]
    with open(p, "w") as fh:
        for ts, raw in lines:
            fh.write(
                json.dumps({"timestamp": ts, "labels": {"detected_level": "info"}, "line": raw + "\n"}) + "\n"
            )
    s = parse.summarizePod(p)
    assert s.group == "head"
    # Only the two lines that explicitly mention 722 bound the range —
    # the silent lines in between don't extend it (no carryover).
    firstLast = s.expIdFirstLast[2026051900722]
    assert firstLast[0] == dt.datetime(2026, 5, 20, 8, 46, 0, tzinfo=dt.timezone.utc)
    assert firstLast[1] == dt.datetime(2026, 5, 20, 8, 46, 10, tzinfo=dt.timezone.utc)


def test_summarizePod_recognises_split_dayObs_seqNum_form(tmp_path: Path) -> None:
    """A pod whose log only ever uses day_obs / seq_num (no bare 13-digit
    form) must still resolve to the right exposure in ``expIdsSeen``.
    """
    p = tmp_path / "fake-pod.jsonl"
    p.write_text(
        '{"timestamp": "2026-05-20T08:46:20.000+00:00", "labels": {"detected_level": "info"}, '
        '"line": "2026-05-20 08:46:20,000 lsst.pipe.base.runner runQuantum INFO   '
        "Running task for dataId={instrument: 'LSSTCam', day_obs: 20260519, seq_num: 722, "
        'detector: 5}\\n"}\n'
    )
    s = parse.summarizePod(p)
    assert 2026051900722 in s.expIdsSeen


def test_groupLabels_round_trips_with_podGroup() -> None:
    """Every (label, prefix) pair in POD_GROUPS appears in groupLabels(),
    and applying `podGroup` to a pod name built from the prefix gives
    back the label.
    """
    labels = parse.groupLabels()
    assert labels == dict(parse.POD_GROUPS)
    for label, prefix in parse.POD_GROUPS.items():
        # A synthesised pod name with this prefix resolves to the same label.
        assert parse.podGroup(f"s-lsstcam-run-{prefix}-abc") == label


# ----- summarizePod -------------------------------------------------------


def test_summarizePod_head_fixture_counts(headNodeJsonl: Path) -> None:
    s = parse.summarizePod(headNodeJsonl)
    assert s.pod == headNodeJsonl.stem
    assert s.group == "other"  # the fixture's stem isn't a real pod name
    assert s.nLines == _countLines(headNodeJsonl)
    # The fixture is filtered to expId 722 references plus one slow loop
    # warning that doesn't mention any exposure.
    assert 2026051900722 in s.expIdsSeen
    # 2 warns in the fixture: the "Event loop running slow" line and the
    # "No free workers available" line.
    assert s.nWarn >= 2
    assert s.nError == 0
    assert s.nTraceback == 0
    assert s.firstTs is not None
    assert s.lastTs is not None
    assert s.firstTs <= s.lastTs


def test_summarizePod_traceback_fixture_counts(tracebackJsonl: Path) -> None:
    s = parse.summarizePod(tracebackJsonl)
    assert s.nError >= 1
    assert s.nTraceback >= 1
    assert 2026051900722 in s.expIdsSeen


def test_iterPodLines_handles_missing_file(tmp_path: Path) -> None:
    p = tmp_path / "does-not-exist.jsonl"
    assert list(parse.iterPodLines(p)) == []


def test_iterPodLines_skips_blank_and_bad_json(tmp_path: Path) -> None:
    p = tmp_path / "mixed.jsonl"
    p.write_text(
        "\n"  # blank
        + "not json at all\n"
        + json.dumps(
            {
                "line": "2026-05-20 08:00:00,000 a b INFO   ok\n",
                "timestamp": "2026-05-20T08:00:00.000+00:00",
            }
        )
        + "\n"
    )
    lines = list(parse.iterPodLines(p))
    assert len(lines) == 1
    assert lines[0].function == "b"


# ----- podsTouchingExp ----------------------------------------------------


def test_podsTouchingExp_includes_only_pods_with_match(
    headNodeJsonl: Path, sfmWorkerJsonl: Path, tmp_path: Path
) -> None:
    # Build a synthetic pod that doesn't mention the target.
    noise = tmp_path / "noise.jsonl"
    noise.write_text(
        json.dumps(
            {
                "line": "2026-05-20 08:00:00,000 some.logger fn INFO   nothing interesting here\n",
                "timestamp": "2026-05-20T08:00:00.000+00:00",
            }
        )
        + "\n"
    )
    summaries = [
        parse.summarizePod(headNodeJsonl),
        parse.summarizePod(sfmWorkerJsonl),
        parse.summarizePod(noise),
    ]
    touching = parse.podsTouchingExp(summaries, 2026051900722)
    pods = {s.pod for s in touching}
    assert headNodeJsonl.stem in pods
    assert sfmWorkerJsonl.stem in pods
    assert noise.stem not in pods


# ----- edge cases: timestamp + level normalisation ------------------------


def test_normalizeLevel_handles_crit_fatal_as_error() -> None:
    """The DM stack occasionally emits CRITICAL / FATAL — those should
    bucket into ``error`` so the per-pod counters and UI colouring
    behave consistently."""
    assert parse._normalizeLevel("crit") == "error"
    assert parse._normalizeLevel("critical") == "error"
    assert parse._normalizeLevel("fatal") == "error"
    assert parse._normalizeLevel("CRITICAL") == "error"


def test_normalizeLevel_unknown_strings_pass_through_lowercased() -> None:
    # Anything we don't recognise stays as-is (lowercased) so it shows up
    # in the UI without being silently swallowed.
    assert parse._normalizeLevel("trace") == "trace"
    assert parse._normalizeLevel(None) == "unknown"
    assert parse._normalizeLevel("") == "unknown"


def test_parseTimestamp_handles_string_without_tz_offset() -> None:
    """Some upstream sources emit a naked ISO with no tz suffix — we
    should treat it as UTC rather than crashing."""
    t = parse._parseTimestamp("2026-05-21T13:00:00.123456")
    assert t == dt.datetime(2026, 5, 21, 13, 0, 0, 123456, tzinfo=dt.timezone.utc)


def test_parseTimestamp_handles_Z_suffix() -> None:
    t = parse._parseTimestamp("2026-05-21T13:00:00Z")
    assert t == dt.datetime(2026, 5, 21, 13, 0, 0, tzinfo=dt.timezone.utc)


def test_parseLogLine_returns_None_for_unparseable_timestamp() -> None:
    """A timestamp the regex would accept but ``fromisoformat`` rejects
    must not throw — the line is just skipped."""
    line = parse.parseLogLine("p", {"timestamp": "not-an-iso", "line": "x"})
    assert line is None


def test_parseLogLine_returns_None_for_missing_timestamp() -> None:
    line = parse.parseLogLine("p", {"line": "x"})
    assert line is None


# ----- edge cases: pod-group classification -------------------------------


def test_podGroup_unknown_prefix_lands_in_other() -> None:
    # No anchored prefix match — the group must be "other" so the UI
    # still shows the pod rather than silently hiding it.
    assert parse.podGroup("s-lsstcam-run-something-completely-new-0") == "other"
    assert parse.podGroup("totally-random-pod-name") == "other"


def test_podInstrument_recognises_inst_prefixes() -> None:
    # podInstrument returns the canonical CamelCase form (matches the
    # Butler `instrument` field that the rest of the codebase compares
    # against).
    assert parse.podInstrument("s-lsstcam-run-aos-worker-0") == "LSSTCam"
    # Longest-match wins over shorter substring: lsstcomcamsim must NOT
    # be misclassified as lsstcomcam.
    assert parse.podInstrument("s-lsstcomcamsim-run-sfm-runner-0") == "LSSTComCamSim"


# ----- edge cases: traceback capture --------------------------------------


def _writePodLog(path: Path, lines: list[tuple[str, str, str]]) -> None:
    """Helper to write a per-pod JSONL fixture: (ts, level, raw)."""
    with open(path, "w") as fh:
        for ts, level, raw in lines:
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


def test_summarizePod_captures_chained_exception_during_handling(tmp_path: Path) -> None:
    """A traceback can contain "During handling of the above exception,
    another exception occurred:" — when the lines arrive contiguously
    (no blank-line gaps, which is what Loki's ``--forward`` JSONL
    delivers when the chained block is one Python logging call) we
    should keep capturing through that marker until we reach the real
    terminal exception class. The captured class stays at the first
    one seen so the failure aggregation tables don't gain spurious
    extra entries from the chained RuntimeError.
    """
    p = tmp_path / "s-lsstcam-run-aos-worker-0.jsonl"
    _writePodLog(
        p,
        [
            (
                "2026-05-21T13:00:00.000+00:00",
                "info",
                "Running pipeline for 2026052100050 detector 1",
            ),
            ("2026-05-21T13:00:01.000+00:00", "error", "Traceback (most recent call last):"),
            ("2026-05-21T13:00:01.001+00:00", "error", '  File "/x.py", line 1, in inner'),
            ("2026-05-21T13:00:01.002+00:00", "error", "ValueError: bad donut"),
            (
                "2026-05-21T13:00:01.004+00:00",
                "error",
                "During handling of the above exception, another exception occurred:",
            ),
            ("2026-05-21T13:00:01.006+00:00", "error", '  File "/y.py", line 2, in outer'),
            ("2026-05-21T13:00:01.007+00:00", "error", "RuntimeError: re-raised"),
        ],
    )
    s = parse.summarizePod(p)
    assert len(s.tracebacks) == 1
    tb = s.tracebacks[0]
    # The first exception class we see is the one recorded.
    assert tb.excClass == "ValueError"
    # The chained block landed inside the same body record.
    assert "During handling" in tb.body
    assert "RuntimeError: re-raised" in tb.body


def test_summarizePod_blank_line_terminates_traceback_capture(tmp_path: Path) -> None:
    """A blank line inside a traceback currently terminates capture —
    pin that as the known limitation so a future loosening of
    ``_isTracebackBodyLine`` shows up as a deliberate test churn,
    not a silent regression.
    """
    p = tmp_path / "s-lsstcam-run-aos-worker-0.jsonl"
    _writePodLog(
        p,
        [
            (
                "2026-05-21T13:00:00.000+00:00",
                "info",
                "Running pipeline for 2026052100051 detector 1",
            ),
            ("2026-05-21T13:00:01.000+00:00", "error", "Traceback (most recent call last):"),
            ("2026-05-21T13:00:01.001+00:00", "error", '  File "/x.py", line 1, in inner'),
            ("2026-05-21T13:00:01.002+00:00", "error", "ValueError: first"),
            # A literal blank line — currently treated as a terminator.
            ("2026-05-21T13:00:01.003+00:00", "info", ""),
            # Anything after the blank is no longer in the captured body.
            ("2026-05-21T13:00:01.004+00:00", "error", "RuntimeError: should-not-appear"),
        ],
    )
    s = parse.summarizePod(p)
    assert len(s.tracebacks) == 1
    assert s.tracebacks[0].excClass == "ValueError"
    assert "should-not-appear" not in s.tracebacks[0].body


def test_summarizePod_back_to_back_tracebacks_both_recorded(tmp_path: Path) -> None:
    """If a new ``Traceback (most recent call last):`` arrives while we
    still have an open one, the open one must be finalised before the
    new one starts — otherwise the second body silently grows onto the
    first record's tail."""
    p = tmp_path / "s-lsstcam-run-aos-worker-0.jsonl"
    _writePodLog(
        p,
        [
            (
                "2026-05-21T13:00:00.000+00:00",
                "info",
                "Running pipeline for 2026052100060 detector 1",
            ),
            ("2026-05-21T13:00:01.000+00:00", "error", "Traceback (most recent call last):"),
            ("2026-05-21T13:00:01.001+00:00", "error", "ValueError: first"),
            # Second traceback starts WITHOUT an intervening non-body line.
            ("2026-05-21T13:00:01.002+00:00", "error", "Traceback (most recent call last):"),
            ("2026-05-21T13:00:01.003+00:00", "error", "RuntimeError: second"),
        ],
    )
    s = parse.summarizePod(p)
    assert len(s.tracebacks) == 2
    classes = [tb.excClass for tb in s.tracebacks]
    assert classes == ["ValueError", "RuntimeError"]
    assert "first" in s.tracebacks[0].excMessage
    assert "second" in s.tracebacks[1].excMessage


def test_summarizePod_traceback_with_no_terminal_class_stays_unknown(tmp_path: Path) -> None:
    """If the lead line appears but the body never carries an
    exception-shaped class, we still ship the record so the user sees
    the context — class just stays <unknown>. Exercises the end-of-
    pod-log flush path."""
    p = tmp_path / "s-lsstcam-run-aos-worker-0.jsonl"
    _writePodLog(
        p,
        [
            (
                "2026-05-21T13:00:00.000+00:00",
                "info",
                "Running pipeline for 2026052100070 detector 1",
            ),
            ("2026-05-21T13:00:01.000+00:00", "error", "Traceback (most recent call last):"),
            ("2026-05-21T13:00:01.001+00:00", "error", '  File "/x.py", line 1, in foo'),
            # End of pod log — no terminal class line. The capture
            # state machine flushes whatever it has.
        ],
    )
    s = parse.summarizePod(p)
    assert len(s.tracebacks) == 1
    assert s.tracebacks[0].excClass == "<unknown>"
    assert "/x.py" in s.tracebacks[0].body


# ----- edge cases: WORKER_REPORT_FAILED variant ---------------------------


def test_classify_emits_WORKER_REPORT_FAILED_for_failed_status() -> None:
    """The "failed" form of the report line should land in
    ``WORKER_REPORT_FAILED`` rather than getting silently dropped or
    mis-bucketed into the FINISHED kind. ``classify`` only inspects
    worker-shaped lines, so the logger string must reflect that."""
    line = parse.LogLine(
        pod="p",
        timestamp=dt.datetime(2026, 5, 21, 13, 0, tzinfo=dt.timezone.utc),
        level="info",
        logger="lsst.rubintv.production.SingleCorePipelineRunner",
        function="report",
        message=("Reporting AOSSingleCorePipelineRunner failed for detector 5 of exposure 2026052100050"),
        raw="Reporting AOSSingleCorePipelineRunner failed for detector 5 of exposure 2026052100050",
    )
    ev = parse.classify(line)
    assert ev is not None
    assert ev.kind == "WORKER_REPORT_FAILED"
    assert ev.expId == 2026052100050
    assert ev.detector == 5


# ----- edge cases: malformed JSONL --------------------------------------


def test_iterPodLines_skips_malformed_jsonl_records(tmp_path: Path) -> None:
    """A single mid-stream malformed JSON record should be skipped, not
    abort the iteration. Real Loki output occasionally has a truncated
    last record."""
    p = tmp_path / "broken.jsonl"
    p.write_text(
        json.dumps(
            {
                "timestamp": "2026-05-21T13:00:00.000+00:00",
                "labels": {"detected_level": "info"},
                "line": "first ok\n",
            }
        )
        + "\n"
        + "this is not valid json\n"
        + json.dumps(
            {
                "timestamp": "2026-05-21T13:00:01.000+00:00",
                "labels": {"detected_level": "info"},
                "line": "second ok\n",
            }
        )
        + "\n"
    )
    out = list(parse.iterPodLines(p))
    assert [ln.raw for ln in out] == ["first ok", "second ok"]


def test_summarizeAll_skips_non_jsonl_files_in_pods_dir(tmp_path: Path) -> None:
    """Cache dirs are walked with ``iterdir`` — a stray non-.jsonl file
    (e.g. an editor swapfile, a partial download) must not be parsed."""
    cacheDir = tmp_path / "cache"
    podsDir = cacheDir / "pods"
    podsDir.mkdir(parents=True)
    _writePodLog(
        podsDir / "s-lsstcam-run-aos-worker-0.jsonl",
        [
            (
                "2026-05-21T13:00:00.000+00:00",
                "info",
                "Running pipeline for 2026052100050 detector 1",
            ),
        ],
    )
    (podsDir / "notes.txt").write_text("just some scratch notes\n")
    summaries = parse.summarizeAll(cacheDir)
    assert {s.pod for s in summaries} == {"s-lsstcam-run-aos-worker-0"}


def test_summarizeAll_returns_empty_when_pods_dir_missing(tmp_path: Path) -> None:
    """fetchAll always creates pods/, but a hand-crafted cache or a
    half-deleted dir might not — make sure that's a safe no-op."""
    cacheDir = tmp_path / "no-pods-here"
    cacheDir.mkdir()
    assert parse.summarizeAll(cacheDir) == []


def test_summarizeAll_sorts_pods_deterministically(tmp_path: Path) -> None:
    """night.errorsByType picks the *first* sample message it sees per
    exception class, which only makes sense if the summary order is
    stable across runs. Pin that here."""
    cacheDir = tmp_path / "cache"
    podsDir = cacheDir / "pods"
    podsDir.mkdir(parents=True)
    for name in ("zzz-pod", "aaa-pod", "mmm-pod"):
        _writePodLog(
            podsDir / f"{name}.jsonl",
            [
                (
                    "2026-05-21T13:00:00.000+00:00",
                    "info",
                    "Running pipeline for 2026052100050 detector 1",
                )
            ],
        )
    summaries = parse.summarizeAll(cacheDir)
    assert [s.pod for s in summaries] == ["aaa-pod", "mmm-pod", "zzz-pod"]


def test_podsForTimeline_includes_other_group_even_without_match(tmp_path: Path) -> None:
    """``"other"``-group pods are deliberately surfaced regardless of
    whether they touched the dataId — the UI safety net for unknown /
    new roles. Pin the contract."""
    p = tmp_path / "weird-pod.jsonl"
    _writePodLog(
        p,
        [
            (
                "2026-05-21T13:00:00.000+00:00",
                "info",
                "Unrelated chatter, no dataIds anywhere",
            )
        ],
    )
    s = parse.summarizePod(p)
    assert s.group == "other"
    assert 2026051900722 not in s.expIdsSeen
    out = parse.podsForTimeline([s], 2026051900722)
    assert s in out


def test_podsForTimeline_excludes_classified_pods_that_dont_touch_expId(tmp_path: Path) -> None:
    """A known-role pod (sfm, aos, head, …) that didn't process the
    target dataId is dropped from the timeline — we don't surface every
    pod for every exposure, only the ones we actually need."""
    p = tmp_path / "s-lsstcam-run-sfm-runner-workerset-99.jsonl"
    _writePodLog(
        p,
        [
            (
                "2026-05-21T13:00:00.000+00:00",
                "info",
                "Running pipeline for 2026052100099 detector 1",
            )
        ],
    )
    s = parse.summarizePod(p)
    out = parse.podsForTimeline([s], 2026051900722)  # different dataId
    assert s not in out


def test_summarizePod_tracks_back_to_back_dataIds_with_carryover(tmp_path: Path) -> None:
    """A worker pod processing two dataIds in succession must record
    distinct first/last spans for each. Without this the timeline would
    show a single conflated span across both visits.
    """
    p = tmp_path / "s-lsstcam-run-sfm-runner-workerset-1.jsonl"
    _writePodLog(
        p,
        [
            (
                "2026-05-21T13:00:00.000+00:00",
                "info",
                "Running pipeline for 2026052100100 detector 0",
            ),
            ("2026-05-21T13:00:05.000+00:00", "info", "still working on the first one"),
            (
                "2026-05-21T13:00:10.000+00:00",
                "info",
                "Running pipeline for 2026052100101 detector 0",
            ),
            ("2026-05-21T13:00:15.000+00:00", "info", "second one chugging along"),
        ],
    )
    s = parse.summarizePod(p)
    f1 = s.expIdFirstLast[2026052100100]
    f2 = s.expIdFirstLast[2026052100101]
    assert f1[0] < f1[1] < f2[0] < f2[1]
    # The first visit's span doesn't extend over the second visit's pickup.
    assert f1[1] < dt.datetime(2026, 5, 21, 13, 0, 10, tzinfo=dt.timezone.utc)


def test_summarizePod_traceback_body_capped_at_max_lines(tmp_path: Path) -> None:
    """A pathological traceback (hundreds of frames) must not blow memory
    or grow the captured body without bound. The cap is internal but
    pinning it here documents the contract."""
    p = tmp_path / "s-lsstcam-run-aos-worker-0.jsonl"
    rows: list[tuple[str, str, str]] = [
        ("2026-05-21T13:00:00.000+00:00", "info", "Running pipeline for 2026052100200 detector 1"),
        ("2026-05-21T13:00:01.000+00:00", "error", "Traceback (most recent call last):"),
    ]
    for i in range(300):  # > _TRACEBACK_MAX_LINES
        rows.append(
            (f"2026-05-21T13:00:0{i % 9 + 2}.{i:06d}+00:00", "error", f'  File "/x.py", line {i}, in inner')
        )
    rows.append(("2026-05-21T13:01:00.000+00:00", "error", "RuntimeError: at last"))
    _writePodLog(p, rows)
    s = parse.summarizePod(p)
    assert len(s.tracebacks) == 1
    tb = s.tracebacks[0]
    # The exception class is still captured even though the body has
    # already filled up — class detection continues past the cap.
    assert tb.excClass == "RuntimeError"
    # The body itself is bounded (we don't pin the exact line count; the
    # contract is "won't grow without bound").
    assert len(tb.body.splitlines()) <= parse._TRACEBACK_MAX_LINES + 1


def test_extractExpId_split_form_no_separator_doesnt_match(tmp_path: Path) -> None:
    """The split-form regex requires at least one non-digit char between
    the key and the value. "dayobs20260519" (no separator) MUST NOT
    match — otherwise stray numeric runs in logs would false-positive
    as dataIds.
    """
    assert parse.extractExpId("dayobs20260519seqnum722") is None


def test_classify_head_incoming_extracts_expId() -> None:
    """The head node logs ``New exposure record for <expId>`` the moment
    the butler watcher hands the record over; that's the earliest
    head-node-side moment we can place on the timeline. Without this
    classification, the time between "incoming" and HEAD_DEFINE_VISIT
    (which includes butler defineVisit cost) is invisible.
    """
    line = parse.LogLine(
        pod="p",
        timestamp=dt.datetime(2026, 5, 20, 8, 45, 46, 100000, tzinfo=dt.timezone.utc),
        level="info",
        logger="lsst.rubintv.production.processControl.HeadProcessController",
        function="ingestRecord",
        message="New exposure record for 2026051900722",
        raw=(
            "2026-05-20 08:45:46,100 lsst.rubintv.production.processControl ... "
            "New exposure record for 2026051900722"
        ),
    )
    ev = parse.classify(line)
    assert ev is not None
    assert ev.kind == "HEAD_INCOMING"
    assert ev.expId == 2026051900722


# ----- helpers ------------------------------------------------------------


def _countLines(path: Path) -> int:
    n = 0
    for line in path.read_text().splitlines():
        if line.strip():
            n += 1
    return n
