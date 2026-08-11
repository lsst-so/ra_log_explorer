"""Stdlib HTTP server: timeline UI + JSON API + on-demand fetches.

The server runs against a long-lived :class:`ServerContext` that holds:

  * three LRU-ordered dicts of loaded states (exposure / night / range),
  * a :class:`~ra_log_explorer.jobs.JobManager` for fetch jobs the user
    kicks off from the home page,
  * the single ``stateLock`` that guards the keyed-state dicts.

HTTP surface (see ``architecture/architecture.md`` for the full schema;
every route is mounted under ``ServerContext.basePath``):

  GET    /healthz                                 readiness probe; touches no state
  GET    /                                        timeline.html (home/admin/explore/night SPA)
  GET    /static/*                                static assets
  GET    /api/summary?dataId=<>[&instrument=<>]   view-state lookup by exposure…
                     |?dayObs=<>                  …by dayObs…
                     |?rangeStart=&rangeStop=[&dataId=]  …or by range (+ one exposure in it)
  GET    /api/pod/<pod>?dataId=<>[&instrument=<>] full parsed log for one pod
                       |?dayObs=<>|?rangeStart=&rangeStop=&dataId=
  GET    /api/night/traceback/<bodyKey>?dayObs=<> per-failure drilldown
  GET    /api/cache                               list of cached windows on disk
  DELETE /api/cache                               delete the entire cache
  DELETE /api/cache/<cluster>/<ns>/<slug>[/<pods=…>]  delete one cached window
  GET    /api/exposure-time/<dataId>[?instrument=<>]  dataId -> ConsDB exposure record (TAI t-zero)
  GET    /api/site                                the one site this server serves (read-only)
  GET    /api/live                                live poller snapshot ({enabled: false} when off)
  POST   /api/fetch                               start an exposure fetch; returns {jobId}
  POST   /api/fetch-night                         start a night fetch; returns {jobId}
  POST   /api/fetch-range                         start a range fetch; returns {jobId}
  GET    /api/fetch/<id>/status                   JSON snapshot of a fetch job
  GET    /api/fetch/<id>/progress                 SSE stream of fetch progress events
"""

from __future__ import annotations

import datetime as dt
import json
import mimetypes
import re
import shutil
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import exposureTimes, night
from . import parse as parser
from .config import (
    DEFAULT_USERNAME,
    DEFAULT_WINDOW_AFTER_S,
    DEFAULT_WINDOW_BEFORE_S,
    DEFAULT_WORKERS,
    MAX_CACHE_BYTES,
    MAX_RANGE_SPAN,
    NIGHT_AOS_POD_REGEX,
    FetchSpec,
    cache_root,
    dayObsEndUtc,
    dayObsStartUtc,
)
from .fetch import (
    META_NAME,
    PARTIAL_FLAG,
    addExposureToCache,
    cacheDuSizeBytes,
    dropLiveSidecarsUnder,
    evictToFit,
    getCacheExposureIds,
    getCacheLastViewed,
    getCacheRange,
    getCacheRangeInstrument,
    loadCacheMeta,
    loadPodLogPath,
    markCacheRange,
    markCacheViewed,
)
from .jobs import FetchJob, JobManager
from .live import LiveNightManager
from .sites import Site, SitesConfigError, siteByCluster, siteByName

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"


# Palette tuned to be visually distinguishable on white. The first two
# entries are pinned to `isr` and `calibrateImage` so the most common bars
# always get the same colour; everything else is assigned by position in
# sorted order, guaranteeing zero collisions until we exceed the palette
# size. The full LSSTCam SFM+AOS step1a/step1b pipeline graph today emits
# ~16 distinct task labels, well under the 24 here.
_TASK_PALETTE = [
    "#d4801f",  # 0 orange (pinned: isr)
    "#1a9c8c",  # 1 teal   (pinned: calibrateImage)
    "#1d6fb0",  # 2 blue
    "#6a4ea3",  # 3 purple
    "#c1252b",  # 4 red
    "#4d9221",  # 5 green
    "#c41a85",  # 6 magenta
    "#a05f00",  # 7 brown
    "#168aad",  # 8 cyan
    "#842cad",  # 9 violet
    "#d4b500",  # 10 gold
    "#8b1ab3",  # 11 deep purple
    "#5b8a3f",  # 12 olive
    "#306b6b",  # 13 dark teal
    "#a52a2a",  # 14 dark red
    "#d96a9d",  # 15 pink
    "#005f73",  # 16 dark cyan
    "#9b59b6",  # 17 mauve
    "#27ae60",  # 18 emerald
    "#e67e22",  # 19 amber
    "#7f7f7f",  # 20 grey
    "#2c3e50",  # 21 slate
    "#16a085",  # 22 sea green
    "#34495e",  # 23 charcoal
]
_TASK_COLOR_PINNED = {
    "isr": _TASK_PALETTE[0],
    "calibrateImage": _TASK_PALETTE[1],
}


def _assignTaskColors(tasks: list[str]) -> dict[str, str]:
    """Return a deterministic {task: hex} mapping with no collisions.

    Sort order is alphabetical for stability across re-renders within a
    single run. We do not try to make the mapping stable across different
    *exposures* — pin the few important tasks in ``_TASK_COLOR_PINNED`` if
    cross-exposure consistency matters for that one.

    Pinned palette entries are only excluded from the rotation when the
    pinned task is actually present in ``tasks``; otherwise the full
    palette is available, which matters when a view doesn't include
    ``isr`` / ``calibrateImage``.
    """
    result: dict[str, str] = {}
    others: list[str] = []
    for t in sorted(tasks):
        if t in _TASK_COLOR_PINNED:
            result[t] = _TASK_COLOR_PINNED[t]
        else:
            others.append(t)
    usedPinned = set(result.values())
    available = [c for c in _TASK_PALETTE if c not in usedPinned]
    for i, t in enumerate(others):
        result[t] = available[i % len(available)]
    return result


@dataclass
class ServerState:
    """A loaded exposure's worth of parsed data."""

    cacheDir: Path
    cacheBytes: int
    meta: dict  # cache meta produced by fetch.fetchAll
    summaries: list[parser.PodSummary]
    expId: int
    tZero: dt.datetime
    # Name of the site this exposure belongs to ("summit" | "bts" | …).
    # Drives ConsDB lookups and per-site cache scoping; populated at
    # fetch time from the request body's `site` field (or derived from
    # the cache path's cluster component when rehydrating from disk).
    siteName: str = ""
    # The instrument this exposure belongs to (lowercase, e.g.
    # "lsstcam"). Part of the exposure's identity — the same dataId
    # names a different exposure per instrument — so it scopes which
    # pods the timeline attributes work to and lets /api/summary refuse
    # to serve this state to a request pinned to a different instrument.
    # None only for states built before the instrument was known.
    instrument: str | None = None
    # Curated ConsDB exposure record for this dataId (filter, exp time,
    # image type, program, reason, …) — drives the explore-view info box.
    # None if ConsDB never resolved it (no token, unknown dataId).
    exposureInfo: exposureTimes.ExposureRecord | None = None
    referencePoints: list[dict] = field(default_factory=list)


@dataclass
class NightState:
    """A loaded dayObs's worth of parsed AOS-pod data."""

    cacheDir: Path
    cacheBytes: int
    meta: dict
    summaries: list[parser.PodSummary]
    dayObs: int
    startTime: dt.datetime  # noon UTC of dayObs (start of dayObs)
    endTime: dt.datetime  # noon UTC of dayObs + 1
    # See ``ServerState.siteName``.
    siteName: str = ""
    # Lazily populated dataId -> shutter-close UTC datetime, used to
    # turn task event timestamps into Δshutter offsets for histograms.
    shutterCloseByExpId: dict[int, dt.datetime] = field(default_factory=dict)
    # dataId -> curated ConsDB exposure record, resolved alongside the
    # shutter closes — used for the dataId-link tooltips in the night view.
    exposureInfoByExpId: dict[int, exposureTimes.ExposureRecord] = field(default_factory=dict)


@dataclass
class RangeState:
    """A loaded contiguous range of exposures, fetched as one wide window.

    Structurally this is one exposure-style cache (all pods, single
    window) parsed once into ``summaries`` and held in memory. The
    per-dataId timeline for any exposure in the range is computed on
    demand from these shared summaries, anchored at that dataId's own
    shutter close (``shutterCloseByExpId``) — no re-fetch, no re-parse.
    """

    cacheDir: Path
    cacheBytes: int
    meta: dict
    summaries: list[parser.PodSummary]
    startId: int
    stopId: int
    fromTime: dt.datetime  # fetch window start (UTC)
    toTime: dt.datetime  # fetch window end (UTC)
    # See ``ServerState.siteName``.
    siteName: str = ""
    # See ``ServerState.instrument`` — a range is a run of *one*
    # instrument's exposures, and its server-side shutter-close batch
    # must resolve against that instrument's table only.
    instrument: str | None = None
    # dataId -> shutter-close (t₀) UTC datetime for every exposure in
    # [startId, stopId] that ConsDB knew about. Ids absent here are the
    # "skipped" integers the user was warned to expect.
    shutterCloseByExpId: dict[int, dt.datetime] = field(default_factory=dict)
    # dataId -> curated ConsDB exposure record, resolved alongside the
    # shutter closes — used for the navigator chip tooltips.
    exposureInfoByExpId: dict[int, exposureTimes.ExposureRecord] = field(default_factory=dict)


def rangeKey(startId: int, stopId: int) -> str:
    """The dict key / URL identifier for a loaded range."""
    return f"{startId}-{stopId}"


# How many parsed exposures / nights we keep loaded in memory at once.
# Big enough to support a handful of open tabs, small enough that we
# don't accumulate megabytes per loaded state. Oldest by last-access
# wins eviction when we go over.
_MAX_LOADED_STATES = 8


@dataclass
class ServerContext:
    """Long-lived per-process state shared between the handler threads.

    Multiple exposures and nights can be loaded at once — one per open
    tab. Each maps a key (exposureId / dayObs) to its parsed state.
    Access is LRU-ordered so the least-recently-used entries are first
    to be evicted when we go over :data:`_MAX_LOADED_STATES`.

    ``sites`` is the catalog (see :mod:`.sites`), loaded once at process
    start; ``siteName`` picks the one entry this process serves.
    """

    jobs: JobManager
    sites: list[Site] = field(default_factory=list)
    siteName: str = ""
    # The live night poller, when live mode is enabled (deployments set
    # RA_LOG_EXPLORER_LIVE_POLL_S > 0). None means /api/live reports
    # {enabled: false} and nothing else changes.
    live: "LiveNightManager | None" = None
    # URL prefix the app is served under (``""`` at the root). Stripped off
    # every incoming request path before routing, and substituted into the
    # HTML so the browser asks for the prefixed URLs back.
    basePath: str = ""
    exposureStates: "OrderedDict[int, ServerState]" = field(default_factory=OrderedDict)
    nightStates: "OrderedDict[int, NightState]" = field(default_factory=OrderedDict)
    # Keyed by ``rangeKey(startId, stopId)``.
    rangeStates: "OrderedDict[str, RangeState]" = field(default_factory=OrderedDict)

    def site(self) -> Site:
        """Return the one site this server serves.

        Which observatory's logs are on offer is a property of where the
        server runs, not something a request gets to choose: a deployment
        on manke means BTS and one on yagan means the summit. The catalog
        may still list several — that's what lets a laptop point at either
        — but ``--site`` picks among them once, at startup, and nothing
        afterwards can change it.
        """
        return siteByName(self.sites, self.siteName)

    def getExposureState(self, expId: int) -> ServerState | None:
        s = self.exposureStates.get(expId)
        if s is not None:
            self.exposureStates.move_to_end(expId)
        return s

    def getNightState(self, dayObs: int) -> NightState | None:
        s = self.nightStates.get(dayObs)
        if s is not None:
            self.nightStates.move_to_end(dayObs)
        return s

    def putExposureState(self, state: ServerState) -> None:
        self.exposureStates[state.expId] = state
        self.exposureStates.move_to_end(state.expId)
        while len(self.exposureStates) > _MAX_LOADED_STATES:
            self.exposureStates.popitem(last=False)

    def putNightState(self, state: NightState) -> None:
        self.nightStates[state.dayObs] = state
        self.nightStates.move_to_end(state.dayObs)
        while len(self.nightStates) > _MAX_LOADED_STATES:
            self.nightStates.popitem(last=False)

    def getRangeState(self, key: str) -> RangeState | None:
        s = self.rangeStates.get(key)
        if s is not None:
            self.rangeStates.move_to_end(key)
        return s

    def putRangeState(self, state: RangeState) -> None:
        key = rangeKey(state.startId, state.stopId)
        self.rangeStates[key] = state
        self.rangeStates.move_to_end(key)
        while len(self.rangeStates) > _MAX_LOADED_STATES:
            self.rangeStates.popitem(last=False)

    def evictByCacheDir(self, target: Path) -> None:
        """Evict any loaded state whose cacheDir matches ``target``.

        Used when a cache window is deleted from disk — keeping the
        stale parse around would surface 404s and stale data to any
        tab still pointed at that key.
        """
        targetR = target.resolve()
        staleExp = [k for k, v in self.exposureStates.items() if v.cacheDir.resolve() == targetR]
        for k in staleExp:
            del self.exposureStates[k]
        staleNight = [k for k, v in self.nightStates.items() if v.cacheDir.resolve() == targetR]
        for k in staleNight:
            del self.nightStates[k]
        staleRange = [rk for rk, v in self.rangeStates.items() if v.cacheDir.resolve() == targetR]
        for rk in staleRange:
            del self.rangeStates[rk]


