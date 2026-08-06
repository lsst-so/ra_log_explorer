"""Night-mode analysis: roll a set of per-pod summaries up into a
dayObs-wide health view.

The exposure-mode :class:`~.parse.PodSummary` already carries everything
we need (events, tracebacks, per-dataId first/last times), so the
analysis here is pure list-and-dict massaging. Nothing in this module
touches Loki, ConsDB, or disk.

What we surface:

* **Top stats** — counts of distinct dataIds, tracebacks, pods,
  exception classes seen.
* **Errors by exception class** — what failed, and how often.
* **Errors by pod** — which workers are throwing them.
* **First-task-start histogram** — Δshutter of the earliest task
  pickup per dataId. A bimodal or long-tailed distribution is the
  thing to look for.
* **calcZernikes-end histogram** — Δshutter of the latest
  ``*calcZernikes*`` task completion per dataId.
* **Failed dataIds** — one row per (dataId × pod × traceback) so the
  user can click straight into the traceback body.
* **Pod restarts / deaths** — one row per ``POD_*`` lifecycle event
  (from the k8s/events stream), attributed to the dataId the pod was
  processing at that instant so the user can click straight to it. A
  ``POD_RESTARTED`` is an in-place container restart (often an OOM the
  kernel didn't log to us); kills / OOMs / failures sit alongside it.
* **Gather-only dataIds** — dataIds with gather (step1b) activity but no
  step1a precursor. Physically impossible, so a tell that the fetch
  dropped the step1a logs (which also biases the first-task histogram).
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable

from . import parse


@dataclass(frozen=True)
class TopStats:
    nVisitsSeen: int
    nPods: int
    nTracebacks: int
    nDataIdsWithTraceback: int
    nPodsWithTraceback: int
    nDistinctExceptionClasses: int
    # Count of in-place container restarts (``POD_RESTARTED``) across the
    # night — the headline "did pods die mid-processing" number.
    nPodRestarts: int = 0


@dataclass(frozen=True)
class ErrorTypeRow:
    excClass: str
    count: int
    sampleMessage: str  # one representative message for this class


@dataclass(frozen=True)
class PodErrorRow:
    pod: str
    group: str
    count: int


@dataclass(frozen=True)
class FailureRow:
    """One traceback's worth of drilldown metadata."""

    dataId: int | None
    pod: str
    group: str
    excClass: str
    excMessage: str
    offsetS: float | None  # Δshutter (TAI) for this traceback, if known
    tIso: str  # absolute timestamp (UTC)
    bodyKey: str  # stable id so the UI can ask for the body on demand


@dataclass(frozen=True)
class LifecycleRow:
    """One pod-lifecycle event (restart / kill / OOM / fail / unhealthy)
    for the night-view "Pod restarts & deaths" table.

    ``dataId`` is the exposure the pod was *processing* at the moment of
    the event (its most-recently-picked-up dataId at that time), not a
    field on the event itself — lifecycle events are pod-global. That
    attribution is what lets a restart link straight to the visit it
    interrupted. ``offsetS`` is the Δshutter of that dataId, when known.
    """

    kind: str  # POD_RESTARTED / POD_KILLED / POD_OOMKILLED / POD_FAILED / POD_UNHEALTHY
    reason: str  # the underlying k8s reason (Event.flavor)
    pod: str
    group: str
    dataId: int | None  # dataId being processed at the event time, if attributable
    offsetS: float | None  # Δshutter (TAI) for that dataId, if known
    tIso: str  # absolute timestamp (UTC)
    message: str


@dataclass(frozen=True)
class Histogram:
    """A simple equi-width histogram, computed from a list of x-values.

    ``dataIdsByBin`` is an optional parallel list-of-lists giving the
    dataIds contributing to each bin (in ascending-x order within the
    bin). It's used by the UI to let the user drill from a bar straight
    to the per-visit logs.
    """

    label: str
    unit: str
    xMin: float
    xMax: float
    binWidth: float
    counts: list[int] = field(default_factory=list)
    nValues: int = 0  # how many x-values went in
    nDropped: int = 0  # how many we couldn't bin (no shutter close known, etc.)
    dataIdsByBin: list[list[int]] = field(default_factory=list)


