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

# Friendly grouping for the UI. Order matters for display priority.
POD_GROUPS: list[tuple[str, str]] = [
    ("head", "head-node"),
    ("butler-watcher", "butler-watcher"),
    ("sfm", "sfm-runner"),
    # Order matters: more specific substrings must come first so that
    # `step-1b-aos-worker` isn't mistakenly classified as plain `aos-worker`.
    ("step1b-aos", "step-1b-aos-worker"),
    ("step1b", "step-1b-worker"),
    ("aos", "aos-worker"),
    ("backlog", "backlog-worker"),
    ("mosaic", "mosaic"),
    ("one-off-exprecord", "one-off-exp-record"),
    ("one-off-postisr", "one-off-post-isr"),
    ("one-off-visitimage", "one-off-visit-image"),
    ("guider", "guider-analysis"),
    ("psf-plot", "psf-plotting"),
    ("fwhm-plot", "fwhm-plotting"),
    ("radial-plot", "radial-plotting"),
    ("zernike-plot", "zernike-prediction-plotting"),
    ("metadata-server", "metadata-server"),
    ("cluster-mgr", "cluster-manager"),
]


def podGroup(pod: str) -> str:
    """Return a short label identifying the pod's role."""
    for label, needle in POD_GROUPS:
        if needle in pod:
            return label
    return "other"


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
    return dt.datetime.fromisoformat(iso).astimezone(dt.timezone.utc)


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
    if "HeadProcessController" in line.logger or "processControl" in line.logger:
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
            # `<inst>-<dayObs>-<seq>` — reconstruct expId as <dayObs><seq zero-padded>
            dayObs = int(m.group("dayObs"))
            seq = int(m.group("seq"))
            expId = dayObs * 100000 + seq  # YYYYMMDDSSSSS - dayObs is YYYYMMDD,
            # but the log uses inst-YYYYMMDD-N where N is the seqNum (no zero-padding)
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


# Worker payloads are deserialized in a noisy way; detect tracebacks by the
# canonical leader. We don't try to fully extract the traceback body — the UI
# can render the surrounding lines when expanded.
_TRACEBACK_LEAD = "Traceback (most recent call last):"


def summarizePod(podLogPath: Path) -> PodSummary:
    pod = podLogPath.stem
    summary = PodSummary(
        pod=pod,
        group=podGroup(pod),
        instrument=podInstrument(pod),
        ordinal=podOrdinal(pod),
        nLines=0,
        nWarn=0,
        nError=0,
        nTraceback=0,
        firstTs=None,
        lastTs=None,
    )
    for ln in iterPodLines(podLogPath):
        summary.nLines += 1
        if summary.firstTs is None:
            summary.firstTs = ln.timestamp
        summary.lastTs = ln.timestamp
        if ln.level == "warn":
            summary.nWarn += 1
        elif ln.level == "error":
            summary.nError += 1
        if _TRACEBACK_LEAD in ln.raw:
            summary.nTraceback += 1
        if m := _BARE_EXPID_RE.search(ln.raw):
            try:
                summary.expIdsSeen.add(int(m.group(1)))
            except ValueError:
                pass
        ev = classify(ln)
        if ev is not None:
            summary.events.append(ev)
    return summary


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