def _toJsonable(obj: Any) -> Any:
    if isinstance(obj, dt.datetime):
        return obj.isoformat()
    if isinstance(obj, set):
        return sorted(obj)
    if isinstance(obj, Path):
        return str(obj)
    if is_dataclass(obj) and not isinstance(obj, type):
        return _toJsonable(asdict(obj))
    if isinstance(obj, dict):
        return {k: _toJsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_toJsonable(x) for x in obj]
    return obj


def _eventToDict(ev: parser.Event, tZero: dt.datetime) -> dict:
    return {
        "pod": ev.pod,
        "t": ev.t.isoformat(),
        "offsetS": (ev.t - tZero).total_seconds(),
        "kind": ev.kind,
        "level": ev.level,
        "expId": ev.expId,
        "detector": ev.detector,
        "visit": ev.visit,
        "who": ev.who,
        "taskLabel": ev.taskLabel,
        "durationS": ev.durationS,
        "flavor": ev.flavor,
        "message": ev.message,
        "raw": ev.raw,
    }


# Worker groups we expect to emit a canonical finish event (QUANTUM_DONE
# / WORKER_REPORT_* / WORKER_BINNED_*) for the dataId they processed.
# Only these get the "window too short" flag — for plotters / one-offs /
# gather workers / control-plane pods the finish pattern varies or
# isn't emitted at all, so flagging would just be guessing.
#
# We don't check for a "truncated start" at all: the fetch window opens
# strictly before shutter close, processing cannot start before then,
# so by construction the pickup is always inside the window.
_FINISH_EXPECTED_GROUPS: frozenset[str] = frozenset({"sfm", "aos", "step1b", "step1b-aos", "backlog"})


def _summaryToDict(s: parser.PodSummary, tZero: dt.datetime, expId: int) -> dict:
    """Build the per-pod payload for the timeline view.

    Events that explicitly reference `expId` are always included. Generic
    WARN/ERROR events (which carry no expId by construction) are included
    only if they fall inside the temporal window where this pod was working
    on the target exposure — defined as the interval between the first and
    last explicitly-relevant event, padded by a few seconds on either side.
    This keeps unrelated warnings from the previous/next exposure from
    cluttering the per-pod timeline.
    """
    targeted = [ev for ev in s.events if ev.expId == expId]
    if targeted:
        window = (
            targeted[0].t - dt.timedelta(seconds=3),
            targeted[-1].t + dt.timedelta(seconds=3),
        )
    else:
        # No classified events for this dataId in this pod (e.g. an
        # 'other'-group pod, or one that only mentioned the id in passing).
        # Anchor the untagged-event window on t₀ instead of keeping
        # *everything* — that matters whenever the cache window is much
        # wider than one exposure (range mode, or a superset-reuse
        # single-exposure view), where "keep all untagged" would pull the
        # whole span's warnings into this one dataId's timeline.
        window = (
            tZero - dt.timedelta(seconds=DEFAULT_WINDOW_BEFORE_S),
            tZero + dt.timedelta(seconds=DEFAULT_WINDOW_AFTER_S),
        )

    # Pod-lifecycle markers (restart/kill/OOM, kind ``POD_*``) are global to
    # the pod, not keyed to a dataId — and a death typically lands a few
    # seconds *after* the last work line, outside the tight per-dataId
    # window. Keep them on the lane whenever they fall in the broad exposure
    # window, so "the pod died while working on this exposure" stays visible.
    lifecycleWindow = (
        tZero - dt.timedelta(seconds=DEFAULT_WINDOW_BEFORE_S),
        tZero + dt.timedelta(seconds=DEFAULT_WINDOW_AFTER_S),
    )

    relevant: list[parser.Event] = []
    for ev in s.events:
        if ev.kind in parser.LIFECYCLE_EVENT_KINDS:
            if lifecycleWindow[0] <= ev.t <= lifecycleWindow[1]:
                relevant.append(ev)
            continue
        if ev.expId == expId:
            relevant.append(ev)
            continue
        if ev.expId is not None:
            continue  # explicitly tagged to a different exposure
        # Untagged event (typically a generic WARN/ERROR). Keep iff it
        # falls inside this pod's working window for the target exposure.
        if window[0] <= ev.t <= window[1]:
            relevant.append(ev)

    # ----- per-pod summary stats for the hover tooltip ---------------------
    firstRelevant = s.expIdFirstLast.get(expId)
    firstRelOffsetS: float | None = None
    lastRelOffsetS: float | None = None
    relevantDurationS: float | None = None
    if firstRelevant is not None:
        firstRelOffsetS = (firstRelevant[0] - tZero).total_seconds()
        lastRelOffsetS = (firstRelevant[1] - tZero).total_seconds()
        relevantDurationS = (firstRelevant[1] - firstRelevant[0]).total_seconds()

    qgBuildSeconds: float | None = None
    for ev in s.events:
        if ev.expId == expId and ev.kind == "WORKER_QG_BUILT" and ev.durationS is not None:
            qgBuildSeconds = ev.durationS
            break

    waitSeconds = s.expIdWaitSeconds.get(expId)

    # "Window too short" heuristic — event-based. A worker pod that
    # processed this dataId should emit at least one canonical finish
    # event (QUANTUM_DONE / WORKER_REPORT_* / any WORKER_BINNED_*). If
    # the pod has the dataId in its log but no finish event for it, the
    # fetch window probably cut off before the work completed.
    looksTruncatedEnd = False
    if s.group in _FINISH_EXPECTED_GROUPS and expId in s.expIdsSeen:
        hasFinish = any(
            ev.expId == expId
            and (
                ev.kind == "QUANTUM_DONE"
                or ev.kind.startswith("WORKER_REPORT_")
                or ev.kind.startswith("WORKER_BINNED_")
            )
            for ev in s.events
        )
        looksTruncatedEnd = not hasFinish

    return {
        "pod": s.pod,
        "group": s.group,
        "instrument": s.instrument,
        "ordinal": s.ordinal,
        "nLines": s.nLines,
        "nWarn": s.nWarn,
        "nError": s.nError,
        "nTraceback": s.nTraceback,
        "firstTs": s.firstTs.isoformat() if s.firstTs else None,
        "lastTs": s.lastTs.isoformat() if s.lastTs else None,
        "firstRelevantOffsetS": firstRelOffsetS,
        "lastRelevantOffsetS": lastRelOffsetS,
        "relevantDurationS": relevantDurationS,
        "qgBuildSeconds": qgBuildSeconds,
        "waitSeconds": waitSeconds,
        "looksTruncatedEnd": looksTruncatedEnd,
        "events": [_eventToDict(ev, tZero) for ev in relevant],
    }


def _podBelongsToInstrument(podInstrument: str | None, instrument: str | None) -> bool:
    """Whether a pod belongs on a view pinned to ``instrument``.

    A dataId is only unique within one instrument, so an LSSTCam pod
    that logged id N is talking about a *different exposure* than the
    LATISS view of id N — cross-instrument pods must never be
    attributed. Instrument-neutral pods (name carries no instrument:
    redis, squid, misc) always belong, and when either side is unknown
    we keep the pod rather than silently hide work.
    """
    if instrument is None or podInstrument is None:
        return True
    return podInstrument.lower() == instrument.lower()


def _buildSummaryPayload(state: ServerState) -> dict:
    matchingSummaries = [
        s
        for s in parser.podsForTimeline(state.summaries, state.expId)
        if _podBelongsToInstrument(s.instrument, state.instrument)
    ]
    refs = list(state.referencePoints)
    # Head node's first acknowledgement of this exposure — the moment
    # ButlerWatcher+head observed it as "ready to process". Useful for
    # subtracting out readout + Butler ingest latency.
    firstDefineVisit: parser.Event | None = None
    for s in matchingSummaries:
        if s.group != "head":
            continue
        for ev in s.events:
            if ev.expId != state.expId:
                continue
            if ev.kind == "HEAD_DEFINE_VISIT" and (firstDefineVisit is None or ev.t < firstDefineVisit.t):
                firstDefineVisit = ev
    if firstDefineVisit is not None:
        refs.append(
            {
                "label": "head node first defined visit",
                "t": firstDefineVisit.t.isoformat(),
                "offsetS": (firstDefineVisit.t - state.tZero).total_seconds(),
                "source": "head",
            }
        )

    # Discover every distinct task label across the relevant pods so we can
    # emit a collision-free colour map for the timeline + dynamic legend.
    taskLabels: set[str] = set()
    for s in matchingSummaries:
        for ev in s.events:
            if ev.taskLabel:
                taskLabels.add(ev.taskLabel)
    taskColors = _assignTaskColors(sorted(taskLabels))

    return {
        "loaded": True,
        "mode": "exposure",
        "site": state.siteName,
        "instrument": state.instrument,
        "expId": state.expId,
        "tZero": state.tZero.isoformat(),
        # Curated ConsDB exposure record (filter, exp time, image type,
        # program, reason, …) for the explore-view info box. None if it
        # was never resolved (no token / unknown dataId).
        "exposure": state.exposureInfo,
        "cacheDir": str(state.cacheDir),
        "cacheBytes": state.cacheBytes,
        "meta": _toJsonable(state.meta),
        "referencePoints": refs,
        "taskColors": taskColors,
        "groupLabels": parser.groupLabels(),
        "pods": [_summaryToDict(s, state.tZero, state.expId) for s in matchingSummaries],
        "podsAll": [
            {"pod": s.pod, "group": s.group, "nLines": s.nLines, "nWarn": s.nWarn, "nError": s.nError}
            for s in state.summaries
            if _podBelongsToInstrument(s.instrument, state.instrument)
        ],
    }


def _podLinesPayload(cacheDir: Path, pod: str, anchor: dt.datetime) -> dict:
    """The line-by-line payload for one pod, with ``offsetS`` from ``anchor``."""
    logPath = loadPodLogPath(cacheDir, pod)
    isCarryover = parser.podGroup(pod) in parser.carryoverGroups()
    currentExpId: int | None = None
    lines: list[dict] = []
    for ln in parser.iterPodLines(logPath):
        found = parser.extractExpId(ln.raw)
        if found is not None:
            currentExpId = found
        # Non-carryover pods (head, metadata-server*, butler-watcher, ...)
        # report only the id explicitly present on this line, since they
        # interleave many dataIds in a single stream.
        inferred = currentExpId if isCarryover else found
        lines.append(
            {
                "t": ln.timestamp.isoformat(),
                "offsetS": (ln.timestamp - anchor).total_seconds(),
                "level": ln.level,
                "logger": ln.logger,
                "function": ln.function,
                "message": ln.message,
                "raw": ln.raw,
                "expId": inferred,
            }
        )
    return {"pod": pod, "lines": lines}


def _podDetail(state: ServerState, pod: str) -> dict:
    return _podLinesPayload(state.cacheDir, pod, state.tZero)


def _podDetailForNight(state: NightState, pod: str) -> dict:
    """Same shape as :func:`_podDetail`, but ``offsetS`` is measured from
    the dayObs start (noon UTC) rather than from a single shutter close —
    there is no per-pod shutter close in night mode.
    """
    return _podLinesPayload(state.cacheDir, pod, state.startTime)


# ----- night payload --------------------------------------------------------


def _neededDataIdsForNight(
    summaries: Iterable[parser.PodSummary],
    firstStarts: dict[int, dt.datetime] | None = None,
    czEnds: dict[int, dt.datetime] | None = None,
) -> set[int]:
    """Return the union of dataIds the night view will need a shutter
    close for. Shared by the prefetch path (so it knows what to look
    up) and the payload build (so the ``nMissingShutterClose`` stat
    matches what the prefetch actually attempted).

    The three sources are: first-task-start times per dataId, the
    calcZernikes end times per dataId, and any dataId carrying a
    traceback. ``firstStarts`` / ``czEnds`` can be passed in by callers
    that have already computed them (the payload build does, to feed
    the histograms) — otherwise we compute them locally.
    """
    summaries = list(summaries)
    if firstStarts is None:
        firstStarts = night.firstTaskStartByDataId(summaries)
    if czEnds is None:
        czEnds = night.calcZernikesEndByDataId(summaries)
    needIds: set[int] = set(firstStarts) | set(czEnds)
    for s in summaries:
        for tb in s.tracebacks:
            if tb.expId is not None:
                needIds.add(tb.expId)
    return needIds


def _prefetchNightShutterCloses(
    state: NightState, summaries: Iterable[parser.PodSummary], job: FetchJob, site: Site
) -> None:
    """Populate ``state.shutterCloseByExpId`` for everything the night
    view will need to render histograms + failure Δshutter offsets.

    Runs as part of the night-fetch job's parsing phase so the
    /api/summary request that follows is instant. We:

    * collect every dataId we'll need to plot,
    * read whatever's already in the on-disk per-site exposure-time
      cache,
    * batch-query ``site.consdbUrl`` for the rest (one
      ``SELECT … IN (…)`` per instrument with a hard chunk size so a
      huge IN-list doesn't blow ConsDB's SQL length limit),
    * persist everything new back to the per-site cache so the next
      night-fetch over the same dataIds starts instant.

    Pushes progress events to ``job.events`` so the SSE consumer can
    show "resolving shutter close for N of M …" without timing out.
    """
    needIds = _neededDataIdsForNight(summaries)
    if not needIds:
        return
    # Night mode is the AOS pipeline, and AOS runs on LSSTCam only (the
    # wavefront sensors live in its corners) — so every dataId in these
    # logs is an LSSTCam id. Pinning matters: on a night where LATISS
    # also observed, a probe-order lookup for a colliding id could
    # anchor an AOS histogram bar to the *LATISS* shutter close.
    _resolveShutterClosesInto(
        needIds,
        state.shutterCloseByExpId,
        state.exposureInfoByExpId,
        job,
        site,
        instrument="lsstcam",
    )