def computeTopStats(summaries: Iterable[parse.PodSummary]) -> TopStats:
    summaries = list(summaries)
    visits: set[int] = set()
    tbCount = 0
    podsWithTb: set[str] = set()
    dataIdsWithTb: set[int] = set()
    excClasses: set[str] = set()
    restarts = 0
    for s in summaries:
        visits |= s.expIdsSeen
        tbCount += len(s.tracebacks)
        if s.tracebacks:
            podsWithTb.add(s.pod)
        for tb in s.tracebacks:
            if tb.expId is not None:
                dataIdsWithTb.add(tb.expId)
            excClasses.add(tb.excClass)
        restarts += sum(1 for ev in s.events if ev.kind == "POD_RESTARTED")
    return TopStats(
        nVisitsSeen=len(visits),
        nPods=len(summaries),
        nTracebacks=tbCount,
        nDataIdsWithTraceback=len(dataIdsWithTb),
        nPodsWithTraceback=len(podsWithTb),
        nDistinctExceptionClasses=len(excClasses),
        nPodRestarts=restarts,
    )


def errorsByType(summaries: Iterable[parse.PodSummary]) -> list[ErrorTypeRow]:
    counts: Counter[str] = Counter()
    samples: dict[str, str] = {}
    for s in summaries:
        for tb in s.tracebacks:
            counts[tb.excClass] += 1
            samples.setdefault(tb.excClass, tb.excMessage)
    return [
        ErrorTypeRow(excClass=cls, count=n, sampleMessage=samples.get(cls, ""))
        for cls, n in counts.most_common()
    ]


def errorsByPod(summaries: Iterable[parse.PodSummary]) -> list[PodErrorRow]:
    rows: list[PodErrorRow] = []
    for s in summaries:
        if not s.tracebacks:
            continue
        rows.append(PodErrorRow(pod=s.pod, group=s.group, count=len(s.tracebacks)))
    rows.sort(key=lambda r: (-r.count, r.pod))
    return rows


def makeBodyKey(pod: str, t: dt.datetime) -> str:
    """A stable id the UI can use to ask for one traceback body.

    Reached across module boundaries (the server uses it to match a
    URL parameter back to a captured traceback), so the public name
    avoids the leading-underscore convention that suggests internal-only.
    """
    return f"{pod}@{t.isoformat()}"


def failureRows(
    summaries: Iterable[parse.PodSummary],
    shutterCloseByExpId: dict[int, dt.datetime] | None = None,
) -> list[FailureRow]:
    rows: list[FailureRow] = []
    shutterCloseByExpId = shutterCloseByExpId or {}
    for s in summaries:
        for tb in s.tracebacks:
            offsetS: float | None = None
            if tb.expId is not None and tb.expId in shutterCloseByExpId:
                offsetS = (tb.t - shutterCloseByExpId[tb.expId]).total_seconds()
            rows.append(
                FailureRow(
                    dataId=tb.expId,
                    pod=s.pod,
                    group=s.group,
                    excClass=tb.excClass,
                    excMessage=tb.excMessage,
                    offsetS=offsetS,
                    tIso=tb.t.isoformat(),
                    bodyKey=makeBodyKey(s.pod, tb.t),
                )
            )
    rows.sort(key=lambda r: r.tIso)
    return rows


# The lifecycle kinds the night view surfaces in its restart/death table.
# ``POD_STARTED`` (a pod's first container start) is deliberately excluded:
# every pod emits one at the start of the night, so it's pure noise here —
# only the *re*-starts and deaths carry signal.
_NIGHT_LIFECYCLE_KINDS: frozenset[str] = parse.LIFECYCLE_EVENT_KINDS - {"POD_STARTED"}


def _dataIdProcessedAt(summary: parse.PodSummary, t: dt.datetime) -> int | None:
    """Return the dataId this pod was processing at time ``t``, if any.

    Lifecycle events carry no dataId, so we attribute the event to the
    pod's most-recently-picked-up dataId at that instant — the exposure it
    was working on when it died/restarted. Uses ``expIdFirstLast`` (the
    carryover-aware per-dataId first/last spans): among dataIds first seen
    at or before ``t``, the one with the latest first-seen wins. ``None``
    if the event precedes any dataId this pod logged.
    """
    best: int | None = None
    bestFirst: dt.datetime | None = None
    for dataId, (first, _last) in summary.expIdFirstLast.items():
        if first <= t and (bestFirst is None or first > bestFirst):
            best, bestFirst = dataId, first
    return best


