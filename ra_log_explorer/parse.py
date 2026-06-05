"""Parse Loki JSONL log files into structured events.

Each log line in a per-pod JSONL file looks like:

    {
      "labels": {"detected_level": "info"},
      "line": "2026-05-20 08:45:46,216 lsst.rubintv.production.processControl"
              ".HeadProcessController doDetectorFanout INFO   Fanning ... \\n",
      "timestamp": "2026-05-20T09:45:46.216887282+01:00"
    }

This module:
  * walks the cache dir
  * parses each JSONL line into a `LogLine`
  * classifies a subset of lines into structured `Event` records
  * works out which pods touched a given exposure id
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

# ----- pod flavor classification -------------------------------------------

# Pod-role map. Key is the short slug used for the CSS badge class
# (`.badge-<slug>`); value is the full role prefix that appears in the
# pod name AFTER the `s-<instrument>-run-` stem.
#
# Classification is anchored longest-prefix-match — neither dict iteration
# order nor relative entry length affects correctness, so adding a new
# more-specific role (e.g. `metadata-server-aos` next to the existing
# `metadata-server`) doesn't risk the substring-collision footgun the
# previous list-of-tuples form had.
POD_GROUPS: dict[str, str] = {
    "head": "head-node",
    "butler-watcher": "butler-watcher",
    "sfm": "sfm-runner",
    "step1b": "step-1b-worker",
    "step1b-aos": "step-1b-aos-worker",
    "aos": "aos-worker",
    "backlog": "backlog-worker",
    "nightly-worker": "nightly-worker",
    "mosaic": "mosaic",
    "one-off-exprecord": "one-off-exp-record",
    "one-off-postisr": "one-off-post-isr",
    "one-off-visitimage": "one-off-visit-image",
    "guider": "guider-analysis",
    "plotter": "plotter",
    "psf-plot": "psf-plotting",
    "fwhm-plot": "fwhm-plotting",
    "radial-plot": "radial-plotting",
    "zernike-plot": "zernike-prediction-plotting",
    "metadata-server": "metadata-server",
    "metadata-server-aos": "metadata-server-aos",
    "metadata-server-guiders": "metadata-server-guiders",
    "metadata-server-ra-performance": "metadata-server-ra-performance",
    "cluster-mgr": "cluster-manager",
    "cleanup": "cleanup",
    "performance-monitor": "performance-monitor",
}

# Strip this stem off the front of a pod name before doing the prefix match.
# The full set of instruments we expect in this repo's logs.
_RUN_PREFIX_RE = re.compile(r"^s-(?:lsstcam|latiss|lsstcomcam|lsstcomcamsim|misc)-run-")


def podGroup(pod: str) -> str:
    """Return a short label identifying the pod's role.

    Longest matching role prefix in :data:`POD_GROUPS` wins, so e.g. a
    `metadata-server-aos-…` pod resolves to ``"metadata-server-aos"`` and
    not the shorter ``"metadata-server"`` even though the latter is also
    a valid prefix. Pods that don't match any known role fall into
    ``"other"`` (intentionally surfaced in the UI as a safety net for
    new/unrecognised roles).
    """
    stem = _RUN_PREFIX_RE.sub("", pod, count=1)
    bestLabel: str | None = None
    bestLen = -1
    for label, prefix in POD_GROUPS.items():
        if stem == prefix or stem.startswith(prefix + "-"):
            if len(prefix) > bestLen:
                bestLabel = label
                bestLen = len(prefix)
    return bestLabel or "other"


def groupLabels() -> dict[str, str]:
    """Map each short ``podGroup`` label to its full on-disk role prefix.

    The frontend uses this for two cosmetic things at once: (1) showing the
    role prefix on the group header / badge so it matches what users would
    grep for in `kubectl get pods`, and (2) stripping that prefix from the
    rendered pod name so e.g. `s-lsstcam-run-sfm-runner-workerset-094`
    collapses to `workerset-094`.
    """
    return dict(POD_GROUPS)


def podInstrument(pod: str) -> str | None:
    """Return 'LSSTCam', 'LATISS', etc. from the pod name, or None.

    Like ``POD_GROUPS``, order matters here because the match is "first
    wins" — ``lsstcomcam`` is a substring of ``lsstcomcamsim``, so the
    Sim variant must come first.
    """
    for inst, needle in (
        ("LSSTComCamSim", "lsstcomcamsim"),
        ("LSSTComCam", "lsstcomcam"),
        ("LSSTCam", "lsstcam"),
        ("LATISS", "latiss"),
    ):
        if needle in pod:
            return inst
    return None


# Worker pods of PER_DETECTOR flavor encode their detector affinity in the
# stateful set ordinal at the end of the name, e.g. `sfm-runner-workerset-094`.
_WORKERSET_RE = re.compile(r"workerset-(\d+)$")
_AOS_WORKERSET_RE = re.compile(r"aosworkerset-(\d+)$")


def podOrdinal(pod: str) -> int | None:
    """Return the StatefulSet ordinal if the pod name ends with `-N`."""
    m = _WORKERSET_RE.search(pod) or _AOS_WORKERSET_RE.search(pod)
    if m:
        return int(m.group(1))
    return None


# ----- raw log line parsing -------------------------------------------------


@dataclass
class LogLine:
    pod: str
    timestamp: dt.datetime  # UTC, microsecond precision
    level: str  # "info" | "warn" | "error" | "debug" | "unknown"
    logger: str  # e.g. "lsst.rubintv.production.processControl.HeadProcessController"
    function: str  # e.g. "doDetectorFanout"
    message: str  # the human-readable tail of the line
    raw: str  # original line, newline stripped


# The standard LSST log format used across all pods:
#   "2026-05-20 08:45:46,216 <logger> <function>  <LEVEL>   <message>"
# Some lines (third-party libs) don't follow this and we fall back to a
# best-effort parse.
_PYLOG_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[.,]\d+\s+"
    r"(?P<logger>\S+)\s+"
    r"(?P<function>\S+)\s+"
    r"(?P<level>DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\s+"
    r"(?P<message>.*)$"
)


def _normalizeLevel(level: str | None) -> str:
    if not level:
        return "unknown"
    lv = level.lower()
    if lv in ("warn", "warning"):
        return "warn"
    if lv in ("err", "error"):
        return "error"
    if lv in ("crit", "critical", "fatal"):
        return "error"
    return lv


def _parseTimestamp(s: str) -> dt.datetime:
    # Loki emits e.g. "2026-05-20T09:45:46.216887282+01:00" — trim the trailing
    # nanosecond digit (datetime.fromisoformat supports up to 6 frac digits in
    # 3.9; we drop down to microseconds).
    # Find the timezone offset.
    tzMatch = re.search(r"([+-]\d{2}:\d{2}|Z)$", s)
    if tzMatch:
        head = s[: tzMatch.start()]
        tz = tzMatch.group(1)
    else:
        head, tz = s, ""
    if "." in head:
        date, frac = head.split(".", 1)
        frac = frac[:6]  # microsecond precision
        head = f"{date}.{frac}"
    if tz == "Z":
        tz = "+00:00"
    iso = head + tz if tz else head
    parsed = dt.datetime.fromisoformat(iso)
    # If the upstream string had no timezone marker, ``fromisoformat``
    # returns a naive datetime; ``astimezone`` would then treat it as
    # *local* time, which is the wrong default for log timestamps that
    # are always UTC by convention. Pin it to UTC explicitly.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def parseLogLine(pod: str, jsonObj: dict) -> LogLine | None:
    raw = jsonObj.get("line", "").rstrip("\n")
    tsStr = jsonObj.get("timestamp", "")
    if not tsStr:
        return None
    try:
        ts = _parseTimestamp(tsStr)
    except ValueError:
        return None
    labelLevel = (jsonObj.get("labels") or {}).get("detected_level")
    m = _PYLOG_RE.match(raw)
    if m:
        return LogLine(
            pod=pod,
            timestamp=ts,
            level=_normalizeLevel(labelLevel or m.group("level")),
            logger=m.group("logger"),
            function=m.group("function"),
            message=m.group("message"),
            raw=raw,
        )
    return LogLine(
        pod=pod,
        timestamp=ts,
        level=_normalizeLevel(labelLevel),
        logger="",
        function="",
        message=raw,
        raw=raw,
    )


def iterPodLines(podLogPath: Path) -> Iterator[LogLine]:
    pod = podLogPath.stem
    if not podLogPath.exists():
        return
    with podLogPath.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            parsed = parseLogLine(pod, obj)
            if parsed is not None:
                yield parsed


# ----- event classification -------------------------------------------------


@dataclass
class Event:
    pod: str
    t: dt.datetime
    kind: str
    level: str
    expId: int | None = None
    detector: int | None = None
    visit: int | None = None
    who: str | None = None  # SFM / AOS / ISR / AOS_DANISH
    taskLabel: str | None = None
    durationS: float | None = None
    flavor: str | None = None
    message: str = ""
    raw: str = ""


# Patterns -------------------------------------------------------------------

# helper: extract "exposure: N" or "visit: N" out of a dataId-style string.
_DATAID_EXP_RE = re.compile(r"exposure:\s*(\d+)")
_DATAID_VISIT_RE = re.compile(r"visit:\s*(\d+)")
_DATAID_DET_RE = re.compile(r"detector:\s*(\d+)")
_BARE_EXPID_RE = re.compile(r"\b(202\d{10})\b")  # 13-digit YYYYMMDDSSSSS

# Split dayObs / seqNum form, accepting camelCase / snake_case / squashed
# spellings as they all turn up in real LSST logs. We pair these on a
# per-line basis (both must appear on the same line to count); the
# combined value is `dayObs * 100000 + seqNum`, matching the canonical
# 13-digit id form.
# Allow up to a few non-digit characters between the key and value so that
# the quoted-dict spelling (`'day_obs': 20260519`) and the plain
# `dayObs=20260519` spelling both match without needing two regexes.
_DAYOBS_RE = re.compile(r"day[_-]?obs[^0-9]{1,5}(\d{8})", re.IGNORECASE)
_SEQNUM_RE = re.compile(r"seq[_-]?num[^0-9]{1,5}(\d{1,6})", re.IGNORECASE)


def extractExpId(raw: str) -> int | None:
    """Pull the dataId out of `raw`, or return ``None`` if absent.

    Accepts both the bare 13-digit form (`2026051900722`) and the split
    form (`day_obs: 20260519` + `seq_num: 722` on the same line, in any
    of the camelCase / snake_case / squashed spellings). When both are
    present the bare 13-digit form wins.
    """
    m = _BARE_EXPID_RE.search(raw)
    if m:
        return int(m.group(1))
    dM = _DAYOBS_RE.search(raw)
    sM = _SEQNUM_RE.search(raw)
    if dM and sM:
        return int(dM.group(1)) * 100000 + int(sM.group(1))
    return None


# Pod groups where a worker is processing one dataId at a time: once the
# id has been logged, every subsequent line in that pod's stream belongs
# to that same id until a new id appears. The control-plane / cluster-
# wide pods (head node, butler watcher, metadata servers, cluster
# manager, cleanup, performance monitor) interleave many dataIds and
# are deliberately excluded — attributing their "next line" to the
# "previous id" would silently misclassify everything.
_CARRYOVER_GROUPS: frozenset[str] = frozenset(
    {
        "sfm",
        "aos",
        "step1b",
        "step1b-aos",
        "backlog",
        "nightly-worker",
        "mosaic",
        "guider",
        "plotter",
        "psf-plot",
        "fwhm-plot",
        "radial-plot",
        "zernike-plot",
        "one-off-exprecord",
        "one-off-postisr",
        "one-off-visitimage",
    }
)


def carryoverGroups() -> frozenset[str]:
    """Return the set of pod-group labels for which the per-line dataId
    carryover rule applies (see :data:`_CARRYOVER_GROUPS`).
    """
    return _CARRYOVER_GROUPS


# "Spent 0.36 seconds waiting for the raw image data" — generic pattern
# across worker pods for "blocked on an external load". Captured per
# inferred-expId and summed for the per-pod summary stats.
_WAIT_RE = re.compile(r"Spent\s+(\d+(?:\.\d+)?)\s+seconds\s+waiting\s+for\s+the\b")


def tagLinesWithExpId(rawLines: Iterable[str], group: str) -> Iterator[int | None]:
    """Yield one inferred dataId per input line.

    For groups in :func:`carryoverGroups`, lines that don't explicitly
    mention a dataId inherit the most-recently-seen one — matching the
    real-world worker behaviour where, once a dataId is picked up, the
    rest of that pod's logs belong to it until the next pickup. For
    other groups (head node, metadata servers, etc.) only lines that
    explicitly contain a dataId get tagged; everything else yields
    ``None``.
    """
    isCarryover = group in _CARRYOVER_GROUPS
    current: int | None = None
    for raw in rawLines:
        found = extractExpId(raw)
        if found is not None:
            current = found
        yield current if isCarryover else found


# head node messages
_HEAD_DEFINE_RE = re.compile(r"Defining visit \(if needed\) for (\d+)")
_HEAD_PIPELINE_RE = re.compile(r"Sending (\d+) imageType='(?P<image>[^']+)' for (?P<rest>.+?)$")
_HEAD_FANOUT_RE = re.compile(r"Fanning (?P<inst>\S+) out to (?P<n>\d+) detectors of (?P<m>\d+) enabled")
_HEAD_SENT_PAYLOADS_RE = re.compile(
    r"Sent (?P<n>\d+) payloads to free workers, (?P<m>\d+) to busy workers for (?P<who>\S+)"
)
_HEAD_GATHER_DISPATCH_RE = re.compile(
    r"Dispatching step1b for (?P<who>\S+) with complete inputs:.*visit:\s*(?P<visit>\d+)"
)
_HEAD_POSTISR_MOSAIC_RE = re.compile(r"Dispatching complete post_isr_image mosaic for expId=(\d+)")
_HEAD_VISITIMAGE_MOSAIC_RE = re.compile(r"Dispatching complete preliminary_visit_image mosaic for (\d+)")
_HEAD_ONEOFF_RE = re.compile(
    r"Sending signal to one-off processor for "
    r"(?P<inst>\S+?)-(?P<dayObs>\d{8})-(?P<seq>\d+)\+PodFlavor\.(?P<flavor>\S+)"
)
_HEAD_LOOP_SLOW_RE = re.compile(
    r"Event loop running slow, last loop took (?P<wall>[\d.]+)s with (?P<work>[\d.]+)s of work"
)
# The head-node logs `New exposure record for <expId>` the moment the
# butler watcher hands an expRecord over for processing — i.e. the
# earliest moment the head node "saw" the exposure. Useful as a
# timeline marker right before HEAD_DEFINE_VISIT.
_HEAD_INCOMING_RE = re.compile(r"New exposure record for (\d+)")

# worker messages
_WORKER_PICKUP_RE = re.compile(r"Running pipeline for \{(?P<body>[^}]+)\}")
_WORKER_WAIT_RAW_RE = re.compile(r"Waiting for raw for \{(?P<body>[^}]+)\}")
_WORKER_QG_START_RE = re.compile(
    r"Making (?P<kind>\S+QG) builder for (?P<step>\S+) for expId (?P<exp>\d+) for (?P<who>\S+)"
)
_WORKER_QG_BUILT_RE = re.compile(
    r"Building quantum graph for \{(?P<body>[^}]+)\} for (?P<who>\S+) took (?P<dur>[\d.]+)s"
)
_WORKER_QUANTUM_PREP_RE = re.compile(
    r"Preparing execution of quantum for label=(?P<task>\S+) dataId=\{(?P<body>[^}]+)\}"
)
_WORKER_QUANTUM_RUN_RE = re.compile(r"Constructing task and executing quantum for label=(?P<task>\S+)")
_WORKER_QUANTUM_DONE_RE = re.compile(
    r"Execution of task '(?P<task>[^']+)' on quantum \{(?P<body>[^}]+)\} took (?P<dur>[\d.]+) seconds"
)
_WORKER_WROTE_BINNED_RE = re.compile(r"Wrote binned (?P<kind>\S+) for \{(?P<body>[^}]+)\}")
_WORKER_REPORT_FINISHED_RE = re.compile(
    r"Reporting (?P<who>\S+) (?P<status>finished|failed) for detector (?P<det>\d+) of exposure (?P<exp>\d+)"
)


def _dataIdFields(body: str) -> tuple[int | None, int | None, int | None]:
    """Pull (expId, visit, detector) out of a brace-body.

    For science exposures the visit ID equals the exposure ID, so when only
    ``visit:`` is present in the dataId we fall back to using it as the
    exposure id too — this keeps downstream filters from misclassifying
    visit-keyed quanta as "global" events.
    """
    expM = _DATAID_EXP_RE.search(body)
    visM = _DATAID_VISIT_RE.search(body)
    detM = _DATAID_DET_RE.search(body)
    exp = int(expM.group(1)) if expM else None
    vis = int(visM.group(1)) if visM else None
    det = int(detM.group(1)) if detM else None
    if exp is None and vis is not None:
        exp = vis
    return exp, vis, det


def classify(line: LogLine) -> Event | None:
    """Try to match `line` to one of the known event patterns."""
    msg = line.message
    t = line.timestamp
    pod = line.pod

    # ----- head node patterns -----
    if "processControl" in line.logger:
        if m := _HEAD_INCOMING_RE.search(msg):
            return Event(
                pod, t, "HEAD_INCOMING", line.level, expId=int(m.group(1)), message=msg, raw=line.raw
            )
        if m := _HEAD_DEFINE_RE.search(msg):
            return Event(
                pod, t, "HEAD_DEFINE_VISIT", line.level, expId=int(m.group(1)), message=msg, raw=line.raw
            )
        if m := _HEAD_PIPELINE_RE.search(msg):
            return Event(
                pod,
                t,
                "HEAD_PIPELINE_DECIDED",
                line.level,
                expId=int(m.group(1)),
                who=m.group("rest").strip(),
                message=msg,
                raw=line.raw,
            )
        if m := _HEAD_FANOUT_RE.search(msg):
            return Event(pod, t, "HEAD_FANOUT_START", line.level, message=msg, raw=line.raw)
        if m := _HEAD_SENT_PAYLOADS_RE.search(msg):
            return Event(
                pod, t, "HEAD_FANOUT_DONE", line.level, who=m.group("who"), message=msg, raw=line.raw
            )
        if m := _HEAD_GATHER_DISPATCH_RE.search(msg):
            return Event(
                pod,
                t,
                "HEAD_GATHER_DISPATCH",
                line.level,
                visit=int(m.group("visit")),
                expId=int(m.group("visit")),
                who=m.group("who"),
                message=msg,
                raw=line.raw,
            )
        if m := _HEAD_POSTISR_MOSAIC_RE.search(msg):
            return Event(
                pod, t, "HEAD_POSTISR_MOSAIC", line.level, expId=int(m.group(1)), message=msg, raw=line.raw
            )
        if m := _HEAD_VISITIMAGE_MOSAIC_RE.search(msg):
            return Event(
                pod, t, "HEAD_VISITIMAGE_MOSAIC", line.level, expId=int(m.group(1)), message=msg, raw=line.raw
            )
        if m := _HEAD_ONEOFF_RE.search(msg):
            # The log message uses an unpadded `<inst>-<dayObs>-<seq>`
            # form. Reconstruct the 13-digit YYYYMMDDSSSSS expId by
            # mixing dayObs (8 digits) and seq into one integer.
            dayObs = int(m.group("dayObs"))
            seq = int(m.group("seq"))
            expId = dayObs * 100000 + seq
            return Event(
                pod,
                t,
                "HEAD_ONEOFF",
                line.level,
                expId=expId,
                flavor=m.group("flavor"),
                message=msg,
                raw=line.raw,
            )
        if m := _HEAD_LOOP_SLOW_RE.search(msg):
            return Event(
                pod,
                t,
                "HEAD_LOOP_SLOW",
                line.level,
                durationS=float(m.group("wall")),
                message=msg,
                raw=line.raw,
            )

    # ----- worker patterns -----
    if "SingleCorePipelineRunner" in line.logger:
        if m := _WORKER_PICKUP_RE.search(msg):
            exp, vis, det = _dataIdFields(m.group("body"))
            return Event(
                pod,
                t,
                "WORKER_PICKUP",
                line.level,
                expId=exp,
                visit=vis,
                detector=det,
                message=msg,
                raw=line.raw,
            )
        if m := _WORKER_WAIT_RAW_RE.search(msg):
            exp, vis, det = _dataIdFields(m.group("body"))
            return Event(
                pod,
                t,
                "WORKER_WAIT_RAW",
                line.level,
                expId=exp,
                visit=vis,
                detector=det,
                message=msg,
                raw=line.raw,
            )
        if m := _WORKER_QG_START_RE.search(msg):
            return Event(
                pod,
                t,
                "WORKER_QG_START",
                line.level,
                expId=int(m.group("exp")),
                who=m.group("who"),
                message=msg,
                raw=line.raw,
            )
        if m := _WORKER_QG_BUILT_RE.search(msg):
            exp, vis, det = _dataIdFields(m.group("body"))
            return Event(
                pod,
                t,
                "WORKER_QG_BUILT",
                line.level,
                expId=exp,
                visit=vis,
                detector=det,
                who=m.group("who"),
                durationS=float(m.group("dur")),
                message=msg,
                raw=line.raw,
            )
        if m := _WORKER_WROTE_BINNED_RE.search(msg):
            exp, vis, det = _dataIdFields(m.group("body"))
            return Event(
                pod,
                t,
                "WORKER_BINNED_" + m.group("kind").upper(),
                line.level,
                expId=exp,
                visit=vis,
                detector=det,
                message=msg,
                raw=line.raw,
            )
        if m := _WORKER_REPORT_FINISHED_RE.search(msg):
            return Event(
                pod,
                t,
                "WORKER_REPORT_" + m.group("status").upper(),
                line.level,
                expId=int(m.group("exp")),
                detector=int(m.group("det")),
                who=m.group("who"),
                message=msg,
                raw=line.raw,
            )

    # quantum start/end via the single_quantum_executor
    if "single_quantum_executor" in line.logger:
        if m := _WORKER_QUANTUM_PREP_RE.search(msg):
            exp, vis, det = _dataIdFields(m.group("body"))
            return Event(
                pod,
                t,
                "QUANTUM_PREP",
                line.level,
                expId=exp,
                visit=vis,
                detector=det,
                taskLabel=m.group("task"),
                message=msg,
                raw=line.raw,
            )
        if m := _WORKER_QUANTUM_DONE_RE.search(msg):
            exp, vis, det = _dataIdFields(m.group("body"))
            return Event(
                pod,
                t,
                "QUANTUM_DONE",
                line.level,
                expId=exp,
                visit=vis,
                detector=det,
                taskLabel=m.group("task"),
                durationS=float(m.group("dur")),
                message=msg,
                raw=line.raw,
            )

    # generic warning / error fallthrough — only escalated through detected_level
    if line.level in ("warn", "error") and line.logger:
        # extract expId from message if present (so the UI can filter by dataId)
        exp = None
        if m := _BARE_EXPID_RE.search(line.raw):
            exp = int(m.group(1))
        return Event(
            pod,
            t,
            "WARN" if line.level == "warn" else "ERROR",
            line.level,
            expId=exp,
            message=msg,
            raw=line.raw,
        )

    return None


# ----- per-pod summary ------------------------------------------------------


@dataclass
class PodSummary:
    pod: str
    group: str
    instrument: str | None
    ordinal: int | None
    nLines: int
    nWarn: int
    nError: int
    nTraceback: int
    firstTs: dt.datetime | None
    lastTs: dt.datetime | None
    expIdsSeen: set[int] = field(default_factory=set)
    events: list[Event] = field(default_factory=list)
    # First / last log-line timestamps per inferred dataId, computed using
    # the carryover rule for worker pods. Drives the per-pod "start" and
    # "duration" stats and the "window too narrow" truncation flags
    # (truncated if first/last == firstTs/lastTs of the whole pod log,
    # i.e. the relevance reaches the very edge of what we captured).
    expIdFirstLast: dict[int, tuple[dt.datetime, dt.datetime]] = field(default_factory=dict)
    # Total seconds spent in "Spent N seconds waiting for the …" lines,
    # keyed by inferred dataId. Carryover-aware.
    expIdWaitSeconds: dict[int, float] = field(default_factory=dict)
    # One :class:`TracebackRecord` per traceback found in this pod's log.
    # Each captures the carryover-attributed dataId, the exception class
    # + message, and a capped body for the drilldown view. Populated by
    # ``summarizePod``.
    tracebacks: list["TracebackRecord"] = field(default_factory=list)


@dataclass
class TracebackRecord:
    """One Python traceback found in a pod's log.

    Capturing the structured exception class + a short body up front
    lets the night-view's "errors by type" tally and the per-dataId
    drilldown work straight off the cached ``PodSummary`` — no second
    pass over the raw JSONL.
    """

    pod: str
    t: dt.datetime  # time of the "Traceback (most recent…)" leader line
    expId: int | None  # carryover-attributed dataId, if any
    # e.g. "RuntimeError". Three sentinel values surface a non-classified
    # traceback, kept distinct so the UI can tell them apart:
    #   "<unclassified>" — the traceback reached its terminating
    #     exception line (a column-0 ``Foo: …`` shape) but that class
    #     didn't match our classifier (e.g. ``StopIteration``, or a
    #     custom class with no canonical Error/Exception/… suffix). The
    #     record is *complete*; we just couldn't name the type.
    #   "<truncated>"    — the traceback was genuinely cut short before
    #     any terminating exception line: the log forwarder lost the
    #     tail, another logger interleaved a line mid-stack, or the pod
    #     log simply ended inside the frames.
    #   "<unknown>"      — transient default while the body is still
    #     being collected; always resolved to one of the two above at
    #     finalisation. Never appears on a finished record.
    excClass: str
    excMessage: str  # the rest of the exception line, capped
    body: str  # full traceback text, capped
    # Set once we observe the traceback's terminating exception line
    # (classified or not). Drives the <unclassified> vs <truncated>
    # split in :func:`_finaliseTraceback`.
    reachedTerminator: bool = False


# How many lines / characters to capture for each traceback body. Bodies
# longer than this just get truncated for the drilldown view, but the
# class-line scan keeps running until the traceback ends so we still
# tag the right exception even when the body would otherwise overflow.
# 250 lines is generous: Python 3.13 includes caret lines for each frame
# (~2 lines per frame), so a 30-frame traceback runs 60+ lines just for
# frames, plus chained exception blocks ("During handling …") and the
# final ExceptionClass line. Worst-case real tracebacks we've seen run
# ~130 lines; 250 leaves room without exploding memory.
_TRACEBACK_MAX_LINES = 250
_TRACEBACK_MAX_CHARS = 32_000
# Exception class line. Tries to match shapes like:
#
#   RuntimeError: ...
#   KeyboardInterrupt
#   galsim.errors.GalSimRangeError: ...        ← lowercase module prefix
#   lsst.daf.butler.DatasetTypeError: ...      ← lowercase module prefix
#
# Many third-party libraries (galsim, lsst, …) raise fully-qualified
# exceptions whose module path is all lowercase. The earlier regex
# required the entire line to start with a capital, which silently
# missed those — every such traceback got tagged "<unknown>".
_EXC_CLASS_RE = re.compile(
    # Optional dotted lowercase module prefix, e.g. `galsim.errors.`.
    r"^(?:[a-z_][a-z0-9_]*\.)*"
    # Capital-led class name, allowing nested dotted suffixes
    # (`Foo.SubError`), ending with one of the canonical class suffixes.
    r"(?P<cls>[A-Z][A-Za-z0-9_]*"
    r"(?:\.[A-Za-z][A-Za-z0-9_]*)*"
    r"(?:Error|Exception|Exit|Warning|Interrupt|Cancelled))"
    r"(?:\s*:\s*(?P<msg>.*))?$"
)
# Broader "this is the terminating exception line of a traceback" shape:
# a column-0 dotted identifier, optionally followed by ``: message``,
# WITHOUT requiring the canonical class suffix. A superset of
# _EXC_CLASS_RE. We use it only to decide whether a traceback *ended*
# (reached its exception line) versus was cut short — not to name the
# class. That keeps "complete but unclassifiable" (e.g. ``StopIteration``,
# a custom ``Halt: …``) labelled <unclassified> rather than <truncated>.
_EXC_TERMINATOR_RE = re.compile(r"^(?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*(?:\s*:\s*.*)?$")


def _isTracebackBodyLine(raw: str) -> bool:
    """Return True if ``raw`` looks like a continuation of a Python
    traceback we're already inside — indented frame lines, blank lines,
    or the standard ``File "…", line N, in func`` and ``  message``
    shapes. Used to greedily extend the captured body.
    """
    if not raw:
        return False
    if raw.startswith((" ", "\t")):
        return True
    if raw.startswith("During handling") or raw.startswith("The above exception"):
        return True
    if _EXC_CLASS_RE.match(raw):
        return True
    return False


# Worker payloads are deserialized in a noisy way; detect tracebacks by the
# canonical leader. We don't try to fully extract the traceback body — the UI
# can render the surrounding lines when expanded.
_TRACEBACK_LEAD = "Traceback (most recent call last):"


def summarizePod(podLogPath: Path) -> PodSummary:
    pod = podLogPath.stem
    group = podGroup(pod)
    summary = PodSummary(
        pod=pod,
        group=group,
        instrument=podInstrument(pod),
        ordinal=podOrdinal(pod),
        nLines=0,
        nWarn=0,
        nError=0,
        nTraceback=0,
        firstTs=None,
        lastTs=None,
    )
    isCarryover = group in _CARRYOVER_GROUPS
    currentExpId: int | None = None
    activeTb: TracebackRecord | None = None
    tbLines: list[str] = []
    for ln in iterPodLines(podLogPath):
        summary.nLines += 1
        if summary.firstTs is None:
            summary.firstTs = ln.timestamp
        summary.lastTs = ln.timestamp
        if ln.level == "warn":
            summary.nWarn += 1
        elif ln.level == "error":
            summary.nError += 1
        # Traceback capture state machine.
        if _TRACEBACK_LEAD in ln.raw:
            if activeTb is not None:
                _finaliseTraceback(activeTb, tbLines, summary)
            activeTb = TracebackRecord(
                pod=pod,
                t=ln.timestamp,
                expId=currentExpId if isCarryover else extractExpId(ln.raw),
                excClass="<unknown>",
                excMessage="",
                body="",
            )
            tbLines = [ln.raw]
            summary.nTraceback += 1
        elif activeTb is not None:
            if _isTracebackBodyLine(ln.raw):
                # Body capture has its own cap; class detection
                # continues to the end of the traceback so we still
                # tag the right exception when a long stack would
                # otherwise overflow the body buffer.
                if len(tbLines) < _TRACEBACK_MAX_LINES:
                    tbLines.append(ln.raw)
                m = _EXC_CLASS_RE.match(ln.raw)
                if m and activeTb.excClass == "<unknown>":
                    activeTb.excClass = m.group("cls").rsplit(".", 1)[-1]
                    activeTb.excMessage = (m.group("msg") or "").strip()[:200]
                    activeTb.reachedTerminator = True
            else:
                # A non-body line ends the traceback. If it's itself a
                # column-0 exception-terminator shape we couldn't strictly
                # classify (StopIteration, a custom Foo: …), the traceback
                # is *complete* — capture the line and mark it so it lands
                # as <unclassified>, not <truncated>. A genuinely foreign
                # interrupt (an INFO line mid-stack) won't match, so it
                # stays <truncated>.
                if activeTb.excClass == "<unknown>" and _EXC_TERMINATOR_RE.match(ln.raw):
                    if len(tbLines) < _TRACEBACK_MAX_LINES:
                        tbLines.append(ln.raw)
                    activeTb.reachedTerminator = True
                _finaliseTraceback(activeTb, tbLines, summary)
                activeTb = None
                tbLines = []
        found = extractExpId(ln.raw)
        if found is not None:
            summary.expIdsSeen.add(found)
            currentExpId = found
        inferred = currentExpId if isCarryover else found
        if inferred is not None:
            firstLast = summary.expIdFirstLast.get(inferred)
            if firstLast is None:
                summary.expIdFirstLast[inferred] = (ln.timestamp, ln.timestamp)
            else:
                summary.expIdFirstLast[inferred] = (firstLast[0], ln.timestamp)
            if m := _WAIT_RE.search(ln.raw):
                summary.expIdWaitSeconds[inferred] = summary.expIdWaitSeconds.get(inferred, 0.0) + float(
                    m.group(1)
                )
        ev = classify(ln)
        if ev is not None:
            summary.events.append(ev)
    # Pod's log ended while still inside a traceback — flush whatever
    # we've collected so it isn't lost.
    if activeTb is not None:
        _finaliseTraceback(activeTb, tbLines, summary)
    return summary


def _finaliseTraceback(record: TracebackRecord, lines: list[str], summary: PodSummary) -> None:
    """Pack `lines` into `record.body` (capped) and attach to `summary`.

    Resolve the transient ``"<unknown>"`` excClass sentinel: a traceback
    that reached its terminating exception line but didn't classify is
    ``"<unclassified>"`` (complete, type unknown); one that ended before
    any terminator is ``"<truncated>"`` (genuinely cut short). See the
    ``excClass`` field doc on :class:`TracebackRecord` for why these are
    kept distinct.
    """
    body = "\n".join(lines)
    if len(body) > _TRACEBACK_MAX_CHARS:
        body = body[:_TRACEBACK_MAX_CHARS] + "\n…(traceback body truncated)"
    record.body = body
    if record.excClass == "<unknown>":
        record.excClass = "<unclassified>" if record.reachedTerminator else "<truncated>"
    summary.tracebacks.append(record)


def summarizeAll(cacheDir: Path) -> list[PodSummary]:
    podsDir = cacheDir / "pods"
    summaries: list[PodSummary] = []
    if not podsDir.exists():
        return summaries
    for podFile in sorted(podsDir.iterdir()):
        if podFile.suffix != ".jsonl":
            continue
        summaries.append(summarizePod(podFile))
    return summaries


# ----- filtering by exposure id --------------------------------------------


def podsTouchingExp(summaries: Iterable[PodSummary], expId: int) -> list[PodSummary]:
    """Return the subset of pod summaries whose logs reference `expId`."""
    return [s for s in summaries if expId in s.expIdsSeen]


def podsForTimeline(summaries: Iterable[PodSummary], expId: int) -> list[PodSummary]:
    """Pods to include in the per-exposure timeline view.

    Includes (a) every pod that explicitly mentioned ``expId`` in its logs
    and (b) every pod that we couldn't classify against any known role
    (``group == "other"``). The ``"other"`` rule is a deliberate safety
    net so that an unknown / new / mis-named role still surfaces — if it
    threw a traceback or warning during the window the user shouldn't have
    to know to look elsewhere for it.
    """
    return [s for s in summaries if expId in s.expIdsSeen or s.group == "other"]