def _prefetchRangeShutterCloses(state: RangeState, job: FetchJob, site: Site) -> None:
    """Populate ``state.shutterCloseByExpId`` with a t₀ for every dataId
    in ``[startId, stopId]``.

    The candidate set is the full integer span — ConsDB is the source of
    truth for which of those are real exposures; the integers it has no
    row for are the "skipped" ones the navigator simply omits. Runs as
    part of the range-fetch job's parsing phase (after the start/stop
    anchors are already seeded) so the /api/summary that follows is
    instant; pushes the same ``shutter-close`` progress events night mode
    does.
    """
    needIds = set(range(state.startId, state.stopId + 1))
    if not needIds:
        return
    _resolveShutterClosesInto(
        needIds,
        state.shutterCloseByExpId,
        state.exposureInfoByExpId,
        job,
        site,
        instrument=job.instrument,
    )


def _resolveShutterClosesInto(
    needIds: set[int],
    target: dict[int, dt.datetime],
    infoTarget: dict[int, exposureTimes.ExposureRecord],
    job: FetchJob,
    site: Site,
    instrument: str | None = None,
) -> None:
    """Resolve each id in ``needIds`` into ``target`` (shutter close t₀,
    UTC) and ``infoTarget`` (the full ConsDB exposure record) — on-disk
    per-site cache first, then one batched ConsDB query for the misses —
    pushing the ``shutter-close`` progress events the SSE consumer
    renders. Shared by the night and range post-parse prefetch paths.

    An id counts as resolved only when its record carries a usable
    ``obs_end`` (the t₀ the histograms need); a record without one is
    treated as a miss and re-queried.

    A ``_manual`` stand-in (see :func:`exposureTimes.manualRecord`) anchors
    its id straight away but is *also* re-queried, exactly as
    ``GET /api/exposure-time`` does: a hand-entered value is a fallback for
    when ConsDB couldn't answer, not the immutable truth a ConsDB row is,
    so a real record must be able to supersede it. Without that, one manual
    entry made during a ConsDB outage would anchor that dataId in every
    future night/range view forever, silently biasing every Δshutter offset
    and histogram derived from it.
    """
    job.push({"type": "shutter-close", "phase": "starting", "total": len(needIds), "site": site.name})

    # 1) Cache lookups — free, instant.
    misses: list[int] = []
    cachedHits = 0
    manualStandins = 0
    for expId in needIds:
        rec = exposureTimes.lookupCachedRecord(expId, siteName=site.name, instrument=instrument)
        iso = exposureTimes.obsEnd(rec)
        if rec is None or iso is None:
            misses.append(expId)
            continue
        target[expId] = exposureTimes.taiIsoToUtc(iso)
        infoTarget[expId] = rec
        if exposureTimes.isManual(rec):
            misses.append(expId)  # anchored provisionally; ConsDB may supersede
            manualStandins += 1
        else:
            cachedHits += 1

    def unanchored() -> int:
        """How many of ``misses`` still have no t₀ at all.

        Manual stand-ins sit in ``misses`` so they get re-queried, but they
        are already anchored — counting them as "remaining" would report a
        fully-resolved night as partly missing.
        """
        return sum(1 for expId in misses if expId not in target)

    job.push(
        {
            "type": "shutter-close",
            "phase": "cache-checked",
            "cacheHits": cachedHits,
            "manualStandins": manualStandins,
            "remaining": unanchored(),
        }
    )
    if not misses:
        return

    # 2) Batch ConsDB queries (one per instrument, chunked). A site that
    #    declares no token file talks to a ConsDB that needs no auth, so we
    #    only insist on a readable token when the catalog names one.
    tokenPath = site.consdbTokenFile
    token = ""
    if tokenPath is not None:
        if not tokenPath.exists():
            job.push(
                {
                    "type": "shutter-close",
                    "phase": "no-token",
                    "remaining": unanchored(),
                    "tokenPath": str(tokenPath),
                    "site": site.name,
                }
            )
            return
        try:
            token = exposureTimes.readToken(tokenPath)
        except OSError:
            token = ""
        if not token:
            job.push({"type": "shutter-close", "phase": "empty-token", "remaining": unanchored()})
            return
    try:
        resolved = exposureTimes.queryExposureRecordBatch(
            misses, token, consdbUrl=site.consdbUrl, instrument=instrument
        )
    except (exposureTimes.ConsDbError, OSError) as e:
        job.push({"type": "shutter-close", "phase": "consdb-error", "error": str(e)})
        return
    # Overwrites any `_manual` stand-in for these ids, on disk and in memory.
    # The bare key is only claimed when the pinned instrument is what a
    # bare-id probe would reach first: a LATISS-pinned batch writing bare
    # keys would hand every colliding id's unqualified lookup — including
    # the night view's rebuild path — the wrong exposure's shutter close.
    exposureTimes.storeCachedRecords(
        resolved, siteName=site.name, bareKey=exposureTimes.isProbeOrderFirst(instrument)
    )
    consdbHits = 0
    for expId, rec in resolved.items():
        iso = exposureTimes.obsEnd(rec)
        if iso is None:
            continue  # row exists but no obs_end — can't anchor a t₀
        target[expId] = exposureTimes.taiIsoToUtc(iso)
        infoTarget[expId] = rec
        consdbHits += 1
    job.push(
        {
            "type": "shutter-close",
            "phase": "done",
            "consdbHits": consdbHits,
            "stillMissing": unanchored(),
        }
    )


def _shutterCloseLabel(record: exposureTimes.ExposureRecord | None) -> str:
    """Reference-point label for a caller-supplied t-zero, distinguishing a
    hand-entered stand-in from one that came (via the home form) from ConsDB.
    """
    if exposureTimes.isManual(record):
        return "shutter close (manual)"
    return "shutter close (caller-supplied)"


def _buildNightPayload(state: NightState) -> dict:
    """Roll the night up into the per-page payload the JS consumes.

    Pure read-from-state: all shutter closes were resolved during the
    fetch job's post-parse phase (:func:`_prefetchNightShutterCloses`)
    or, if no token was configured then, are absent until the user
    re-fetches with a token in place.
    """
    stats = night.computeTopStats(state.summaries)
    errType = night.errorsByType(state.summaries)
    errPod = night.errorsByPod(state.summaries)
    firstStarts = night.firstTaskStartByDataId(state.summaries)
    czEnds = night.calcZernikesEndByDataId(state.summaries)
    gatherOnly = night.gatherOnlyDataIds(state.summaries)
    shutterCloseByExpId = state.shutterCloseByExpId

    # Keep this in lockstep with the prefetch path so the
    # ``nMissingShutterClose`` counter the UI surfaces matches what
    # the prefetch actually attempted. Pass the already-computed
    # starts / ends in so the helper doesn't redo the per-event scan.
    needIds = _neededDataIdsForNight(state.summaries, firstStarts=firstStarts, czEnds=czEnds)
    nMissingShutter = sum(1 for eid in needIds if eid not in shutterCloseByExpId)

    firstOffsets, firstIds, firstNDropped = night.computeDeltaShutterOffsets(firstStarts, shutterCloseByExpId)
    czOffsets, czIds, czNDropped = night.computeDeltaShutterOffsets(czEnds, shutterCloseByExpId)
    histFirst = night.buildHistogram(
        "First task pickup (Δshutter)",
        "s",
        firstOffsets,
        nDroppedNoTZero=firstNDropped,
        dataIds=firstIds,
    )
    histCz = night.buildHistogram(
        "calcZernikes end (Δshutter)",
        "s",
        czOffsets,
        nDroppedNoTZero=czNDropped,
        dataIds=czIds,
    )
    failures = night.failureRows(state.summaries, shutterCloseByExpId)
    restarts = night.lifecycleRows(state.summaries, shutterCloseByExpId)

    return {
        "loaded": True,
        "mode": "night",
        "site": state.siteName,
        # Night mode is the AOS pipeline, which runs on LSSTCam only —
        # every dataId in this payload is an LSSTCam id and was resolved
        # against the LSSTCam table.
        "instrument": "lsstcam",
        "dayObs": state.dayObs,
        "startTime": state.startTime.isoformat(),
        "endTime": state.endTime.isoformat(),
        "cacheDir": str(state.cacheDir),
        "cacheBytes": state.cacheBytes,
        "meta": _toJsonable(state.meta),
        "stats": {
            "nVisitsSeen": stats.nVisitsSeen,
            "nPods": stats.nPods,
            "nTracebacks": stats.nTracebacks,
            "nDataIdsWithTraceback": stats.nDataIdsWithTraceback,
            "nPodsWithTraceback": stats.nPodsWithTraceback,
            "nDistinctExceptionClasses": stats.nDistinctExceptionClasses,
            "nMissingShutterClose": nMissingShutter,
            "nPodRestarts": stats.nPodRestarts,
        },
        # One row per pod restart / kill / OOM / failure (POD_* lifecycle
        # events), attributed to the dataId the pod was processing then.
        "restarts": [_toJsonable(r) for r in restarts],
        "errorsByType": [_toJsonable(r) for r in errType],
        "errorsByPod": [_toJsonable(r) for r in errPod],
        "histograms": {
            "firstTaskStart": _toJsonable(histFirst),
            "calcZernikesEnd": _toJsonable(histCz),
        },
        "failures": [_toJsonable(r) for r in failures],
        # dataIds with gather (step1b) activity but no step1a precursor —
        # physically impossible, so a tell that the fetch dropped step1a
        # lines. The UI surfaces these as a data-completeness warning.
        "gatherOnly": gatherOnly,
        # dataId (as string) -> curated ConsDB record, for the tooltips on
        # the histogram-bin and failure-table dataId links.
        "exposureInfo": {str(eid): rec for eid, rec in state.exposureInfoByExpId.items()},
    }


# ----- range payload --------------------------------------------------------


def _buildRangePayload(state: RangeState) -> dict:
    """The lightweight range *index* payload (``mode: "range"``).

    Carries one entry per resolved dataId (a small per-exposure overview
    so the navigator can flag failures at a glance) but no pod/event
    arrays — those are fetched per-dataId on demand via
    :func:`_buildRangeExposurePayload`.
    """
    tracebackByExpId: dict[int, int] = {}
    podsByExpId: dict[int, int] = {}
    for s in state.summaries:
        for eid in s.expIdsSeen:
            podsByExpId[eid] = podsByExpId.get(eid, 0) + 1
        for tb in s.tracebacks:
            if tb.expId is not None:
                tracebackByExpId[tb.expId] = tracebackByExpId.get(tb.expId, 0) + 1

    dataIds = [
        {
            "expId": expId,
            "tZero": state.shutterCloseByExpId[expId].isoformat(),
            "nPods": podsByExpId.get(expId, 0),
            "nTraceback": tracebackByExpId.get(expId, 0),
            "hasLogs": expId in podsByExpId,
            # Curated ConsDB record for the navigator chip's tooltip.
            "exposure": state.exposureInfoByExpId.get(expId),
        }
        for expId in sorted(state.shutterCloseByExpId)
    ]
    nCandidates = state.stopId - state.startId + 1
    return {
        "loaded": True,
        "mode": "range",
        "site": state.siteName,
        "instrument": state.instrument,
        "startId": state.startId,
        "stopId": state.stopId,
        "fromTime": state.fromTime.isoformat(),
        "toTime": state.toTime.isoformat(),
        "cacheDir": str(state.cacheDir),
        "cacheBytes": state.cacheBytes,
        "meta": _toJsonable(state.meta),
        # Integers in [start, stop] with no ConsDB row — the "skipped" ones.
        "nMissing": nCandidates - len(state.shutterCloseByExpId),
        "dataIds": dataIds,
    }


def _rangeExposureState(state: RangeState, dataId: int) -> ServerState | None:
    """Build a transient :class:`ServerState` for one dataId in the range.

    Shares the range's already-parsed ``summaries`` and points t₀ at that
    dataId's own shutter close, so the per-exposure helpers
    (:func:`_buildSummaryPayload`, :func:`_podDetail`) can be reused
    verbatim. Returns ``None`` if we never resolved a shutter close for
    this dataId (a skipped integer).
    """
    tZero = state.shutterCloseByExpId.get(dataId)
    if tZero is None:
        return None
    return ServerState(
        cacheDir=state.cacheDir,
        cacheBytes=state.cacheBytes,
        meta=state.meta,
        summaries=state.summaries,
        expId=dataId,
        tZero=tZero,
        siteName=state.siteName,
        instrument=state.instrument,
        exposureInfo=state.exposureInfoByExpId.get(dataId),
        referencePoints=[
            {
                "label": "shutter close (ConsDB)",
                "t": tZero.isoformat(),
                "offsetS": 0.0,
                "source": "shutter close",
            }
        ],
    )