def lifecycleRows(
    summaries: Iterable[parse.PodSummary],
    shutterCloseByExpId: dict[int, dt.datetime] | None = None,
) -> list[LifecycleRow]:
    """One row per pod restart / kill / OOM / failure across the night.

    ``POD_STARTED`` is excluded (see :data:`_NIGHT_LIFECYCLE_KINDS`). Each
    row is attributed to the dataId the pod was processing at the event
    time (:func:`_dataIdProcessedAt`); when that dataId has a known shutter
    close the row also carries its Δshutter offset. Sorted by time.
    """
    shutterCloseByExpId = shutterCloseByExpId or {}
    # Sort on the datetime, not the rendered ``tIso`` string: string order
    # only happens to match chronological order while every event carries the
    # same tz suffix and isoformat's variable-width fractional part lines up.
    dated: list[tuple[dt.datetime, LifecycleRow]] = []
    for s in summaries:
        for ev in s.events:
            if ev.kind not in _NIGHT_LIFECYCLE_KINDS:
                continue
            dataId = _dataIdProcessedAt(s, ev.t)
            offsetS: float | None = None
            if dataId is not None and dataId in shutterCloseByExpId:
                offsetS = (ev.t - shutterCloseByExpId[dataId]).total_seconds()
            dated.append(
                (
                    ev.t,
                    LifecycleRow(
                        kind=ev.kind,
                        reason=ev.flavor or "",
                        pod=s.pod,
                        group=s.group,
                        dataId=dataId,
                        offsetS=offsetS,
                        tIso=ev.t.isoformat(),
                        message=ev.message,
                    ),
                )
            )
    dated.sort(key=lambda pair: pair[0])
    return [row for _t, row in dated]


def tracebackBody(summaries: Iterable[parse.PodSummary], bodyKey: str) -> str | None:
    """Look up one traceback's full body by its stable id."""
    for s in summaries:
        for tb in s.tracebacks:
            if makeBodyKey(s.pod, tb.t) == bodyKey:
                return tb.body
    return None


def firstTaskStartByDataId(
    summaries: Iterable[parse.PodSummary],
) -> dict[int, dt.datetime]:
    """Earliest task pickup time per dataId, across all summaries.

    Uses :class:`~.parse.Event` records of kind ``QUANTUM_PREP``,
    ``WORKER_PICKUP`` or ``WORKER_QG_START`` — whichever fires first
    for each dataId.
    """
    starts: dict[int, dt.datetime] = {}
    startKinds = {"QUANTUM_PREP", "WORKER_PICKUP", "WORKER_QG_START"}
    for s in summaries:
        for ev in s.events:
            if ev.expId is None or ev.kind not in startKinds:
                continue
            existing = starts.get(ev.expId)
            if existing is None or ev.t < existing:
                starts[ev.expId] = ev.t
    return starts


# A gather (step1b) step aggregates the per-detector outputs of its step1a
# precursor, so it physically *cannot* run for a dataId unless step1a ran
# first. Seeing gather activity with no step1a for the same dataId is
# therefore impossible in reality — it means the fetch dropped the step1a
# lines (exactly the grafana/loki#17270 symptom). The pairing is by
# pipeline: AOS gather (step1b-aos) consumes aos-worker output; science
# gather (step1b) consumes sfm output. Both halves of each pair are in the
# same fetch scope (the night fetch is AOS-only, so only the first pair can
# ever fire there), so a missing precursor is a true data-loss tell, not an
# out-of-scope artefact.
GATHER_PRECURSOR_GROUPS: dict[str, str] = {
    "step1b-aos": "aos",
    "step1b": "sfm",
}


def gatherOnlyDataIds(summaries: Iterable[parse.PodSummary]) -> list[int]:
    """Return dataIds with gather (step1b) activity but no step1a precursor.

    Such a dataId is physically impossible — gather can't run without the
    step1a output it aggregates — so it's a reliable signal that the fetch
    silently dropped the precursor's logs. See
    :data:`GATHER_PRECURSOR_GROUPS`. Returned ascending.
    """
    seenByGroup: dict[str, set[int]] = {}
    for s in summaries:
        seenByGroup.setdefault(s.group, set()).update(s.expIdsSeen)
    flagged: set[int] = set()
    for gatherGroup, precursorGroup in GATHER_PRECURSOR_GROUPS.items():
        gathered = seenByGroup.get(gatherGroup, set())
        precursed = seenByGroup.get(precursorGroup, set())
        flagged |= gathered - precursed
    return sorted(flagged)


