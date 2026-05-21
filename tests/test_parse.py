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


# ----- helpers ------------------------------------------------------------


def _countLines(path: Path) -> int:
    n = 0
    for line in path.read_text().splitlines():
        if line.strip():
            n += 1
    return n
