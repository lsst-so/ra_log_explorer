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
class Histogram:
    """A simple equi-width histogram, computed from a list of x-values."""

    label: str
    unit: str
    xMin: float
    xMax: float
    binWidth: float
    counts: list[int] = field(default_factory=list)
    nValues: int = 0  # how many x-values went in
    nDropped: int = 0  # how many we couldn't bin (no shutter close known, etc.)


def computeTopStats(summaries: Iterable[parse.PodSummary]) -> TopStats:
    summaries = list(summaries)
    visits: set[int] = set()
    tbCount = 0
    podsWithTb: set[str] = set()
    dataIdsWithTb: set[int] = set()
    excClasses: set[str] = set()
    for s in summaries:
        visits |= s.expIdsSeen
        tbCount += len(s.tracebacks)
        if s.tracebacks:
            podsWithTb.add(s.pod)
        for tb in s.tracebacks:
            if tb.expId is not None:
                dataIdsWithTb.add(tb.expId)
            excClasses.add(tb.excClass)
    return TopStats(
        nVisitsSeen=len(visits),
        nPods=len(summaries),
        nTracebacks=tbCount,
        nDataIdsWithTraceback=len(dataIdsWithTb),
        nPodsWithTraceback=len(podsWithTb),
        nDistinctExceptionClasses=len(excClasses),
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


def _bodyKey(pod: str, t: dt.datetime) -> str:
    """A stable id the UI can use to ask for one traceback body."""
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
                    bodyKey=_bodyKey(s.pod, tb.t),
                )
            )
    rows.sort(key=lambda r: r.tIso)
    return rows


def tracebackBody(summaries: Iterable[parse.PodSummary], bodyKey: str) -> str | None:
    """Look up one traceback's full body by its stable id."""
    for s in summaries:
        for tb in s.tracebacks:
            if _bodyKey(s.pod, tb.t) == bodyKey:
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
) -> Histogram:
    """Build a 30-bin equi-width histogram over ``offsetsS``.

    If ``offsetsS`` is empty we return an empty histogram (xMin == xMax,
    binWidth == 1) so the consumer can still render a placeholder.
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
        )
    lo = min(offsetsS)
    hi = max(offsetsS)
    if lo == hi:
        # All values identical — fall back to a single bin centred there.
        return Histogram(
            label=label,
            unit=unit,
            xMin=lo,
            xMax=hi,
            binWidth=1.0,
            counts=[len(offsetsS)],
            nValues=len(offsetsS),
            nDropped=nDroppedNoTZero,
        )
    width = (hi - lo) / nBins
    counts = [0] * nBins
    for x in offsetsS:
        i = int((x - lo) / width)
        if i == nBins:  # right edge case
            i = nBins - 1
        counts[i] += 1
    return Histogram(
        label=label,
        unit=unit,
        xMin=lo,
        xMax=hi,
        binWidth=width,
        counts=counts,
        nValues=len(offsetsS),
        nDropped=nDroppedNoTZero,
    )


def computeDeltaShutterOffsets(
    timesByDataId: dict[int, dt.datetime],
    shutterCloseByExpId: dict[int, dt.datetime],
) -> tuple[list[float], int]:
    """Convert ``{dataId: timestamp}`` into a flat list of Δshutter offsets.

    Returns ``(offsetsS, nDropped)`` where ``nDropped`` counts the
    dataIds we couldn't resolve a shutter close for.
    """
    offsets: list[float] = []
    nDropped = 0
    for dataId, t in timesByDataId.items():
        close = shutterCloseByExpId.get(dataId)
        if close is None:
            nDropped += 1
            continue
        offsets.append((t - close).total_seconds())
    return offsets, nDropped