def _buildRangeExposurePayload(state: RangeState, dataId: int) -> dict | None:
    """The per-dataId timeline payload for a range — the same shape the
    single-exposure explore view consumes, plus a ``podDetailQuery`` so
    the client routes pod-detail fetches back through the range state.
    """
    transient = _rangeExposureState(state, dataId)
    if transient is None:
        return None
    payload = _buildSummaryPayload(transient)
    payload["mode"] = "range-exposure"
    # The instrument rides along so pod-detail lookups carry the same pin
    # the summary was served under — the range key alone is shared with
    # the other instrument's span.
    podDetailQuery = f"rangeStart={state.startId}&rangeStop={state.stopId}&dataId={dataId}"
    if state.instrument:
        podDetailQuery += f"&instrument={state.instrument}"
    payload["podDetailQuery"] = podDetailQuery
    payload["startId"] = state.startId
    payload["stopId"] = state.stopId
    return payload


# How much surrounding context to ship with a single traceback. A busy
# AOS quantum can emit ~hundreds of lines per dataId; this is a sanity
# cap so the drilldown doesn't have to render a multi-megabyte
# response. Bumped if real bodies routinely exceed it; truncation is
# explicitly reported to the UI.
_TB_CONTEXT_MAX_LINES = 4_000
_TB_CONTEXT_MAX_CHARS = 600_000
# When the traceback couldn't be attributed to a dataId (e.g. carryover
# was off, or it fired before the first dataId mention), fall back to a
# fixed pre-window of N seconds so the drilldown still has *some*
# context to show.
_TB_NO_EXPID_LOOKBACK_S = 30.0
_TB_NO_EXPID_LOOKAHEAD_S = 5.0


def _tracebackContextForNight(state: NightState, bodyKey: str) -> dict | None:
    """Return the pod's log lines from a dataId's pickup through the
    traceback (and through the end of that dataId's processing block).

    This is what the night-view drilldown fetches when the user clicks
    a failure row. The point is to give surrounding context — what was
    the worker doing right before it crashed — without making the user
    open the full per-pod log to scroll back.
    """
    targetSummary: parser.PodSummary | None = None
    targetTb: parser.TracebackRecord | None = None
    for s in state.summaries:
        for tb in s.tracebacks:
            if night.makeBodyKey(s.pod, tb.t) == bodyKey:
                targetSummary, targetTb = s, tb
                break
        if targetSummary is not None:
            break
    if targetSummary is None or targetTb is None:
        return None

    startTs: dt.datetime
    endTs: dt.datetime
    contextSource: str
    if targetTb.expId is not None and targetTb.expId in targetSummary.expIdFirstLast:
        startTs, endTs = targetSummary.expIdFirstLast[targetTb.expId]
        contextSource = "dataId-block"
    else:
        # No carryover-attributed dataId — fall back to a fixed pre-window.
        startTs = targetTb.t - dt.timedelta(seconds=_TB_NO_EXPID_LOOKBACK_S)
        endTs = targetTb.t + dt.timedelta(seconds=_TB_NO_EXPID_LOOKAHEAD_S)
        contextSource = "time-window"

    logPath = loadPodLogPath(state.cacheDir, targetSummary.pod)
    lines: list[dict] = []
    truncated = False
    totalChars = 0
    for ln in parser.iterPodLines(logPath):
        if ln.timestamp < startTs:
            continue
        if ln.timestamp > endTs:
            break
        if len(lines) >= _TB_CONTEXT_MAX_LINES or totalChars >= _TB_CONTEXT_MAX_CHARS:
            truncated = True
            break
        lines.append(
            {
                "t": ln.timestamp.isoformat(),
                "level": ln.level,
                "raw": ln.raw,
            }
        )
        totalChars += len(ln.raw)

    return {
        "bodyKey": bodyKey,
        "pod": targetSummary.pod,
        "group": targetSummary.group,
        "expId": targetTb.expId,
        "excClass": targetTb.excClass,
        "excMessage": targetTb.excMessage,
        "firstTs": startTs.isoformat(),
        "lastTs": endTs.isoformat(),
        "tracebackTs": targetTb.t.isoformat(),
        "contextSource": contextSource,
        "lines": lines,
        "truncated": truncated,
        # The captured traceback body is still handy when context lookup
        # is degenerate (e.g. the cache dir is missing). Ship it as a
        # backup the UI can fall through to.
        "body": targetTb.body,
    }


# ----- cache listing --------------------------------------------------------