def calcZernikesEndByDataId(
    summaries: Iterable[parse.PodSummary],
) -> dict[int, dt.datetime]:
    """Latest ``QUANTUM_DONE`` time per dataId where the task label
    contains "calczernikes" (case-insensitive)."""
    ends: dict[int, dt.datetime] = {}
    for s in summaries:
        for ev in s.events:
            if ev.expId is None or ev.kind != "QUANTUM_DONE":
                continue
            if not (ev.taskLabel and "calczernikes" in ev.taskLabel.lower()):
                continue
            existing = ends.get(ev.expId)
            if existing is None or ev.t > existing:
                ends[ev.expId] = ev.t
    return ends


def buildHistogram(
    label: str,
    unit: str,
    offsetsS: list[float],
    nDroppedNoTZero: int = 0,
    nBins: int = 30,
    dataIds: list[int] | None = None,
) -> Histogram:
    """Build a 30-bin equi-width histogram over ``offsetsS``.

    If ``offsetsS`` is empty we return an empty histogram (xMin == xMax,
    binWidth == 1) so the consumer can still render a placeholder.

    If ``dataIds`` is supplied it must be parallel to ``offsetsS`` —
    the result's :attr:`Histogram.dataIdsByBin` will then carry the
    dataIds bucketed alongside their counts. Within each bin the
    dataIds are sorted by ascending offset so the eye lands on the
    outliers immediately.
    """
    if not offsetsS:
        return Histogram(
            label=label,
            unit=unit,
            xMin=0.0,
            xMax=0.0,
            binWidth=1.0,
            counts=[],
            nValues=0,
            nDropped=nDroppedNoTZero,
            dataIdsByBin=[],
        )
    if dataIds is not None and len(dataIds) != len(offsetsS):
        raise ValueError(f"len(dataIds)={len(dataIds)} doesn't match len(offsetsS)={len(offsetsS)}")
    lo = min(offsetsS)
    hi = max(offsetsS)
    if lo == hi:
        # All values identical — fall back to a single bin centred there.
        singleBin = [list(dataIds)] if dataIds is not None else []
        return Histogram(
            label=label,
            unit=unit,
            xMin=lo,
            xMax=hi,
            binWidth=1.0,
            counts=[len(offsetsS)],
            nValues=len(offsetsS),
            nDropped=nDroppedNoTZero,
            dataIdsByBin=singleBin,
        )
    width = (hi - lo) / nBins
    counts = [0] * nBins
    # Build per-bin lists of (offset, dataId) so we can sort within bin
    # and ship just the dataIds out.
    binMembers: list[list[tuple[float, int]]] = [[] for _ in range(nBins)]
    for idx, x in enumerate(offsetsS):
        i = int((x - lo) / width)
        if i == nBins:  # right edge case
            i = nBins - 1
        counts[i] += 1
        if dataIds is not None:
            binMembers[i].append((x, dataIds[idx]))
    dataIdsByBin: list[list[int]] = []
    if dataIds is not None:
        for bm in binMembers:
            bm.sort(key=lambda pair: pair[0])
            dataIdsByBin.append([did for _, did in bm])
    return Histogram(
        label=label,
        unit=unit,
        xMin=lo,
        xMax=hi,
        binWidth=width,
        counts=counts,
        nValues=len(offsetsS),
        nDropped=nDroppedNoTZero,
        dataIdsByBin=dataIdsByBin,
    )


def computeDeltaShutterOffsets(
    timesByDataId: dict[int, dt.datetime],
    shutterCloseByExpId: dict[int, dt.datetime],
) -> tuple[list[float], list[int], int]:
    """Convert ``{dataId: timestamp}`` into parallel Δshutter / dataId lists.

    Returns ``(offsetsS, dataIds, nDropped)``: ``offsetsS[i]`` is the
    Δshutter offset for ``dataIds[i]``, and ``nDropped`` counts the
    dataIds we couldn't resolve a shutter close for. The two lists are
    parallel and same-length so the histogram builder can attribute each
    bin back to the contributing dataIds.
    """
    offsets: list[float] = []
    dataIds: list[int] = []
    nDropped = 0
    for dataId, t in timesByDataId.items():
        close = shutterCloseByExpId.get(dataId)
        if close is None:
            nDropped += 1
            continue
        offsets.append((t - close).total_seconds())
        dataIds.append(dataId)
    return offsets, dataIds, nDropped
