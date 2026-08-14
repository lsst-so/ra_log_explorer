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
from typing import Callable, Iterable, Iterator

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
_RUN_PREFIX_RE = re.compile(r"^s-(?:lsstcam|latiss|misc)-run-")


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


def isAosPod(pod: str) -> bool:
    """Whether this pod belongs to the AOS half of the night view.

    Deliberately a substring test on the pod *name* rather than anything
    derived from :func:`podGroup`, because it has to be the exact
    complement of :data:`config.NIGHT_AOS_POD_REGEX` — the LogQL filter
    the AOS night fetch pushes down to Loki. If the two ever disagree, a
    pod falls into both halves of the night view or into neither, and the
    "neither" case is silent. ``tests/test_parse.py`` pins them together
    against the real pod list.
    """
    return "aos" in pod


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
    """Return 'LSSTCam' or 'LATISS' from the pod name, or None.

    ``None`` means instrument-neutral (redis, cluster-manager, …) rather
    than unknown, and such pods are attributed to whichever exposure is
    being viewed.
    """
    for inst, needle in (
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


# ----- k8s/events (pod lifecycle) parsing -----------------------------------
#
# The ``k8s/events`` Loki stream is a different shape from app logs: each
# line is a flat ``key=value`` record with a quoted ``msg="…"`` tail, e.g.
#
#   name=… kind=Pod … reason=Started type=Normal count=2 msg="Started container run-aos-worker"
#
# so it gets its own parse + classify path (``classifyK8sEvent``), wholly
# separate from the LSST-log-format ``classify`` above. We surface only the
# lifecycle reasons that explain a pod dropping off the timeline — a
# restart, kill, OOM, or probe failure — and drop the bulk of the chatter
# (image pulls, sandbox setup, scheduling, container create). The specific
# k8s ``reason`` rides along in ``flavor`` for the hover tooltip.
#
# These events carry no dataId: they're pod-global lifecycle facts, not
# per-exposure ones, so ``expId`` is always None. The server includes them
# on any pod already in a timeline, windowed by time (see ``server.
# _summaryToDict``). All kinds share the ``POD_`` prefix so the UI can
# style them as one family.

# key=value, where the value is either a "double-quoted string" (the msg,
# which can contain escaped quotes) or a bare non-space token.
_K8S_KV_RE = re.compile(r'(\w+)=(?:"((?:[^"\\]|\\.)*)"|(\S+))')

# k8s event reasons that mean "this container went down hard" (as opposed
# to the graceful ``Killing`` of a rollout). Mapped to one POD_FAILED kind;
# the precise reason is preserved in the event's ``flavor``.
_POD_DOWN_REASONS: frozenset[str] = frozenset(
    {"Failed", "BackOff", "Evicted", "Preempted", "NodeNotReady", "FailedKillPod"}
)

# k8s reasons that mean "a *pod sandbox* was created here" — the pod
# object itself is new, so any log lines before this point belong to its
# predecessor (a StatefulSet pod keeps its name across a delete/recreate)
# and any container start just after it is a first start, not a restart.
_POD_SANDBOX_REASONS: frozenset[str] = frozenset({"Scheduled", "AddedInterface"})

# How much of a pod instance's *own* logging must precede a "Container
# started" event before we read that event as an in-place restart rather
# than a first start. See :func:`_promoteInPlaceRestarts` for why the
# rule is needed at all; this constant is the one judgement call in it,
# and it was chosen from measurement rather than taste. Over four
# captured nights (20260711 summit, 20260811-13 BTS) there were 1672
# `Started` events, 846 of them with app logs on both sides:
#
#   * the 13 genuine restarts had between 4.8 minutes and 12.3 hours of
#     the same pod instance's logging before them (median 87 minutes);
#   * all 833 others had *zero* — their only preceding line is the
#     `secret-perm-fixer` init container's one-line complaint, emitted
#     in the same second.
#
# So the threshold sits in an empty band two orders of magnitude wide,
# and anything from a few seconds to a few minutes would separate the
# two populations identically.
RESTART_MIN_WORK_S = 60.0

# The lifecycle event kinds, for consumers that want to recognise the
# family without string-prefix sniffing. Kept in sync with the kinds
# emitted by :func:`classifyK8sEvent`.
LIFECYCLE_EVENT_KINDS: frozenset[str] = frozenset(
    {
        "POD_OOMKILLED",
        "POD_KILLED",
        "POD_FAILED",
        "POD_UNHEALTHY",
        "POD_RESTARTED",
        "POD_STARTED",
        "POD_MOUNT_FAILED",
    }
)


def _parseK8sEventFields(raw: str) -> dict[str, str]:
    """Split a ``key=value`` k8s-event line into a dict.

    Quoted values (the ``msg``) keep their interior spaces and have their
    backslash escapes unwound; bare values are taken verbatim. Later keys
    win on the (not-expected) chance of a duplicate.
    """
    fields: dict[str, str] = {}
    for m in _K8S_KV_RE.finditer(raw):
        quoted, bare = m.group(2), m.group(3)
        if quoted is not None:
            value = quoted.replace('\\"', '"').replace("\\\\", "\\")
        else:
            value = bare
        fields[m.group(1)] = value
    return fields


def classifyK8sEvent(pod: str, jsonObj: dict) -> Event | None:
    """Classify one ``k8s/events`` JSONL line into a pod-lifecycle Event.

    Returns ``None`` for the bulk of lifecycle chatter (image pulls,
    scheduling, container *create*) and for events whose involved object
    isn't the Pod itself (a StatefulSet ``SuccessfulCreate`` names the set,
    not the pod, and would mislabel the lane). The ``count`` field — k8s's
    occurrence counter for the reason — is what distinguishes an in-place
    container *restart* (``Started`` with ``count ≥ 2``) from the pod's
    first start.
    """
    raw = jsonObj.get("line", "").rstrip("\n")
    tsStr = jsonObj.get("timestamp", "")
    if not tsStr:
        return None
    try:
        t = _parseTimestamp(tsStr)
    except ValueError:
        return None
    fields = _parseK8sEventFields(raw)
    # Only Pod-object events describe a single pod's lifecycle. ``kind`` is
    # absent on the odd malformed line; treat that as "could be a pod".
    if fields.get("kind", "Pod") != "Pod":
        return None
    reason = fields.get("reason", "")
    if not reason:
        return None
    try:
        count = int(fields.get("count", "1"))
    except ValueError:
        count = 1

    kind: str | None = None
    level = "info"
    if "OOM" in reason:  # OOMKilling (node-pressure) — rare but unambiguous
        kind, level = "POD_OOMKILLED", "error"
    elif reason in _POD_DOWN_REASONS:
        kind, level = "POD_FAILED", "error"
    elif reason == "Unhealthy":  # liveness/readiness probe failed
        kind, level = "POD_UNHEALTHY", "warn"
    elif reason == "FailedMount":
        # The kubelet couldn't mount one of the pod's volumes — the pod is
        # down (or being restarted) until it can, so this explains a gap
        # the same way a restart does. Seen for real as a cluster-wide
        # secret-sync hiccup hitting several running pods at one moment.
        # The kubelet retries on a backoff and emits one event per attempt,
        # so a single incident shows up as a small burst of these markers.
        kind, level = "POD_MOUNT_FAILED", "warn"
    elif reason == "Killing":  # container stopping (graceful rollout, or pre-restart)
        kind, level = "POD_KILLED", "warn"
    elif reason == "Started":
        # count ≥ 2 ⇒ the container has started before in this pod, i.e. it
        # died and was restarted in place — the signal that explains an
        # abrupt mid-work gap in the app log (e.g. an OOM the kernel didn't
        # ship us a message for).
        kind, level = ("POD_RESTARTED", "warn") if count >= 2 else ("POD_STARTED", "info")
    if kind is None:
        return None

    msg = fields.get("msg", "").strip()
    node = fields.get("sourcehost") or fields.get("reportinginstance") or ""
    if count >= 2 and kind == "POD_RESTARTED":
        msg = f"{msg} (restart #{count})" if msg else f"restart #{count}"
    detail = f"{msg}  ·  on {node}" if (msg and node) else (msg or (f"on {node}" if node else reason))
    return Event(pod, t, kind, level, flavor=reason, message=detail, raw=raw)


def readPodLifecycle(eventsLogPath: Path) -> tuple[list[Event], list[dt.datetime]]:
    """Read a pod's lifecycle file into (classified events, sandbox times).

    The sandbox times are the ``Scheduled`` / ``AddedInterface`` moments,
    which :func:`classifyK8sEvent` deliberately drops — they say nothing
    about a pod that is merely running. They matter only to
    :func:`_promoteInPlaceRestarts`, which needs to know when *this* pod
    instance began in order to judge what came before a container start.
    """
    events: list[Event] = []
    sandbox: list[dt.datetime] = []
    if not eventsLogPath.exists():
        return events, sandbox
    pod = eventsLogPath.stem
    with eventsLogPath.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev = classifyK8sEvent(pod, obj)
            if ev is not None:
                events.append(ev)
                continue
            fields = _parseK8sEventFields(obj.get("line", ""))
            if fields.get("reason") in _POD_SANDBOX_REASONS and fields.get("kind", "Pod") == "Pod":
                try:
                    sandbox.append(_parseTimestamp(obj.get("timestamp", "")))
                except ValueError:
                    continue
    sandbox.sort()
    return events, sandbox


@dataclass
class _RestartEvidence:
    """Evidence gathered about one ``POD_STARTED``, to judge if it's a restart.

    ``lower`` is the moment this pod instance came into being (its most
    recent sandbox creation) or ``None`` when that happened before the
    window. Lines at or before it belong to a previous pod of the same
    name and are ignored — which is what stops a rescheduled StatefulSet
    pod from looking like a restart.
    """

    event: Event
    lower: dt.datetime | None
    first: dt.datetime | None = None  # first line of this instance before the start
    last: dt.datetime | None = None  # last such line
    after: bool = False  # did this pod log anything after the start?

    def workBeforeS(self) -> float:
        """Seconds of this pod instance's logging that precede the start."""
        if self.first is None or self.last is None:
            return 0.0
        return (self.last - self.first).total_seconds()

    def isRestart(self) -> bool:
        """Whether the evidence says the container restarted in place.

        Both halves are needed. Without ``after`` a pod that started and
        then went quiet reads as a restart; without the work threshold a
        pod's *first* start reads as one, since an init container's
        preamble lands before it.
        """
        return self.after and self.workBeforeS() >= RESTART_MIN_WORK_S


def _promoteInPlaceRestarts(tracked: list[_RestartEvidence]) -> None:
    """Re-label the ``POD_STARTED`` events that were really restarts.

    Kubernetes' own restart signal is the event's ``count``, and
    :func:`classifyK8sEvent` uses it: ``Started`` with ``count ≥ 2`` is a
    restart. But ``count`` is an aggregation counter on an Event object
    with a one-hour TTL, so it only survives while the *previous* start's
    event does. A crash loop keeps it alive — hence the ``restart #6``
    the fixtures capture — but an isolated restart hours into a pod's
    life gets a fresh Event object with ``count=1``, indistinguishable by
    that rule alone from the pod's first start. That is exactly the shape
    an OOM takes, and it is the shape we most need to see: on 20260813
    the ``count`` rule found 3 of the night's 7 restarts.

    So we settle it with the app log instead, which the events stream
    can't see: a container that was producing output for a sustained
    period, and produces more afterwards, has restarted. The pod's *own*
    logging is what counts — anything before its sandbox was created
    belongs to a predecessor of the same name.
    """
    for tr in tracked:
        if not tr.isRestart():
            continue
        tr.event.kind = "POD_RESTARTED"
        tr.event.level = "warn"
        mins = tr.workBeforeS() / 60.0
        shown = f"{mins / 60:.1f} h" if mins >= 90 else f"{mins:.0f} min"
        # Say what the label rests on: this one is inferred from the log,
        # not read off the event, and a reader deserves to know which.
        tr.event.message = f"{tr.event.message} (restart inferred: {shown} of work before it)"


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


def summarizePod(podLogPath: Path, eventsLogPath: Path | None = None) -> PodSummary:
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
    # Read the lifecycle stream *before* the log, not after: judging
    # whether a "Container started" is really a restart needs to know
    # what the pod was doing either side of it, and the log is streamed
    # once (a night's worth is far too big to hold).
    lifecycle: list[Event] = []
    sandboxTimes: list[dt.datetime] = []
    if eventsLogPath is not None:
        lifecycle, sandboxTimes = readPodLifecycle(eventsLogPath)
    tracked = [
        _RestartEvidence(event=ev, lower=_lastSandboxBefore(sandboxTimes, ev.t))
        for ev in lifecycle
        if ev.kind == "POD_STARTED"
    ]
    for ln in iterPodLines(podLogPath):
        summary.nLines += 1
        # Empty for all but a handful of pods in any window, so this
        # costs one loop-setup per line and nothing else.
        for tr in tracked:
            if tr.lower is not None and ln.timestamp <= tr.lower:
                continue
            if ln.timestamp < tr.event.t:
                if tr.first is None:
                    tr.first = ln.timestamp
                tr.last = ln.timestamp
            else:
                tr.after = True
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
    # Merge in the pod's k8s lifecycle events (restart/kill/OOM markers)
    # from the parallel events stream, if it was fetched. They carry their
    # own timestamps, so re-sort the combined list to keep the per-pod
    # event stream time-ascending for the timeline.
    if lifecycle:
        _promoteInPlaceRestarts(tracked)
        summary.events.extend(lifecycle)
        summary.events.sort(key=lambda e: e.t)
    return summary


def _lastSandboxBefore(sandboxTimes: list[dt.datetime], t: dt.datetime) -> dt.datetime | None:
    """The most recent pod-sandbox creation before ``t``, if any."""
    earlier = [s for s in sandboxTimes if s < t]
    return earlier[-1] if earlier else None


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


def summarizeAll(cacheDir: Path, keep: Callable[[str], bool] | None = None) -> list[PodSummary]:
    """Summarize every pod in a window; ``keep`` filters by pod name.

    The filter is applied before parsing, not after, because the caller
    that uses it is the night view's SFM half: its window holds the whole
    night unfiltered (see :data:`config.NIGHT_VIEWS`), and parsing the
    AOS pods only to discard them would be most of a minute on a summit
    night.
    """
    podsDir = cacheDir / "pods"
    eventsDir = cacheDir / "pods_events"
    summaries: list[PodSummary] = []
    if not podsDir.exists():
        return summaries
    for podFile in sorted(podsDir.iterdir()):
        if podFile.suffix != ".jsonl":
            continue
        if keep is not None and not keep(podFile.stem):
            continue
        # The sibling pods_events/<pod>.jsonl is optional: absent for a pod
        # that had no lifecycle events in the window. (Not for an older
        # cache: those don't survive the schema flush — see caching.md.)
        eventsFile = eventsDir / podFile.name
        summaries.append(summarizePod(podFile, eventsFile if eventsFile.exists() else None))
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