def _completedCacheMeta(window: Path) -> dict | None:
    """Parsed ``_meta.json`` for a completed cache window, else ``None``.

    "Completed" means the meta is present, parseable, and no ``.partial``
    flag — the one eligibility test every cache-walking reader shares.
    """
    metaPath = window / META_NAME
    if not metaPath.exists() or (window / PARTIAL_FLAG).exists():
        return None
    try:
        return json.loads(metaPath.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _iterCompletedCaches(nightMode: bool) -> Iterator[tuple[Path, dict]]:
    """Yield ``(dir, meta)`` for every completed cache window on disk.

    ``nightMode`` picks the layout depth: night-mode (pod-filtered)
    caches nest one level deeper, at ``<cluster>/<ns>/<window>/pods=<slug>/``;
    everything else — exposure and range caches — lives at
    ``<cluster>/<ns>/<window>/``. One walker, so the rebuild-path finders
    can't disagree about what counts as a usable cache.
    """
    root = cache_root()
    if not root.exists():
        return
    for cluster in root.iterdir():
        if not cluster.is_dir():
            continue
        for ns in cluster.iterdir():
            if not ns.is_dir():
                continue
            for window in ns.iterdir():
                if not window.is_dir():
                    continue
                if nightMode:
                    candidates = [d for d in window.iterdir() if d.is_dir() and d.name.startswith("pods=")]
                else:
                    candidates = [window]
                for candidate in candidates:
                    meta = _completedCacheMeta(candidate)
                    if meta is not None:
                        yield candidate, meta


def _listCacheWindows() -> list[dict]:
    """Inspect the cache root and summarise each completed window.

    Walks two depths:

    * exposure-mode caches at ``<root>/<cluster>/<ns>/<window>/``
    * night-mode caches at ``<root>/<cluster>/<ns>/<window>/pods=<slug>/``

    Both are returned as rows. The ``podFilter`` field is the regex
    string from the cached spec (or ``None`` for unfiltered exposure
    caches); the ``relPath`` joins ``<window>`` and ``pods=<slug>`` when
    relevant so the JS can DELETE the right sub-path.
    """
    root = cache_root()
    rows: list[dict] = []
    if not root.exists():
        return rows
    for cluster in sorted(p for p in root.iterdir() if p.is_dir()):
        for ns in sorted(p for p in cluster.iterdir() if p.is_dir()):
            for window in sorted(p for p in ns.iterdir() if p.is_dir()):
                _appendCacheRow(rows, cluster.name, ns.name, window, relPath=window.name)
                # Night-mode caches nest one level deeper under
                # `pods=<slug>/`. List them alongside the top-level
                # window so the user can manage them independently.
                for inner in sorted(p for p in window.iterdir() if p.is_dir()):
                    if inner.name.startswith("pods="):
                        _appendCacheRow(
                            rows,
                            cluster.name,
                            ns.name,
                            inner,
                            relPath=f"{window.name}/{inner.name}",
                        )
    rows.sort(key=lambda r: r.get("fetchedAt") or "", reverse=True)
    return rows


def _appendCacheRow(rows: list[dict], cluster: str, ns: str, window: Path, *, relPath: str) -> None:
    meta = _completedCacheMeta(window)
    if meta is None:
        return
    spec = meta.get("spec") or {}
    # A range cache is exposure-style (no podRegex) but carries a
    # `_range.txt` sidecar; that marker wins over plain "exposure".
    rangeBounds = None if spec.get("podRegex") else getCacheRange(window)
    if spec.get("podRegex"):
        kind = "night"
    elif rangeBounds is not None:
        kind = "range"
    else:
        kind = "exposure"
    lastViewed = getCacheLastViewed(window)
    # Only plain exposure caches carry the dataId sidecar (night caches
    # are keyed by dayObs, range caches by their bounds — both recovered
    # below). Each entry keeps the instrument it was fetched under, so
    # the listing's link opens this run rather than the same-numbered
    # exposure on the other instrument.
    exposures: list[dict[str, object]] = (
        [{"dataId": eid, "instrument": inst} for inst, eid in getCacheExposureIds(window)]
        if kind == "exposure"
        else []
    )
    # For night caches the dayObs is recoverable from the window start
    # (noon UTC of dayObs). For exposure caches there's no single
    # dataId in the meta — the UI looks it up against the loaded state
    # if/when one is attached, but the cache list itself just leaves
    # it null.
    dayObs: int | None = None
    if kind == "night":
        fromIso = spec.get("fromIso")
        if isinstance(fromIso, str):
            try:
                # Strip ms / Z to land on a normal isoformat.
                d = dt.datetime.fromisoformat(fromIso.replace("Z", "+00:00"))
                dayObs = int(d.strftime("%Y%m%d"))
            except (TypeError, ValueError):
                dayObs = None
    rows.append(
        {
            "cluster": cluster,
            "namespace": ns,
            "windowDir": window.name,
            "relPath": relPath,
            "podFilter": spec.get("podRegex"),
            "kind": kind,
            "dayObs": dayObs,
            "rangeStart": rangeBounds[0] if rangeBounds else None,
            "rangeStop": rangeBounds[1] if rangeBounds else None,
            # Range rows carry their pin so the admin table's link can
            # reopen *this* run rather than the twin span the other
            # instrument fetched over the same bounds.
            "rangeInstrument": (getCacheRangeInstrument(window) if rangeBounds else None),
            "exposures": exposures,
            "fromIso": spec.get("fromIso"),
            "toIso": spec.get("toIso"),
            "fetchedAt": meta.get("fetched_at"),
            "lastViewedAt": lastViewed.isoformat() if lastViewed else None,
            "podCount": meta.get("pod_count", 0),
            "totalBytes": meta.get("total_bytes", 0),
            "sizeOnDisk": cacheDuSizeBytes(window),
        }
    )


def _supersededNightSlices(job: FetchJob) -> list[Path]:
    """Night windows for this night that the job's own window contains.

    Night mode on the *current* dayObs is served clamped to the live
    watermark, so the window's path moves as the watermark advances:
    every fetch while the night is in progress mints a new
    ``[nightStart, watermark]`` directory, and the previous one becomes a
    strict subset of it — same pod filter, same start, fewer lines.
    Nothing looks at it again (a reload picks the newest by
    ``fetched_at``, and a contained request is served by the newer one),
    so it is dead weight from the moment the next fetch lands: a few
    hundred MiB a click, and a click is all it takes.

    Leaving them to LRU eviction is not good enough, because eviction
    reaches them *last*. Each is freshly stamped, so a pass under
    pressure prefers whatever was viewed longest ago — which is
    typically *yesterday's finalised night dir*, 9 GiB that took minutes
    of Loki to build and that today's redundant copies would evict to
    keep themselves. Same inversion ``_tryNightSlice``'s
    ``markCacheViewed`` guards against, arriving from the other side.

    Deliberately keyed off ``job.meta``'s spec rather than ``job.spec``:
    the requested window is the whole night, and the window actually
    written is the clamped one. Only strict subsets are returned, so a
    watermark that regressed (a cache wipe, or one tick of a newly-seen
    pod failing) leaves the wider dir alone rather than deleting live
    coverage. Night mode is browser-only, so the fetch-job callback is
    the one path that needs this.
    """
    if job.cacheDir is None:
        return []
    spec = job.meta.get("spec") or {}
    fromIso, toIso = spec.get("fromIso"), spec.get("toIso")
    if not isinstance(fromIso, str) or not isinstance(toIso, str):
        return []
    try:
        keptTo = _parseClientIso(toIso)
    except ValueError:
        return []
    keep = job.cacheDir.resolve()
    stale: list[Path] = []
    for window, meta in _iterCompletedCaches(nightMode=True):
        if window.resolve() == keep:
            continue
        cSpec = meta.get("spec") or {}
        if (cSpec.get("cluster"), cSpec.get("namespace"), cSpec.get("podRegex")) != (
            spec.get("cluster"),
            spec.get("namespace"),
            spec.get("podRegex"),
        ):
            continue
        if cSpec.get("fromIso") != fromIso:
            continue
        try:
            if _parseClientIso(str(cSpec.get("toIso"))) < keptTo:
                stale.append(window)
        except (TypeError, ValueError):
            continue
    return stale


def _cacheRootInfo() -> dict:
    root = cache_root()
    return {
        "path": str(root),
        "totalBytes": cacheDuSizeBytes(root),
    }


def _findExposureCacheDirs(expId: int, instrument: str | None = None) -> list[Path]:
    """Exposure caches recorded as holding ``expId``, newest fetch first.

    A *list*, because a bare id is not unique: on a night where two
    instruments both observe, the same id names an exposure on each, and
    both their windows can be cached at once. Handing back only the
    newest would make whichever deep link was opened second break the
    first — and re-fetching to fix it just swaps which one is broken. The
    caller picks the window that actually holds the t₀ it is after.

    With ``instrument`` given, only windows whose sidecar records that
    exposure *under that instrument* qualify. The recorded pin is what
    the fetch actually ran as, so this is a stronger discriminator than
    the caller's t₀-containment check: the two instruments' twins are an
    hour apart, but a widened window can span both, and then containment
    alone would happily hand back the wrong run's logs. Unpinned (the
    bare-id path) every window listing the id qualifies, as before.
    """
    found = [
        (str(meta.get("fetched_at") or ""), window)
        for window, meta in _iterCompletedCaches(nightMode=False)
        if not (meta.get("spec") or {}).get("podRegex")  # night caches live one level deeper
        and any(
            eid == expId and (instrument is None or inst == instrument)
            for inst, eid in getCacheExposureIds(window)
        )
    ]
    return [window for _, window in sorted(found, reverse=True)]


def _findNightCacheDir(dayObs: int) -> Path | None:
    """Return the most-recently-fetched night cache for ``dayObs``."""
    best: tuple[str, Path] | None = None
    for inner, meta in _iterCompletedCaches(nightMode=True):
        spec = meta.get("spec") or {}
        fromIso = spec.get("fromIso")
        if not spec.get("podRegex") or not isinstance(fromIso, str):
            continue
        try:
            windowFrom = dt.datetime.fromisoformat(fromIso.replace("Z", "+00:00"))
        except ValueError:
            continue
        if int(windowFrom.strftime("%Y%m%d")) != dayObs:
            continue
        fetchedAt = str(meta.get("fetched_at") or "")
        if best is None or fetchedAt > best[0]:
            best = (fetchedAt, inner)
    return best[1] if best else None


def _pickExposureCache(
    ctx: ServerContext, expId: int, instrument: str | None
) -> tuple[Path, Site, dict, dict, dt.datetime] | None:
    """The cached window that really holds this exposure, or ``None``.

    A window qualifies only if it was fetched for this exposure under the
    caller's instrument (see :func:`_findExposureCacheDirs`) *and* spans
    the exposure's own shutter close, resolved under that same
    instrument. Newest-first among the ones that do, which also means a
    re-fetch wins over an older window of the same exposure.

    Being wrong here is bounded rather than dangerous: any window that
    spans this t₀ does contain this exposure's logs, at worst
    edge-truncated (which `looksTruncatedEnd` flags). The pin has
    already decided *which* exposure is being asked about, by the time
    the record was looked up.
    """
    for cacheDir in _findExposureCacheDirs(expId, instrument):
        site = _siteForCacheDir(ctx, cacheDir)
        if site is None:
            continue
        record = exposureTimes.lookupCachedRecord(expId, siteName=site.name, instrument=instrument)
        tZeroIso = exposureTimes.obsEnd(record)
        if record is None or tZeroIso is None:
            # This candidate's site has no record to anchor on. Deployed
            # there is one site and every candidate shares it, but a
            # laptop's cache can hold windows from both clusters — a
            # later candidate on the other site may still resolve.
            continue
        try:
            meta = loadCacheMeta(cacheDir)
        except (OSError, json.JSONDecodeError, FileNotFoundError):
            continue
        tZero = exposureTimes.taiIsoToUtc(tZeroIso)
        specMeta = meta.get("spec") or {}
        try:
            windowFrom = _parseClientIso(str(specMeta.get("fromIso")))
            windowTo = _parseClientIso(str(specMeta.get("toIso")))
        except (TypeError, ValueError):
            continue
        if windowFrom <= tZero <= windowTo:
            return cacheDir, site, record, meta, tZero
    return None


def _loadExposureFromCache(
    ctx: ServerContext, expId: int, instrument: str | None = None
) -> ServerState | None:
    """Reconstruct a :class:`ServerState` from disk for ``expId``, if possible.

    Lets the user open a deep-linked exposure URL (e.g. from the cache
    table in another tab) without forcing a re-fetch — the cache + the
    on-disk exposure-time record together carry everything we need to
    rebuild the in-memory state. Returns ``None`` if either is missing.

    The site is derived from the cache path's cluster component (the
    layout is ``<cache_root>/<cluster>/<namespace>/<window>/…``), so the
    right per-site exposure-time cache gets consulted for the shutter
    close. ``instrument`` pins that lookup, and the cache window found
    for the bare id must actually contain the pinned t₀ — the sidecar
    lists bare ids, so on a colliding id it could name the *other*
    instrument's window, whose logs would be a different exposure's.
    """
    found = _pickExposureCache(ctx, expId, instrument)
    if found is None:
        return None
    cacheDir, site, record, meta, tZero = found
    summaries = parser.summarizeAll(cacheDir)
    state = ServerState(
        cacheDir=cacheDir,
        cacheBytes=cacheDuSizeBytes(cache_root()),
        meta=meta,
        summaries=summaries,
        expId=expId,
        tZero=tZero,
        siteName=site.name,
        instrument=instrument or exposureTimes.recordInstrument(record),
        exposureInfo=record,
        referencePoints=[
            {
                "label": _shutterCloseLabel(record),
                "t": tZero.isoformat(),
                "offsetS": 0.0,
                "source": "shutter close",
            }
        ],
    )
    with ctx.jobs.stateLock:
        ctx.putExposureState(state)
    markCacheViewed(cacheDir)
    return state


def _loadNightFromCache(ctx: ServerContext, dayObs: int) -> NightState | None:
    """Reconstruct a :class:`NightState` from disk for ``dayObs``, if possible.

    Mirrors :func:`_loadExposureFromCache` for the night-mode view. Shutter
    closes are populated from the on-disk exposure-time cache only — no
    ConsDB call is made (it's synchronous and we have no progress stream
    here). Any dataId not already in the local cache stays absent until a
    real fetch fills it in.
    """
    cacheDir = _findNightCacheDir(dayObs)
    if cacheDir is None:
        return None
    site = _siteForCacheDir(ctx, cacheDir)
    if site is None:
        return None
    try:
        meta = loadCacheMeta(cacheDir)
    except (OSError, json.JSONDecodeError, FileNotFoundError):
        return None
    summaries = parser.summarizeAll(cacheDir)
    state = NightState(
        cacheDir=cacheDir,
        cacheBytes=cacheDuSizeBytes(cache_root()),
        meta=meta,
        summaries=summaries,
        dayObs=dayObs,
        startTime=dayObsStartUtc(dayObs),
        endTime=dayObsEndUtc(dayObs),
        siteName=site.name,
    )
    # Pinned to LSSTCam, exactly like _prefetchNightShutterCloses: AOS
    # runs on LSSTCam only, and on a night where LATISS also observed a
    # bare lookup for a colliding id could anchor an AOS histogram bar
    # to the LATISS shutter close.
    for needId in _neededDataIdsForNight(summaries):
        record = exposureTimes.lookupCachedRecord(needId, siteName=site.name, instrument="lsstcam")
        iso = exposureTimes.obsEnd(record)
        if record is not None and iso is not None:
            state.shutterCloseByExpId[needId] = exposureTimes.taiIsoToUtc(iso)
            state.exposureInfoByExpId[needId] = record
    with ctx.jobs.stateLock:
        ctx.putNightState(state)
    markCacheViewed(cacheDir)
    return state


def _findRangeCacheDir(startId: int, stopId: int, instrument: str | None = None) -> Path | None:
    """Return the most-recently-fetched range cache for ``[startId, stopId]``.

    Range caches are exposure-style (top-level, all pods) and identified
    by the ``_range.txt`` sidecar, so we walk the same depth as
    :func:`_findExposureCacheDirs` and match on the recorded bounds.

    ``instrument`` narrows the match to caches fetched under that pin.
    The bounds alone do not identify a range: the same
    ``[startId, stopId]`` span exists on every instrument that observed
    that many exposures, and they are different exposures. Without the
    pin the newest fetch wins, which is how a LATISS tab ends up looking
    at LSSTCam's span.
    """
    best: tuple[str, Path] | None = None
    for window, meta in _iterCompletedCaches(nightMode=False):
        if getCacheRange(window) != (startId, stopId):
            continue
        if instrument is not None and getCacheRangeInstrument(window) != instrument:
            continue
        fetchedAt = str(meta.get("fetched_at") or "")
        if best is None or fetchedAt > best[0]:
            best = (fetchedAt, window)
    return best[1] if best else None


def _loadRangeFromCache(
    ctx: ServerContext, startId: int, stopId: int, instrument: str | None = None
) -> RangeState | None:
    """Reconstruct a :class:`RangeState` from disk, if possible.

    Mirrors :func:`_loadNightFromCache`: shutter closes are read from the
    on-disk per-site cache only (no ConsDB call — there's no SSE channel
    on a sync /api/summary request), so any dataId not already cached
    locally stays absent until a real fetch fills it in.

    The instrument comes back from the ``_range.txt`` sidecar and pins
    both the rebuilt state (so its per-exposure timelines exclude the
    other instrument's pods) and every per-id lookup here — a bare lookup
    on a colliding id would anchor that exposure to the *other*
    instrument's shutter close. A sidecar without one isn't recognised
    as a range at all, so a found range always carries its pin.

    A caller-supplied ``instrument`` additionally narrows *which* cache
    is eligible, so a pinned tab rebuilds its own range rather than the
    twin span another tab fetched more recently.
    """
    cacheDir = _findRangeCacheDir(startId, stopId, instrument)
    if cacheDir is None:
        return None
    site = _siteForCacheDir(ctx, cacheDir)
    if site is None:
        return None
    try:
        meta = loadCacheMeta(cacheDir)
    except (OSError, json.JSONDecodeError, FileNotFoundError):
        return None
    spec = meta.get("spec") or {}
    try:
        fromTime = dt.datetime.fromisoformat(str(spec.get("fromIso")).replace("Z", "+00:00"))
        toTime = dt.datetime.fromisoformat(str(spec.get("toIso")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    instrument = getCacheRangeInstrument(cacheDir)
    summaries = parser.summarizeAll(cacheDir)
    state = RangeState(
        cacheDir=cacheDir,
        cacheBytes=cacheDuSizeBytes(cache_root()),
        meta=meta,
        summaries=summaries,
        startId=startId,
        stopId=stopId,
        fromTime=fromTime,
        toTime=toTime,
        siteName=site.name,
        instrument=instrument,
    )
    for expId in range(startId, stopId + 1):
        record = exposureTimes.lookupCachedRecord(expId, siteName=site.name, instrument=instrument)
        iso = exposureTimes.obsEnd(record)
        if record is not None and iso is not None:
            state.shutterCloseByExpId[expId] = exposureTimes.taiIsoToUtc(iso)
            state.exposureInfoByExpId[expId] = record
    with ctx.jobs.stateLock:
        ctx.putRangeState(state)
    markCacheViewed(cacheDir)
    return state


def _siteForCacheDir(ctx: ServerContext, cacheDir: Path) -> Site | None:
    """Return the catalog site whose ``cluster`` matches this cache dir.

    The cache layout encodes cluster as the first path component under
    ``cache_root()``, e.g.
    ``<cache_root>/yagan/rapid-analysis/<window>/…``. We rely on that
    to figure out which ConsDB / per-site exposure-time cache to use
    when reconstructing state from disk. Returns ``None`` if the
    cluster has no site mapping (a stale cache from a removed entry).
    """
    try:
        rel = cacheDir.resolve().relative_to(cache_root().resolve())
    except (OSError, ValueError):
        return None
    parts = rel.parts
    if not parts:
        return None
    try:
        return siteByCluster(ctx.sites, parts[0])
    except SitesConfigError:
        return None


# A cache path component must be a "safe" basename — no path separators,
# no leading dot, no `..` traversal. The same pattern is also used to
# validate pod names elsewhere so the choice is consistent.
# Path components in the cache layout. Allows `=` so the night-mode
# ``pods=<slug>`` subdir name is accepted; everything else is the
# previous filesystem-friendly safe set.
_PATH_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._=-]+$")


def _safePathComponent(s: str) -> bool:
    return bool(_PATH_COMPONENT_RE.match(s)) and s not in (".", "..")


def _resolveStaticFile(rel: str) -> Path | None:
    """Return the static file ``rel`` names, or ``None`` if it escapes.

    Containment is checked *after* joining, not by pattern-matching the
    request. Rejecting ``..`` segments alone is not enough: ``Path("a") /
    "/etc/passwd"`` discards the left operand entirely, so a request for
    ``/static//etc/passwd`` would otherwise read an absolute path — and
    the deployed process holds ``LOKI_PASSWORD`` in an environment
    ``/proc/self/environ`` would hand over.
    """
    if not rel or rel.startswith("/"):
        return None
    path = (STATIC_DIR / rel).resolve()
    try:
        path.relative_to(STATIC_DIR.resolve())
    except ValueError:
        return None
    if not path.is_file():
        return None
    return path


def _resolveCacheWindow(cluster: str, namespace: str, slug: str, podsSub: str | None = None) -> Path | None:
    """Return the cache directory for ``(cluster, namespace, slug[, podsSub])``.

    Validates the components first so an attacker can't escape ``cache_root()``.
    Returns ``None`` when any component is unsafe or the directory doesn't exist.

    ``podsSub`` is the optional 4th URL segment used for night-mode caches
    (e.g. ``"pods=__aos__"``). Required to start with ``pods=`` so the
    URL space stays unambiguous.
    """
    components = [cluster, namespace, slug]
    if podsSub is not None:
        if not podsSub.startswith("pods="):
            return None
        components.append(podsSub)
    if not all(_safePathComponent(c) for c in components):
        return None
    path = cache_root().joinpath(*components)
    if not path.exists() or not path.is_dir():
        return None
    # Final safety check: the resolved path must still live under cache_root.
    try:
        path.resolve().relative_to(cache_root().resolve())
    except ValueError:
        return None
    return path


def _deleteCacheDir(ctx: "ServerContext", target: Path) -> None:
    """Remove a single cache directory, evicting any loaded state that
    used it.

    Empty parent directories (the per-cluster and per-namespace ones) are
    also removed when they become empty, so a flush via repeated deletes
    leaves the same clean state as `DELETE /api/cache` followed by ``ls``.
    """
    with ctx.jobs.stateLock:
        ctx.evictByCacheDir(target)
    dropLiveSidecarsUnder(target)
    # A single rmtree can legitimately fail: another request thread
    # slicing out of this window writes its `_last_viewed.txt` as we go,
    # and a directory that regrew a file raises ENOTEMPTY. One retry
    # settles it, and letting the OSError out instead would drop the
    # connection with no response at all rather than answering.
    try:
        shutil.rmtree(target)
    except OSError:
        shutil.rmtree(target, ignore_errors=True)
    # Tidy up empty parents — best-effort for the same reason.
    parent = target.parent
    while parent != cache_root() and parent.exists() and not any(parent.iterdir()):
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def _deleteCacheRoot(ctx: "ServerContext") -> None:
    """Wipe the entire cache and clear every loaded state (all of which
    by definition referenced the now-gone cache)."""
    with ctx.jobs.stateLock:
        ctx.exposureStates.clear()
        ctx.nightStates.clear()
        ctx.rangeStates.clear()
    root = cache_root()
    if root.exists():
        # Sidecars first (see fetch.dropLiveSidecarsUnder), then the
        # tree. The poller may be writing into it as we go, so a single
        # rmtree can legitimately fail on a directory that regrew a
        # file; one retry settles it, and the poller re-opens the night
        # either way.
        dropLiveSidecarsUnder(root)
        try:
            shutil.rmtree(root)
        except OSError:
            shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)


# ----- request handling ----------------------------------------------------


def _readJsonBody(handler: BaseHTTPRequestHandler) -> Any:
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    return json.loads(raw)


def _onFetchComplete(ctx: ServerContext) -> Any:
    """Return a callback that inserts the new ServerState / NightState
    into the context's keyed-state dict after a fetch.

    Other loaded states (other exposures, other nights) are left alone —
    a fetch in one tab doesn't disturb another tab's view.
    """

    def cb(job: FetchJob) -> None:
        if job.cacheDir is None:
            return  # fetchAll raised; caller will see an error event
        # The fetch finished successfully — this cache is now the
        # most-recently-used one. Mark it before any LRU eviction so
        # it can't get caught up in its own cleanup pass.
        markCacheViewed(job.cacheDir)
        # Record the dataId that triggered this fetch alongside the
        # cache (exposure jobs only; night jobs are keyed by dayObs and
        # range jobs by their bounds). The instrument goes in with it:
        # the id alone doesn't say which exposure this window holds.
        # Done before eviction so a later eviction pass can't race
        # with the sidecar write.
        if job.kind != "night" and job.expId is not None and job.instrument:
            addExposureToCache(job.cacheDir, job.expId, job.instrument)
        # Drop the "night so far" windows this fetch supersedes. *Before*
        # eviction, not after: these are bytes already known to be dead,
        # and freeing them first is often the whole overage — where a
        # sweep running first would reach for the least-recently-viewed
        # window instead, which is typically yesterday's finalised night
        # dir. (Also before the new state is published, since the delete
        # evicts any loaded state pointing at a doomed window and one of
        # those is this dayObs's.)
        if job.kind == "night":
            for stale in _supersededNightSlices(job):
                _deleteCacheDir(ctx, stale)
        # Run LRU eviction so the on-disk total stays at or under the
        # configured cap. The just-fetched cache is exempted; we
        # accept a brief over-cap state during the fetch itself and
        # only sweep at the end. Any loaded state whose window was
        # evicted is dropped with it — a state held in memory over a
        # deleted directory would keep serving its summary while every
        # pod drilldown came back silently empty.
        evicted = evictToFit(MAX_CACHE_BYTES, exempt=[job.cacheDir])
        if evicted:
            with ctx.jobs.stateLock:
                for gone in evicted:
                    ctx.evictByCacheDir(gone)
        summaries = parser.summarizeAll(job.cacheDir)
        # Sites are validated when the request comes in, so this should
        # always succeed for a job we actually started. Bail out on the
        # paranoid edge case (stale catalog reload) rather than crashing
        # the worker thread.
        try:
            site = siteByName(ctx.sites, job.siteName)
        except SitesConfigError:
            return
        if job.kind == "night":
            assert job.dayObs is not None
            newNight = NightState(
                cacheDir=job.cacheDir,
                cacheBytes=cacheDuSizeBytes(cache_root()),
                meta=job.meta,
                summaries=summaries,
                dayObs=job.dayObs,
                startTime=dayObsStartUtc(job.dayObs),
                endTime=dayObsEndUtc(job.dayObs),
                siteName=site.name,
            )
            # Resolve shutter closes for every dataId we'll need before
            # publishing the state, so the /api/summary response is
            # instant and the histograms are populated on first paint.
            # This can take a few seconds for a busy night (hundreds of
            # dataIds), so we report progress to the SSE stream while
            # we work.
            _prefetchNightShutterCloses(newNight, summaries, job, site)
            with ctx.jobs.stateLock:
                ctx.putNightState(newNight)
            return
        if job.kind == "range":
            assert job.startId is not None and job.stopId is not None
            assert job.tZeroStart is not None and job.tZeroStop is not None
            assert job.instrument is not None  # every range job is created with one
            newRange = RangeState(
                cacheDir=job.cacheDir,
                cacheBytes=cacheDuSizeBytes(cache_root()),
                meta=job.meta,
                summaries=summaries,
                startId=job.startId,
                stopId=job.stopId,
                fromTime=dt.datetime.fromisoformat(job.spec.fromIso.replace("Z", "+00:00")),
                toTime=dt.datetime.fromisoformat(job.spec.toIso.replace("Z", "+00:00")),
                siteName=site.name,
                instrument=job.instrument,
            )
            # Seed the two anchors the client already resolved so even a
            # token-less server has start + stop; the prefetch fills the
            # middle (and re-confirms these from cache).
            newRange.shutterCloseByExpId[job.startId] = job.tZeroStart
            newRange.shutterCloseByExpId[job.stopId] = job.tZeroStop
            _prefetchRangeShutterCloses(newRange, job, site)
            markCacheRange(job.cacheDir, job.startId, job.stopId, instrument=job.instrument)
            with ctx.jobs.stateLock:
                ctx.putRangeState(newRange)
            return
        assert job.expId is not None and job.tZero is not None
        # The home page resolves the dataId via /api/exposure-time before
        # firing the fetch, so the full ConsDB record is already in the
        # per-site cache — read it back for the explore-view info box,
        # pinned to the job's instrument (a bare lookup could hand back
        # the other instrument's record for a colliding id).
        # No extra ConsDB call here; None just means no info box.
        exposureInfo = exposureTimes.lookupCachedRecord(
            job.expId, siteName=site.name, instrument=job.instrument
        )
        newState = ServerState(
            cacheDir=job.cacheDir,
            cacheBytes=cacheDuSizeBytes(cache_root()),
            meta=job.meta,
            summaries=summaries,
            expId=job.expId,
            tZero=job.tZero,
            siteName=site.name,
            instrument=job.instrument,
            exposureInfo=exposureInfo,
            referencePoints=[
                {
                    "label": _shutterCloseLabel(exposureInfo),
                    "t": job.tZero.isoformat(),
                    "offsetS": 0.0,
                    "source": "shutter close",
                }
            ],
        )
        with ctx.jobs.stateLock:
            ctx.putExposureState(newState)

    return cb


def _trimFloat(v: float) -> str:
    """Render a float for an HTML number field without a pointless ``.0``.

    ``5.0`` in a spinner reads as a setting someone fiddled with; ``5``
    reads as the default it is.
    """
    return str(int(v)) if v == int(v) else str(v)


def _makeHandler(ctx: ServerContext) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        # HTTP/1.0 is the default; that's fine for SSE because the spec
        # supports "read until close" — we just don't get keep-alive.

        def _send_json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_error_json(self, status: int, message: str) -> None:
            self._send_json({"error": message}, status=status)

        def _send_file(self, path: Path) -> None:
            if not path.exists() or not path.is_file():
                self.send_error(404, f"Not found: {path.name}")
                return
            mime, _ = mimetypes.guess_type(path.name)
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_index(self) -> None:
            """Serve the single-page shell with the base path substituted in.

            ``timeline.html`` carries literal placeholders for the handful of
            values that differ between deployments: ``__BASE_PATH__``
            everywhere a URL back to us appears, and the starting values of
            the fetch window fields. Substituting at request time (rather
            than baking it in at build time, as a bundler would) keeps the
            container image environment-agnostic and keeps the no-build-step
            edit-and-reload loop working locally.
            """
            path = TEMPLATES_DIR / "timeline.html"
            if not path.exists():
                self.send_error(404, f"Not found: {path.name}")
                return
            text = path.read_text()
            for placeholder, value in (
                ("__BASE_PATH__", ctx.basePath),
                # Only the *starting* values: the fields stay editable, so
                # widening a window mid-investigation still works. The
                # deployment just decides where they start.
                ("__WINDOW_BEFORE__", _trimFloat(DEFAULT_WINDOW_BEFORE_S)),
                ("__WINDOW_AFTER__", _trimFloat(DEFAULT_WINDOW_AFTER_S)),
            ):
                text = text.replace(placeholder, value)
            body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _routePath(self, path: str) -> str | None:
            """Strip the deployment's base path off an incoming request path.

            Returns the app-relative path (always ``/``-prefixed), or ``None``
            when the request falls outside the base path — which the caller
            turns into a 404 rather than quietly serving the home page to a
            URL that doesn't belong to us.
            """
            base = ctx.basePath
            if not base:
                return path
            if path == base:
                return "/"
            if path.startswith(base + "/"):
                return path[len(base) :]
            return None

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return  # silence default access logs

        def _instrumentParam(self, qs: dict[str, list[str]]) -> tuple[str | None, bool]:
            """Read and validate the optional ``instrument`` query param.

            Returns ``(instrument, ok)``. On an unknown name the 400 has
            already been sent and ``ok`` is ``False`` — the caller just
            returns. One validator for every GET route that accepts the
            pin, so they can't drift on what counts as a known instrument.
            """
            raw = (qs.get("instrument", [""])[0] or "").strip().lower() or None
            if raw is not None and raw not in exposureTimes.INSTRUMENTS_BY_PROBE_ORDER:
                self._send_error_json(
                    400,
                    f"Unknown instrument {raw!r}; known: "
                    f"{list(exposureTimes.INSTRUMENTS_BY_PROBE_ORDER)}",
                )
                return None, False
            return raw, True

        # ----- SSE helper -----

        def _send_sse(self, job: FetchJob) -> None:
            """Stream a job's progress events as Server-Sent Events.

            Replays history then waits on the job's condition variable for
            further events until a terminal one (``done`` / ``error``) is
            sent. Safe for multiple concurrent readers (each starts at
            index 0 of the event list independently).
            """
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            idx = 0
            try:
                while True:
                    with job.condition:
                        # Wait for more events unless the job is already terminal
                        # AND we've drained everything that was produced.
                        while idx >= len(job.events) and not job.isTerminal():
                            job.condition.wait(timeout=15.0)
                        toSend = job.events[idx:]
                        idx = len(job.events)
                        terminalReached = job.isTerminal()
                    for ev in toSend:
                        chunk = f"data: {json.dumps(ev)}\n\n".encode("utf-8")
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    if terminalReached and idx >= len(job.events):
                        return
                    if not toSend:
                        # No events arrived in the wait window — send a comment
                        # ping so clients / proxies don't time out the stream.
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return  # client went away

        # ----- routing -----

        def do_GET(self) -> None:  # noqa: N802 (stdlib API)
            url = urlparse(self.path)
            routed = self._routePath(url.path)
            if routed is None:
                self.send_error(404)
                return
            path = routed
            if path == "/healthz":
                # Deliberately touches no state: it is polled by the
                # deployment's readiness probe, and a probe that can be made
                # to fail by a slow fetch would pull the only pod out of the
                # Service mid-investigation.
                self._send_json({"status": "ok"})
                return
            if path in ("/", "/index.html"):
                self._send_index()
                return
            if path.startswith("/static/"):
                target = _resolveStaticFile(path[len("/static/") :])
                if target is None:
                    self.send_error(404)
                    return
                self._send_file(target)
                return
            if path == "/api/live":
                # Snapshot of the live night poller: watermark, tonight's
                # exposure list with readiness, error states. Static
                # {enabled: false} when live mode isn't running, so the
                # UI can decide whether to render the Tonight panel with
                # one unconditional call.
                if ctx.live is None:
                    self._send_json({"enabled": False})
                else:
                    self._send_json(ctx.live.snapshot())
                return
            if path == "/api/summary":
                # ?dataId=<int> selects a loaded exposure; ?dayObs=<int>
                # selects a loaded night; ?rangeStart=&rangeStop= selects a
                # loaded range (+ optional &dataId= for one exposure's
                # timeline within it); no params -> home view shape.
                qs = parse_qs(url.query)
                dataIdRaw = qs.get("dataId", [""])[0] or None
                dayObsRaw = qs.get("dayObs", [""])[0] or None
                rangeStartRaw = qs.get("rangeStart", [""])[0] or None
                rangeStopRaw = qs.get("rangeStop", [""])[0] or None
                instrumentRaw, ok = self._instrumentParam(qs)
                if not ok:
                    return
                if rangeStartRaw is not None and rangeStopRaw is not None:
                    self._handle_range_summary(rangeStartRaw, rangeStopRaw, dataIdRaw, instrumentRaw)
                    return
                if dataIdRaw is not None:
                    try:
                        dataId = int(dataIdRaw)
                    except ValueError:
                        self._send_error_json(400, "dataId must be an integer")
                        return
                    with ctx.jobs.stateLock:
                        state = ctx.getExposureState(dataId)
                    if (
                        state is not None
                        and instrumentRaw is not None
                        and state.instrument is not None
                        and state.instrument != instrumentRaw
                    ):
                        # Same bare id, different instrument — a
                        # different exposure entirely. Never serve one
                        # instrument's timeline to a tab pinned to the
                        # other; fall through to the rebuild/fetch path.
                        state = None
                    if state is None:
                        # Not loaded in memory — try rebuilding from the
                        # on-disk cache so a deep-linked tab (e.g. the
                        # dataId column in the cache table) doesn't
                        # silently fall back to home view.
                        state = _loadExposureFromCache(ctx, dataId, instrument=instrumentRaw)
                    if state is not None:
                        # Touch the LRU sidecar so eviction sees this
                        # window as freshly used. Done here (rather
                        # than only on fetch) so re-opening a tab
                        # bumps the cache up the LRU even if no fetch
                        # happens.
                        markCacheViewed(state.cacheDir)
                        self._send_json(_buildSummaryPayload(state))
                        return
                    self._send_json({"loaded": False, "cache": _cacheRootInfo()})
                    return
                if dayObsRaw is not None:
                    try:
                        dayObs = int(dayObsRaw)
                    except ValueError:
                        self._send_error_json(400, "dayObs must be an integer")
                        return
                    with ctx.jobs.stateLock:
                        nightState = ctx.getNightState(dayObs)
                    if nightState is None:
                        nightState = _loadNightFromCache(ctx, dayObs)
                    if nightState is not None:
                        markCacheViewed(nightState.cacheDir)
                        self._send_json(_buildNightPayload(nightState))
                        return
                    self._send_json({"loaded": False, "cache": _cacheRootInfo()})
                    return
                self._send_json({"loaded": False, "cache": _cacheRootInfo()})
                return
            if path.startswith("/api/night/traceback/"):
                qs = parse_qs(url.query)
                dayObsRaw = qs.get("dayObs", [""])[0] or None
                nightState = None
                if dayObsRaw is not None:
                    try:
                        dayObs = int(dayObsRaw)
                    except ValueError:
                        self._send_error_json(400, "dayObs must be an integer")
                        return
                    with ctx.jobs.stateLock:
                        nightState = ctx.getNightState(dayObs)
                    if nightState is None:
                        # Same rebuild-from-disk attempt the summary and
                        # pod routes make. Without it, opening a ninth
                        # night evicts the first and every failure row
                        # already on that page 404s until the user
                        # happens to reload the summary.
                        nightState = _loadNightFromCache(ctx, dayObs)
                if nightState is None:
                    self._send_error_json(404, "No night loaded for the requested dayObs")
                    return
                key = unquote(path[len("/api/night/traceback/") :])
                payload = _tracebackContextForNight(nightState, key)
                if payload is None:
                    self._send_error_json(404, f"No traceback with key {key}")
                    return
                self._send_json(payload)
                return
            if path.startswith("/api/pod/"):
                qs = parse_qs(url.query)
                dataIdRaw = qs.get("dataId", [""])[0] or None
                dayObsRaw = qs.get("dayObs", [""])[0] or None
                rangeStartRaw = qs.get("rangeStart", [""])[0] or None
                rangeStopRaw = qs.get("rangeStop", [""])[0] or None
                instrumentRaw, ok = self._instrumentParam(qs)
                if not ok:
                    return
                pod = path[len("/api/pod/") :]
                if not re.match(r"^[A-Za-z0-9._-]+$", pod):
                    self.send_error(400, "Invalid pod name")
                    return
                if rangeStartRaw is not None and rangeStopRaw is not None and dataIdRaw is not None:
                    self._handle_range_pod(rangeStartRaw, rangeStopRaw, dataIdRaw, pod, instrumentRaw)
                    return
                if dataIdRaw is not None:
                    try:
                        dataId = int(dataIdRaw)
                    except ValueError:
                        self._send_error_json(400, "dataId must be an integer")
                        return
                    with ctx.jobs.stateLock:
                        state = ctx.getExposureState(dataId)
                    if (
                        state is not None
                        and instrumentRaw is not None
                        and state.instrument is not None
                        and state.instrument != instrumentRaw
                    ):
                        # Same guard /api/summary applies: the bare id's
                        # in-memory slot may hold the other instrument's
                        # exposure (another tab opened its twin), and that
                        # state's cache dir is a different window — its
                        # lines are a different exposure's, an hour away.
                        state = None
                    if state is None:
                        self._send_error_json(404, f"No exposure loaded for dataId {dataId}")
                        return
                    self._send_json(_podDetail(state, pod))
                    return
                if dayObsRaw is not None:
                    try:
                        dayObs = int(dayObsRaw)
                    except ValueError:
                        self._send_error_json(400, "dayObs must be an integer")
                        return
                    with ctx.jobs.stateLock:
                        nightState = ctx.getNightState(dayObs)
                    if nightState is None:
                        self._send_error_json(404, f"No night loaded for dayObs {dayObs}")
                        return
                    self._send_json(_podDetailForNight(nightState, pod))
                    return
                self._send_error_json(
                    400, "Provide ?dataId=<int>, ?dayObs=<int>, or ?rangeStart=&rangeStop=&dataId="
                )
                return
            if path == "/api/cache":
                self._send_json({"root": _cacheRootInfo(), "windows": _listCacheWindows()})
                return
            if path == "/api/site":
                # Which observatory's logs these are. Read-only: the UI
                # labels the page with it so nobody misreads summit data as
                # BTS data, but it is chosen by where this server runs.
                site = ctx.site()
                self._send_json(
                    {
                        "name": site.name,
                        "cluster": site.cluster,
                        "namespace": site.namespace,
                        "lokiAddr": site.lokiAddr,
                        "consdbUrl": site.consdbUrl,
                    }
                )
                return
            m = re.match(r"^/api/exposure-time/(\d+)$", path)
            if m:
                # An exposure id is only unique within one instrument
                # (see exposureTimes), so the caller may name one.
                instrument, ok = self._instrumentParam(parse_qs(url.query))
                if not ok:
                    return
                self._handle_exposure_time(int(m.group(1)), instrument)
                return
            m = re.match(r"^/api/fetch/([A-Za-z0-9]+)/status$", path)
            if m:
                job = ctx.jobs.getJob(m.group(1))
                if job is None:
                    self._send_error_json(404, "No such job")
                    return
                self._send_json(
                    {
                        "jobId": job.jobId,
                        "status": job.status,
                        "kind": job.kind,
                        "site": job.siteName,
                        "expId": job.expId,
                        "instrument": job.instrument,
                        "tZero": job.tZero.isoformat() if job.tZero else None,
                        "dayObs": job.dayObs,
                        "startId": job.startId,
                        "stopId": job.stopId,
                        "fromIso": job.spec.fromIso,
                        "toIso": job.spec.toIso,
                        "startedAt": job.startedAt.isoformat() if job.startedAt else None,
                        "finishedAt": job.finishedAt.isoformat() if job.finishedAt else None,
                        "cacheDir": str(job.cacheDir) if job.cacheDir else None,
                        "cacheReuse": job.meta.get("cacheReuse"),
                        "error": job.error,
                        "eventCount": len(job.events),
                    }
                )
                return
            m = re.match(r"^/api/fetch/([A-Za-z0-9]+)/progress$", path)
            if m:
                job = ctx.jobs.getJob(m.group(1))
                if job is None:
                    self._send_error_json(404, "No such job")
                    return
                self._send_sse(job)
                return
            self.send_error(404)

        # ----- handler bodies (kept out of do_GET so they don't bloat it) -----

        def _handle_range_summary(
            self,
            rangeStartRaw: str,
            rangeStopRaw: str,
            dataIdRaw: str | None,
            instrument: str | None = None,
        ) -> None:
            """Serve the range index payload, or one dataId's timeline
            within it when ``dataId`` is also supplied.

            ``instrument`` is the same guard the exposure form applies:
            the range key is the bare ``[startId, stopId]`` span, which
            every instrument that observed that many exposures shares, so
            the loaded slot may hold the twin span. A mismatch is treated
            as not-loaded and falls through to the rebuild path, which
            re-pins.
            """
            try:
                startId = int(rangeStartRaw)
                stopId = int(rangeStopRaw)
            except ValueError:
                self._send_error_json(400, "rangeStart / rangeStop must be integers")
                return
            with ctx.jobs.stateLock:
                state = ctx.getRangeState(rangeKey(startId, stopId))
            if state is not None and instrument is not None and state.instrument != instrument:
                state = None
            if state is None:
                state = _loadRangeFromCache(ctx, startId, stopId, instrument)
            if state is None:
                self._send_json({"loaded": False, "cache": _cacheRootInfo()})
                return
            markCacheViewed(state.cacheDir)
            if dataIdRaw is not None:
                try:
                    dataId = int(dataIdRaw)
                except ValueError:
                    self._send_error_json(400, "dataId must be an integer")
                    return
                payload = _buildRangeExposurePayload(state, dataId)
                if payload is None:
                    self._send_error_json(404, f"dataId {dataId} is not in range [{startId}, {stopId}]")
                    return
                self._send_json(payload)
                return
            self._send_json(_buildRangePayload(state))

        def _handle_range_pod(
            self,
            rangeStartRaw: str,
            rangeStopRaw: str,
            dataIdRaw: str,
            pod: str,
            instrument: str | None = None,
        ) -> None:
            try:
                startId = int(rangeStartRaw)
                stopId = int(rangeStopRaw)
                dataId = int(dataIdRaw)
            except ValueError:
                self._send_error_json(400, "rangeStart / rangeStop / dataId must be integers")
                return
            with ctx.jobs.stateLock:
                state = ctx.getRangeState(rangeKey(startId, stopId))
            if state is not None and instrument is not None and state.instrument != instrument:
                # The slot holds the twin span — a different set of
                # exposures out of a different window. Never serve its
                # lines to a tab pinned elsewhere; the next summary
                # reload re-pins.
                state = None
            if state is None:
                self._send_error_json(404, f"No range loaded for [{startId}, {stopId}]")
                return
            transient = _rangeExposureState(state, dataId)
            if transient is None:
                self._send_error_json(404, f"dataId {dataId} is not in range [{startId}, {stopId}]")
                return
            self._send_json(_podDetail(transient, pod))

        def _handle_exposure_time(self, dataId: int, instrument: str | None) -> None:
            # The site picks the (consdbUrl, tokenFile) pair AND which
            # per-site cache file the resolved iso lands in. The same
            # dataId means different things at different sites — BTS
            # simulated values can collide with summit real-camera ids —
            # so the two are never mixed, and the site is the server's,
            # not the caller's.
            #
            # ``instrument`` is different: it is part of the *dataId's*
            # identity, not server configuration, so the caller does get
            # to name it. Omitted, the answer is the probe-order one.
            site = ctx.site()
            # Cache check first: exposure properties are immutable once
            # they exist, so a hit lets us skip the token + network call
            # entirely. This also means a user with no ConsDB token can
            # still resolve any dataId they (or anyone) previously
            # looked up on this machine for *this* site.
            cachedRec = exposureTimes.lookupCachedRecord(dataId, siteName=site.name, instrument=instrument)
            cachedIso = exposureTimes.obsEnd(cachedRec)
            cachedManual = exposureTimes.isManual(cachedRec)
            # A *real* (ConsDB-sourced) cached record is immutable truth —
            # return it with no network call. A *manual* stand-in only fills
            # in for when ConsDB couldn't answer, so we still try ConsDB
            # first and fall back to the manual value below if it can't.
            if cachedRec is not None and cachedIso is not None and not cachedManual:
                self._send_json(
                    {
                        "dataId": dataId,
                        "tZero": cachedIso,
                        "scale": "TAI",
                        "fromCache": True,
                        "manual": False,
                        "site": site.name,
                        "instrument": exposureTimes.recordInstrument(cachedRec),
                        "exposure": cachedRec,
                    }
                )
                return

            def fallbackOrError(status: int, msg: str) -> None:
                # Prefer a real ConsDB answer; only when ConsDB can't resolve
                # the dataId do we surface a previously-stored manual stand-in
                # (so a transient ConsDB outage doesn't strand the user). With
                # no stand-in, the original error stands.
                if cachedRec is not None and cachedIso is not None:
                    self._send_json(
                        {
                            "dataId": dataId,
                            "tZero": cachedIso,
                            "scale": "TAI",
                            "fromCache": True,
                            "manual": True,
                            "site": site.name,
                            "instrument": exposureTimes.recordInstrument(cachedRec),
                            "exposure": cachedRec,
                        }
                    )
                else:
                    self._send_error_json(status, msg)

            tokenPath = site.consdbTokenFile
            token = ""
            if tokenPath is not None:
                # A site with no token file declared talks to a ConsDB that
                # needs no auth (an in-cluster Service); only validate the
                # file when the catalog says there should be one.
                if not tokenPath.exists():
                    fallbackOrError(
                        503,
                        f"ConsDB token file for site {site.name!r} not found at {tokenPath}. "
                        "Get a token from the relevant RSP and drop it there.",
                    )
                    return
                try:
                    token = exposureTimes.readToken(tokenPath)
                except OSError as e:
                    fallbackOrError(503, f"Could not read ConsDB token file: {e}")
                    return
                if not token:
                    fallbackOrError(503, f"ConsDB token file is empty: {tokenPath}")
                    return
            try:
                record = exposureTimes.queryExposureRecord(
                    dataId, token, consdbUrl=site.consdbUrl, instrument=instrument
                )
            except exposureTimes.ConsDbError as e:
                fallbackOrError(502, f"ConsDB query failed: {e}")
                return
            except OSError as e:
                fallbackOrError(503, f"Could not reach ConsDB: {e}")
                return
            isot = exposureTimes.obsEnd(record)
            if record is None or isot is None:
                where = f" on {instrument}" if instrument else ""
                fallbackOrError(404, f"No exposure-time record for dataId={dataId}{where}")
                return
            # A real hit supersedes any manual stand-in we'd stored earlier.
            # An answer for a *pinned* instrument only claims the bare id
            # when that instrument is the one a bare lookup probes first.
            exposureTimes.storeCachedRecord(
                dataId,
                record,
                siteName=site.name,
                bareKey=exposureTimes.isProbeOrderFirst(instrument),
            )
            self._send_json(
                {
                    "dataId": dataId,
                    "tZero": isot,
                    "scale": "TAI",
                    "fromCache": False,
                    "manual": False,
                    "site": site.name,
                    "instrument": exposureTimes.recordInstrument(record),
                    "exposure": record,
                }
            )

        def do_DELETE(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            routed = self._routePath(url.path)
            if routed is None:
                self.send_error(404)
                return
            path = routed
            if path == "/api/cache":
                _deleteCacheRoot(ctx)
                self._send_json({"root": _cacheRootInfo(), "windows": _listCacheWindows()})
                return
            m = re.match(r"^/api/cache/([^/]+)/([^/]+)/([^/]+)(?:/([^/]+))?$", path)
            if m:
                # Percent-decode each segment: the client encodeURIComponent's
                # them, so a night cache's `pods=__aos__` arrives as
                # `pods%3D__aos__`. Decode before matching, then
                # _resolveCacheWindow re-validates the decoded form (rejecting
                # any smuggled `/` or `..`), so this stays traversal-safe.
                cluster, namespace, slug = unquote(m.group(1)), unquote(m.group(2)), unquote(m.group(3))
                podsSub = unquote(m.group(4)) if m.group(4) else None  # optional `pods=<slug>` segment
                target = _resolveCacheWindow(cluster, namespace, slug, podsSub)
                if target is None:
                    self._send_error_json(404, "No such cache directory")
                    return
                _deleteCacheDir(ctx, target)
                self._send_json({"root": _cacheRootInfo(), "windows": _listCacheWindows()})
                return
            self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            path = self._routePath(url.path)
            if path is None:
                self.send_error(404)
                return
            if path == "/api/fetch":
                try:
                    body = _readJsonBody(self)
                except json.JSONDecodeError as e:
                    self._send_error_json(400, f"Bad JSON body: {e}")
                    return
                # Build the spec from the request. Only the question being
                # asked comes from the browser — which exposure, which
                # window. Everything about *how* we talk to Loki is server
                # configuration and is never taken from a request body.
                try:
                    spec, site, expId, tZero, instrument = _buildSpecFromRequest(ctx, body)
                except ValueError as e:
                    self._send_error_json(400, str(e))
                    return
                # A hand-entered shutter close (the dataId didn't resolve via
                # ConsDB) is persisted to the per-site exposure-time cache as
                # a tagged stand-in, so reopening or refreshing the explore
                # view finds a t-zero without the user re-typing it. Stamped
                # with the request's instrument so the pinned lookups that
                # follow (the job's info-box read, later /api/exposure-time
                # calls) can actually find it.
                if body.get("tZeroManual"):
                    # Bare key only when the pin is the probe-order first
                    # instrument — a LATISS stand-in must not become what an
                    # unqualified lookup of the shared id resolves to.
                    exposureTimes.storeCachedRecord(
                        expId,
                        exposureTimes.manualRecord(exposureTimes.utcToTaiIso(tZero), instrument=instrument),
                        siteName=site.name,
                        bareKey=exposureTimes.isProbeOrderFirst(instrument),
                    )
                job = ctx.jobs.createJob(spec, expId, tZero, siteName=site.name, instrument=instrument)
                ctx.jobs.startJob(job, onComplete=_onFetchComplete(ctx))
                self._send_json({"jobId": job.jobId}, status=202)
                return
            if path == "/api/fetch-night":
                try:
                    body = _readJsonBody(self)
                except json.JSONDecodeError as e:
                    self._send_error_json(400, f"Bad JSON body: {e}")
                    return
                try:
                    spec, site, dayObs = _buildNightSpecFromRequest(ctx, body)
                except ValueError as e:
                    self._send_error_json(400, str(e))
                    return
                job = ctx.jobs.createNightJob(spec, dayObs, siteName=site.name)
                ctx.jobs.startJob(job, onComplete=_onFetchComplete(ctx))
                self._send_json({"jobId": job.jobId}, status=202)
                return
            if path == "/api/fetch-range":
                try:
                    body = _readJsonBody(self)
                except json.JSONDecodeError as e:
                    self._send_error_json(400, f"Bad JSON body: {e}")
                    return
                try:
                    spec, site, startId, stopId, tZeroStart, tZeroStop, instrument = (
                        _buildRangeSpecFromRequest(ctx, body)
                    )
                except ValueError as e:
                    self._send_error_json(400, str(e))
                    return
                job = ctx.jobs.createRangeJob(
                    spec, startId, stopId, tZeroStart, tZeroStop, siteName=site.name, instrument=instrument
                )
                ctx.jobs.startJob(job, onComplete=_onFetchComplete(ctx))
                self._send_json({"jobId": job.jobId}, status=202)
                return
            self.send_error(404)

    return Handler


# ----- request body helpers -------------------------------------------------


def _instrumentFromBody(body: dict) -> str:
    """The instrument a fetch body names, defaulting to LSSTCam.

    An exposure id is only unique within one instrument, so every fetch
    is *for* some instrument even when the client didn't say — and the
    default is LSSTCam, the instrument that owns ~all rapid-analysis
    traffic. Raises ``ValueError`` for an unknown name so a typo can't
    silently resolve against the wrong table.
    """
    raw = body.get("instrument")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return "lsstcam"
    if not isinstance(raw, str):
        raise ValueError("instrument must be a string")
    instrument = raw.strip().lower()
    if instrument not in exposureTimes.INSTRUMENTS_BY_PROBE_ORDER:
        raise ValueError(
            f"Unknown instrument {instrument!r}; known: {list(exposureTimes.INSTRUMENTS_BY_PROBE_ORDER)}"
        )
    return instrument


def _buildSpecFromRequest(ctx: ServerContext, body: dict) -> tuple[FetchSpec, Site, int, dt.datetime, str]:
    """Translate a JSON fetch request body into (FetchSpec, Site, expId, tZero).

    The body says *what* to fetch — which exposure, which window. Which
    cluster, which Loki, and which credentials are all deployment
    configuration read from the environment, never from the request:
    a browser cannot point this server at a different Loki or make it
    authenticate as somebody else. Raises ``ValueError`` for
    client-fixable mistakes (missing fields, unparseable timestamp); the
    handler converts those into a 400 response.
    """
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object")

    expIdRaw = body.get("exposureId")
    if expIdRaw is None:
        raise ValueError("exposureId is required")
    try:
        expId = int(expIdRaw)
    except (TypeError, ValueError) as e:
        raise ValueError("exposureId must be an integer") from e

    tZeroStr = body.get("tZero")
    if not tZeroStr:
        raise ValueError("tZero is required (ISO-8601 string)")
    tZeroInput = _parseClientIso(tZeroStr)
    if body.get("tZeroUtc", False):
        tZero = tZeroInput
    else:
        tZero = tZeroInput - dt.timedelta(seconds=exposureTimes.TAI_MINUS_UTC_S)

    windowBefore = _floatField(body, "windowBefore", DEFAULT_WINDOW_BEFORE_S)
    windowAfter = _floatField(body, "windowAfter", DEFAULT_WINDOW_AFTER_S)
    fromT = tZero - dt.timedelta(seconds=windowBefore)
    toT = tZero + dt.timedelta(seconds=windowAfter)

    site = ctx.site()
    spec = FetchSpec(
        lokiAddr=site.lokiAddr,
        username=DEFAULT_USERNAME,
        cluster=site.cluster,
        namespace=site.namespace,
        fromIso=_isoForLogcli(fromT),
        toIso=_isoForLogcli(toT),
        workers=DEFAULT_WORKERS,
    )
    return spec, site, expId, tZero, _instrumentFromBody(body)


def _buildNightSpecFromRequest(ctx: ServerContext, body: dict) -> tuple[FetchSpec, Site, int]:
    """Translate a JSON night-fetch request body into (FetchSpec, Site, dayObs)."""
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object")
    dayObsRaw = body.get("dayObs")
    if dayObsRaw is None:
        raise ValueError("dayObs is required")
    try:
        dayObs = int(dayObsRaw)
    except (TypeError, ValueError) as e:
        raise ValueError("dayObs must be an integer YYYYMMDD") from e
    if dayObs < 19000000 or dayObs > 30000000:
        raise ValueError(f"dayObs {dayObs} doesn't look like a YYYYMMDD integer")

    fromT = dayObsStartUtc(dayObs)
    toT = dayObsEndUtc(dayObs)

    site = ctx.site()
    spec = FetchSpec(
        lokiAddr=site.lokiAddr,
        username=DEFAULT_USERNAME,
        cluster=site.cluster,
        namespace=site.namespace,
        fromIso=_isoForLogcli(fromT),
        toIso=_isoForLogcli(toT),
        workers=DEFAULT_WORKERS,
        podRegex=NIGHT_AOS_POD_REGEX,
    )
    return spec, site, dayObs


def _buildRangeSpecFromRequest(
    ctx: ServerContext, body: dict
) -> tuple[FetchSpec, Site, int, int, dt.datetime, dt.datetime, str]:
    """Translate a JSON range-fetch body into (FetchSpec, Site, startId,
    stopId, tZeroStartUtc, tZeroStopUtc, instrument).

    The window is one wide span: ``[tZeroStart - windowBefore,
    tZeroStop + windowAfter]`` with no pod filter (all pods, like a single
    exposure). The client resolves the two shutter-close anchors up front
    and passes them as ``tZeroStart`` / ``tZeroStop`` (TAI by default).
    """
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object")

    startId = _requireRangeInt(body, "rangeStart")
    stopId = _requireRangeInt(body, "rangeStop")
    if stopId <= startId:
        raise ValueError("rangeStop must be greater than rangeStart")
    if stopId - startId > MAX_RANGE_SPAN:
        raise ValueError(
            f"range span {stopId - startId} exceeds the {MAX_RANGE_SPAN}-exposure limit "
            "(this mode is for tens of consecutive exposures)"
        )

    tZeroStart = _parseRangeAnchor(body, "tZeroStart")
    tZeroStop = _parseRangeAnchor(body, "tZeroStop")
    if not body.get("tZeroUtc", False):
        tZeroStart = tZeroStart - dt.timedelta(seconds=exposureTimes.TAI_MINUS_UTC_S)
        tZeroStop = tZeroStop - dt.timedelta(seconds=exposureTimes.TAI_MINUS_UTC_S)

    windowBefore = _floatField(body, "windowBefore", DEFAULT_WINDOW_BEFORE_S)
    windowAfter = _floatField(body, "windowAfter", DEFAULT_WINDOW_AFTER_S)
    fromT = tZeroStart - dt.timedelta(seconds=windowBefore)
    toT = tZeroStop + dt.timedelta(seconds=windowAfter)

    site = ctx.site()
    spec = FetchSpec(
        lokiAddr=site.lokiAddr,
        username=DEFAULT_USERNAME,
        cluster=site.cluster,
        namespace=site.namespace,
        fromIso=_isoForLogcli(fromT),
        toIso=_isoForLogcli(toT),
        workers=DEFAULT_WORKERS,
    )
    return spec, site, startId, stopId, tZeroStart, tZeroStop, _instrumentFromBody(body)


def _requireRangeInt(body: dict, field: str) -> int:
    raw = body.get(field)
    if raw is None:
        raise ValueError(f"{field} is required")
    try:
        return int(raw)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{field} must be an integer") from e


def _parseRangeAnchor(body: dict, field: str) -> dt.datetime:
    raw = body.get(field)
    if not raw:
        raise ValueError(f"{field} is required (ISO-8601 string)")
    return _parseClientIso(str(raw))


def _floatField(body: dict, field: str, default: float) -> float:
    """Parse a float body field, treating missing / null / blank as the
    default — an empty browser number input serializes to JSON ``null``,
    and ``float(None)`` would otherwise raise an *uncaught* ``TypeError``
    (the handler only catches ``ValueError``), dropping the connection
    with no response. A genuinely non-numeric string still raises
    ``ValueError`` → 400. ``0`` is preserved (a legitimate window).
    """
    val = body.get(field)
    if val is None or val == "":
        return default
    return float(val)


def _parseClientIso(s: str) -> dt.datetime:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if "+" not in s and "-" not in s[10:]:
        s = s + "+00:00"
    try:
        return dt.datetime.fromisoformat(s).astimezone(dt.timezone.utc)
    except ValueError as e:
        raise ValueError(f"tZero is not a valid ISO-8601 timestamp: {e}") from e


def _isoForLogcli(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


# ----- public entry point ---------------------------------------------------


def serve(host: str, port: int, ctx: ServerContext) -> None:
    handler = _makeHandler(ctx)
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"Serving on http://{host}:{port}{ctx.basePath}/ (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        httpd.server_close()
