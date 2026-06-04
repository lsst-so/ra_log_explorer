"""Stdlib HTTP server: timeline UI + JSON API + on-demand fetches.

The server runs against a long-lived :class:`ServerContext` that holds:

  * two LRU-ordered dicts of loaded states (one per exposure / night),
  * a :class:`~ra_log_explorer.jobs.JobManager` for fetch jobs the user
    kicks off from the home page,
  * the single ``stateLock`` that guards both keyed-state dicts.

HTTP surface (see ``architecture/architecture.md`` for the full schema):

  GET    /                                       timeline.html (home + explore + night SPA)
  GET    /static/*                                static assets
  GET    /api/summary?dataId=<>|?dayObs=<>        view-state lookup by exposure or dayObs
  GET    /api/pod/<pod>?dataId=<>|?dayObs=<>      full parsed log for one pod
  GET    /api/night/traceback/<bodyKey>?dayObs=<> per-failure drilldown
  GET    /api/cache                               list of cached windows on disk
  DELETE /api/cache                               delete the entire cache
  DELETE /api/cache/<cluster>/<ns>/<slug>[/<pods=…>]  delete one cached window
  GET    /api/exposure-time/<dataId>?site=<name>  dataId -> shutter-close (TAI) lookup
  GET    /api/sites                               site catalog (cluster + ConsDB pairings)
  GET    /api/settings                            current persisted server-side settings
  PUT    /api/settings                            update server-side settings
  POST   /api/fetch                               start an exposure fetch; returns {jobId}
  POST   /api/fetch-night                         start a night fetch; returns {jobId}
  GET    /api/fetch/<id>/status                   JSON snapshot of a fetch job
  GET    /api/fetch/<id>/progress                 SSE stream of fetch progress events
"""

from __future__ import annotations

import datetime as dt
import json
import mimetypes
import os
import re
import shutil
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import appSettings, exposureTimes, night
from . import parse as parser
from .config import (
    DEFAULT_WINDOW_AFTER_S,
    DEFAULT_WINDOW_BEFORE_S,
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
    evictToFit,
    getCacheExposureIds,
    getCacheLastViewed,
    getCacheRange,
    loadCacheMeta,
    loadPodLogPath,
    markCacheRange,
    markCacheViewed,
)
from .jobs import FetchJob, JobManager
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
    # dataId -> shutter-close (t₀) UTC datetime for every exposure in
    # [startId, stopId] that ConsDB knew about. Ids absent here are the
    # "skipped" integers the user was warned to expect.
    shutterCloseByExpId: dict[int, dt.datetime] = field(default_factory=dict)


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

    ``sites`` is the per-deployment catalog (see :mod:`.sites`), loaded
    once at process start; ``defaultSiteName`` is the catalog's named
    fallback when a request body / query string omits ``site``.
    """

    jobs: JobManager
    sites: list[Site] = field(default_factory=list)
    defaultSiteName: str = ""
    exposureStates: "OrderedDict[int, ServerState]" = field(default_factory=OrderedDict)
    nightStates: "OrderedDict[int, NightState]" = field(default_factory=OrderedDict)
    # Keyed by ``rangeKey(startId, stopId)``.
    rangeStates: "OrderedDict[str, RangeState]" = field(default_factory=OrderedDict)

    def siteForRequest(self, name: str | None) -> Site:
        """Return the named site, falling back to the catalog default
        when ``name`` is missing or blank. Raises
        :class:`SitesConfigError` for an unknown name so the handler
        surfaces a 400.
        """
        return siteByName(self.sites, name or self.defaultSiteName)

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

    relevant: list[parser.Event] = []
    for ev in s.events:
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


def _buildSummaryPayload(state: ServerState) -> dict:
    matchingSummaries = parser.podsForTimeline(state.summaries, state.expId)
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
        "expId": state.expId,
        "tZero": state.tZero.isoformat(),
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
        ],
    }


def _podDetail(state: ServerState, pod: str) -> dict:
    logPath = loadPodLogPath(state.cacheDir, pod)
    group = parser.podGroup(pod)
    isCarryover = group in parser.carryoverGroups()
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
                "offsetS": (ln.timestamp - state.tZero).total_seconds(),
                "level": ln.level,
                "logger": ln.logger,
                "function": ln.function,
                "message": ln.message,
                "raw": ln.raw,
                "expId": inferred,
            }
        )
    return {"pod": pod, "lines": lines}


def _podDetailForNight(state: NightState, pod: str) -> dict:
    """Same line-by-line shape as :func:`_podDetail`, but ``offsetS`` is
    measured from the dayObs start (noon UTC) rather than from a single
    shutter close — there is no per-pod shutter close in night mode.
    """
    logPath = loadPodLogPath(state.cacheDir, pod)
    group = parser.podGroup(pod)
    isCarryover = group in parser.carryoverGroups()
    currentExpId: int | None = None
    lines: list[dict] = []
    for ln in parser.iterPodLines(logPath):
        found = parser.extractExpId(ln.raw)
        if found is not None:
            currentExpId = found
        inferred = currentExpId if isCarryover else found
        lines.append(
            {
                "t": ln.timestamp.isoformat(),
                "offsetS": (ln.timestamp - state.startTime).total_seconds(),
                "level": ln.level,
                "logger": ln.logger,
                "function": ln.function,
                "message": ln.message,
                "raw": ln.raw,
                "expId": inferred,
            }
        )
    return {"pod": pod, "lines": lines}


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
    _resolveShutterClosesInto(needIds, state.shutterCloseByExpId, job, site)


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
    _resolveShutterClosesInto(needIds, state.shutterCloseByExpId, job, site)


def _resolveShutterClosesInto(
    needIds: set[int],
    target: dict[int, dt.datetime],
    job: FetchJob,
    site: Site,
) -> None:
    """Resolve a shutter close (t₀, UTC) for each id in ``needIds`` into
    ``target`` — on-disk per-site cache first, then one batched ConsDB
    query for the misses — pushing the ``shutter-close`` progress events
    the SSE consumer renders. Shared by the night and range post-parse
    prefetch paths.
    """
    job.push({"type": "shutter-close", "phase": "starting", "total": len(needIds), "site": site.name})

    # 1) Cache lookups — free, instant.
    misses: list[int] = []
    cachedHits = 0
    for expId in needIds:
        iso = exposureTimes.lookupCached(expId, siteName=site.name)
        if iso is None:
            misses.append(expId)
            continue
        target[expId] = _taiIsoToUtc(iso)
        cachedHits += 1
    job.push(
        {
            "type": "shutter-close",
            "phase": "cache-checked",
            "cacheHits": cachedHits,
            "remaining": len(misses),
        }
    )
    if not misses:
        return

    # 2) Batch ConsDB queries (one per instrument, chunked) — needs a token.
    tokenPath = site.consdbTokenFile
    if not tokenPath.exists():
        job.push(
            {
                "type": "shutter-close",
                "phase": "no-token",
                "remaining": len(misses),
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
        job.push({"type": "shutter-close", "phase": "empty-token", "remaining": len(misses)})
        return
    try:
        resolved = exposureTimes.queryIsotBatch(misses, token, consdbUrl=site.consdbUrl)
    except (exposureTimes.ConsDbError, OSError) as e:
        job.push({"type": "shutter-close", "phase": "consdb-error", "error": str(e)})
        return
    for expId, iso in resolved.items():
        exposureTimes.storeCached(expId, iso, siteName=site.name)
        target[expId] = _taiIsoToUtc(iso)
    job.push(
        {
            "type": "shutter-close",
            "phase": "done",
            "consdbHits": len(resolved),
            "stillMissing": len(misses) - len(resolved),
        }
    )


def _taiIsoToUtc(taiIso: str) -> dt.datetime:
    """Parse a ConsDB ``obs_end`` (TAI ISO, no tz) into a UTC datetime."""
    return parser._parseTimestamp(taiIso + "Z") - dt.timedelta(seconds=exposureTimes.TAI_MINUS_UTC_S)


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

    return {
        "loaded": True,
        "mode": "night",
        "site": state.siteName,
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
        },
        "errorsByType": [_toJsonable(r) for r in errType],
        "errorsByPod": [_toJsonable(r) for r in errPod],
        "histograms": {
            "firstTaskStart": _toJsonable(histFirst),
            "calcZernikesEnd": _toJsonable(histCz),
        },
        "failures": [_toJsonable(r) for r in failures],
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
        }
        for expId in sorted(state.shutterCloseByExpId)
    ]
    nCandidates = state.stopId - state.startId + 1
    return {
        "loaded": True,
        "mode": "range",
        "site": state.siteName,
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
    payload["podDetailQuery"] = f"rangeStart={state.startId}&rangeStop={state.stopId}&dataId={dataId}"
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
    metaPath = window / "_meta.json"
    if not metaPath.exists() or (window / ".partial").exists():
        return
    try:
        meta = json.loads(metaPath.read_text())
    except (OSError, json.JSONDecodeError):
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
    # below).
    exposureIds: list[int] = getCacheExposureIds(window) if kind == "exposure" else []
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
            "exposureIds": exposureIds,
            "fromIso": spec.get("fromIso"),
            "toIso": spec.get("toIso"),
            "fetchedAt": meta.get("fetched_at"),
            "lastViewedAt": lastViewed.isoformat() if lastViewed else None,
            "podCount": meta.get("pod_count", 0),
            "totalBytes": meta.get("total_bytes", 0),
            "sizeOnDisk": cacheDuSizeBytes(window),
        }
    )


def _cacheRootInfo() -> dict:
    root = cache_root()
    return {
        "path": str(root),
        "totalBytes": cacheDuSizeBytes(root),
    }


def _findExposureCacheDir(expId: int) -> Path | None:
    """Return the most-recently-fetched exposure cache containing ``expId``.

    Walks the cache root, skips partial / night-mode caches, and picks the
    cache with the latest ``fetched_at`` whose ``_exposure_ids.txt`` lists
    this dataId. Returns ``None`` if no such cache exists.
    """
    root = cache_root()
    if not root.exists():
        return None
    best: tuple[str, Path] | None = None
    for cluster in root.iterdir():
        if not cluster.is_dir():
            continue
        for ns in cluster.iterdir():
            if not ns.is_dir():
                continue
            for window in ns.iterdir():
                if not window.is_dir():
                    continue
                metaPath = window / META_NAME
                if not metaPath.exists() or (window / PARTIAL_FLAG).exists():
                    continue
                try:
                    meta = json.loads(metaPath.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                spec = meta.get("spec") or {}
                if spec.get("podRegex"):
                    continue  # night-mode cache; lives one level deeper
                if expId not in getCacheExposureIds(window):
                    continue
                fetchedAt = str(meta.get("fetched_at") or "")
                if best is None or fetchedAt > best[0]:
                    best = (fetchedAt, window)
    return best[1] if best else None


def _findNightCacheDir(dayObs: int) -> Path | None:
    """Return the most-recently-fetched night cache for ``dayObs``."""
    root = cache_root()
    if not root.exists():
        return None
    best: tuple[str, Path] | None = None
    for cluster in root.iterdir():
        if not cluster.is_dir():
            continue
        for ns in cluster.iterdir():
            if not ns.is_dir():
                continue
            for window in ns.iterdir():
                if not window.is_dir():
                    continue
                for inner in window.iterdir():
                    if not inner.is_dir() or not inner.name.startswith("pods="):
                        continue
                    metaPath = inner / META_NAME
                    if not metaPath.exists() or (inner / PARTIAL_FLAG).exists():
                        continue
                    try:
                        meta = json.loads(metaPath.read_text())
                    except (OSError, json.JSONDecodeError):
                        continue
                    spec = meta.get("spec") or {}
                    if not spec.get("podRegex"):
                        continue
                    fromIso = spec.get("fromIso")
                    if not isinstance(fromIso, str):
                        continue
                    try:
                        f = dt.datetime.fromisoformat(fromIso.replace("Z", "+00:00"))
                    except (TypeError, ValueError):
                        continue
                    if int(f.strftime("%Y%m%d")) != dayObs:
                        continue
                    fetchedAt = str(meta.get("fetched_at") or "")
                    if best is None or fetchedAt > best[0]:
                        best = (fetchedAt, inner)
    return best[1] if best else None


def _loadExposureFromCache(ctx: ServerContext, expId: int) -> ServerState | None:
    """Reconstruct a :class:`ServerState` from disk for ``expId``, if possible.

    Lets the user open a deep-linked exposure URL (e.g. from the cache
    table in another tab) without forcing a re-fetch — the cache + the
    on-disk exposure-time record together carry everything we need to
    rebuild the in-memory state. Returns ``None`` if either is missing.

    The site is derived from the cache path's cluster component (the
    layout is ``<cache_root>/<cluster>/<namespace>/<window>/…``), so the
    right per-site exposure-time cache gets consulted for the shutter
    close.
    """
    cacheDir = _findExposureCacheDir(expId)
    if cacheDir is None:
        return None
    site = _siteForCacheDir(ctx, cacheDir)
    if site is None:
        return None
    tZeroIso = exposureTimes.lookupCached(expId, siteName=site.name)
    if tZeroIso is None:
        return None
    try:
        meta = loadCacheMeta(cacheDir)
    except (OSError, json.JSONDecodeError, FileNotFoundError):
        return None
    tZero = _taiIsoToUtc(tZeroIso)
    summaries = parser.summarizeAll(cacheDir)
    state = ServerState(
        cacheDir=cacheDir,
        cacheBytes=cacheDuSizeBytes(cache_root()),
        meta=meta,
        summaries=summaries,
        expId=expId,
        tZero=tZero,
        siteName=site.name,
        referencePoints=[
            {
                "label": "shutter close (caller-supplied)",
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
    for needId in _neededDataIdsForNight(summaries):
        iso = exposureTimes.lookupCached(needId, siteName=site.name)
        if iso is not None:
            state.shutterCloseByExpId[needId] = _taiIsoToUtc(iso)
    with ctx.jobs.stateLock:
        ctx.putNightState(state)
    markCacheViewed(cacheDir)
    return state


def _findRangeCacheDir(startId: int, stopId: int) -> Path | None:
    """Return the most-recently-fetched range cache for ``[startId, stopId]``.

    Range caches are exposure-style (top-level, all pods) and identified
    by the ``_range.txt`` sidecar, so we walk the same depth as
    :func:`_findExposureCacheDir` and match on the recorded bounds.
    """
    root = cache_root()
    if not root.exists():
        return None
    best: tuple[str, Path] | None = None
    for cluster in root.iterdir():
        if not cluster.is_dir():
            continue
        for ns in cluster.iterdir():
            if not ns.is_dir():
                continue
            for window in ns.iterdir():
                if not window.is_dir():
                    continue
                metaPath = window / META_NAME
                if not metaPath.exists() or (window / PARTIAL_FLAG).exists():
                    continue
                if getCacheRange(window) != (startId, stopId):
                    continue
                try:
                    meta = json.loads(metaPath.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                fetchedAt = str(meta.get("fetched_at") or "")
                if best is None or fetchedAt > best[0]:
                    best = (fetchedAt, window)
    return best[1] if best else None


def _loadRangeFromCache(ctx: ServerContext, startId: int, stopId: int) -> RangeState | None:
    """Reconstruct a :class:`RangeState` from disk, if possible.

    Mirrors :func:`_loadNightFromCache`: shutter closes are read from the
    on-disk per-site cache only (no ConsDB call — there's no SSE channel
    on a sync /api/summary request), so any dataId not already cached
    locally stays absent until a real fetch fills it in.
    """
    cacheDir = _findRangeCacheDir(startId, stopId)
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
    )
    for expId in range(startId, stopId + 1):
        iso = exposureTimes.lookupCached(expId, siteName=site.name)
        if iso is not None:
            state.shutterCloseByExpId[expId] = _taiIsoToUtc(iso)
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
    shutil.rmtree(target)
    # Tidy up empty parents.
    parent = target.parent
    while parent != cache_root() and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()
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
        shutil.rmtree(root)
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
        # cache (exposure jobs only; night jobs are keyed by dayObs).
        # Done before eviction so a later eviction pass can't race
        # with the sidecar write.
        if job.kind != "night" and job.expId is not None:
            addExposureToCache(job.cacheDir, job.expId)
        # Run LRU eviction so the on-disk total stays at or under the
        # configured cap. The just-fetched cache is exempted; we
        # accept a brief over-cap state during the fetch itself and
        # only sweep at the end.
        settings = appSettings.loadAppSettings()
        evictToFit(settings.maxCacheBytes, exempt=[job.cacheDir])
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
            )
            # Seed the two anchors the client already resolved so even a
            # token-less server has start + stop; the prefetch fills the
            # middle (and re-confirms these from cache).
            newRange.shutterCloseByExpId[job.startId] = job.tZeroStart
            newRange.shutterCloseByExpId[job.stopId] = job.tZeroStop
            _prefetchRangeShutterCloses(newRange, job, site)
            markCacheRange(job.cacheDir, job.startId, job.stopId)
            with ctx.jobs.stateLock:
                ctx.putRangeState(newRange)
            return
        assert job.expId is not None and job.tZero is not None
        newState = ServerState(
            cacheDir=job.cacheDir,
            cacheBytes=cacheDuSizeBytes(cache_root()),
            meta=job.meta,
            summaries=summaries,
            expId=job.expId,
            tZero=job.tZero,
            siteName=site.name,
            referencePoints=[
                {
                    "label": "shutter close (caller-supplied)",
                    "t": job.tZero.isoformat(),
                    "offsetS": 0.0,
                    "source": "shutter close",
                }
            ],
        )
        with ctx.jobs.stateLock:
            ctx.putExposureState(newState)

    return cb


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

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return  # silence default access logs

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
            path = url.path
            if path in ("/", "/index.html"):
                self._send_file(TEMPLATES_DIR / "timeline.html")
                return
            if path.startswith("/static/"):
                rel = path[len("/static/") :]
                if ".." in rel.split("/"):
                    self.send_error(400)
                    return
                self._send_file(STATIC_DIR / rel)
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
                if rangeStartRaw is not None and rangeStopRaw is not None:
                    self._handle_range_summary(rangeStartRaw, rangeStopRaw, dataIdRaw)
                    return
                if dataIdRaw is not None:
                    try:
                        dataId = int(dataIdRaw)
                    except ValueError:
                        self._send_error_json(400, "dataId must be an integer")
                        return
                    with ctx.jobs.stateLock:
                        state = ctx.getExposureState(dataId)
                    if state is None:
                        # Not loaded in memory — try rebuilding from the
                        # on-disk cache so a deep-linked tab (e.g. the
                        # dataId column in the cache table) doesn't
                        # silently fall back to home view.
                        state = _loadExposureFromCache(ctx, dataId)
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
                pod = path[len("/api/pod/") :]
                if not re.match(r"^[A-Za-z0-9._-]+$", pod):
                    self.send_error(400, "Invalid pod name")
                    return
                if rangeStartRaw is not None and rangeStopRaw is not None and dataIdRaw is not None:
                    self._handle_range_pod(rangeStartRaw, rangeStopRaw, dataIdRaw, pod)
                    return
                if dataIdRaw is not None:
                    try:
                        dataId = int(dataIdRaw)
                    except ValueError:
                        self._send_error_json(400, "dataId must be an integer")
                        return
                    with ctx.jobs.stateLock:
                        state = ctx.getExposureState(dataId)
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
            if path == "/api/settings":
                s = appSettings.loadAppSettings()
                # ``effectiveCacheRoot`` resolves through the same
                # priority order as a live fetch (env var → persisted
                # cacheDir → default) so the UI can show the user what
                # the server will *actually* use, not just what they
                # typed.
                self._send_json(
                    {
                        "maxCacheBytes": s.maxCacheBytes,
                        "cacheDir": s.cacheDir,
                        "effectiveCacheRoot": str(cache_root()),
                    }
                )
                return
            if path == "/api/sites":
                self._send_json(
                    {
                        "default_site": ctx.defaultSiteName,
                        "sites": [
                            {
                                "name": s.name,
                                "cluster": s.cluster,
                                "namespace": s.namespace,
                                "lokiAddr": s.lokiAddr,
                                "consdbUrl": s.consdbUrl,
                            }
                            for s in ctx.sites
                        ],
                    }
                )
                return
            m = re.match(r"^/api/exposure-time/(\d+)$", path)
            if m:
                qs = parse_qs(url.query)
                siteValues = qs.get("site")
                siteName: str | None = siteValues[0] if siteValues else None
                self._handle_exposure_time(int(m.group(1)), siteName)
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

        def _handle_range_summary(self, rangeStartRaw: str, rangeStopRaw: str, dataIdRaw: str | None) -> None:
            """Serve the range index payload, or one dataId's timeline
            within it when ``dataId`` is also supplied."""
            try:
                startId = int(rangeStartRaw)
                stopId = int(rangeStopRaw)
            except ValueError:
                self._send_error_json(400, "rangeStart / rangeStop must be integers")
                return
            with ctx.jobs.stateLock:
                state = ctx.getRangeState(rangeKey(startId, stopId))
            if state is None:
                state = _loadRangeFromCache(ctx, startId, stopId)
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

        def _handle_range_pod(self, rangeStartRaw: str, rangeStopRaw: str, dataIdRaw: str, pod: str) -> None:
            try:
                startId = int(rangeStartRaw)
                stopId = int(rangeStopRaw)
                dataId = int(dataIdRaw)
            except ValueError:
                self._send_error_json(400, "rangeStart / rangeStop / dataId must be integers")
                return
            with ctx.jobs.stateLock:
                state = ctx.getRangeState(rangeKey(startId, stopId))
            if state is None:
                self._send_error_json(404, f"No range loaded for [{startId}, {stopId}]")
                return
            transient = _rangeExposureState(state, dataId)
            if transient is None:
                self._send_error_json(404, f"dataId {dataId} is not in range [{startId}, {stopId}]")
                return
            self._send_json(_podDetail(transient, pod))

        def _handle_exposure_time(self, dataId: int, siteName: str | None) -> None:
            # Site picks the (consdbUrl, tokenFile) pair AND which
            # per-site cache file the resolved iso lands in. The same
            # dataId means different things at different sites — BTS
            # simulated values can collide with summit real-camera ids
            # — so we never mix them.
            try:
                site = ctx.siteForRequest(siteName)
            except SitesConfigError as e:
                self._send_error_json(400, str(e))
                return
            # Cache check first: exposure end-times are immutable once
            # they exist, so a hit lets us skip the token + network call
            # entirely. This also means a user with no ConsDB token can
            # still resolve any dataId they (or anyone) previously
            # looked up on this machine for *this* site.
            cached = exposureTimes.lookupCached(dataId, siteName=site.name)
            if cached is not None:
                self._send_json(
                    {
                        "dataId": dataId,
                        "tZero": cached,
                        "scale": "TAI",
                        "fromCache": True,
                        "site": site.name,
                    }
                )
                return
            path = site.consdbTokenFile
            if not path.exists():
                self._send_error_json(
                    503,
                    f"ConsDB token file for site {site.name!r} not found at {path}. "
                    "Get a token from the relevant RSP and drop it there.",
                )
                return
            try:
                token = exposureTimes.readToken(path)
            except OSError as e:
                self._send_error_json(503, f"Could not read ConsDB token file: {e}")
                return
            if not token:
                self._send_error_json(503, f"ConsDB token file is empty: {path}")
                return
            try:
                isot = exposureTimes.queryIsot(dataId, token, consdbUrl=site.consdbUrl)
            except exposureTimes.ConsDbError as e:
                self._send_error_json(502, f"ConsDB query failed: {e}")
                return
            except OSError as e:
                self._send_error_json(503, f"Could not reach ConsDB: {e}")
                return
            if isot is None:
                self._send_error_json(404, f"No exposure-time record for dataId={dataId}")
                return
            exposureTimes.storeCached(dataId, isot, siteName=site.name)
            self._send_json(
                {
                    "dataId": dataId,
                    "tZero": isot,
                    "scale": "TAI",
                    "fromCache": False,
                    "site": site.name,
                }
            )

        def do_DELETE(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            path = url.path
            if path == "/api/cache":
                _deleteCacheRoot(ctx)
                self._send_json({"root": _cacheRootInfo(), "windows": _listCacheWindows()})
                return
            m = re.match(r"^/api/cache/([^/]+)/([^/]+)/([^/]+)(?:/([^/]+))?$", path)
            if m:
                cluster, namespace, slug = m.group(1), m.group(2), m.group(3)
                podsSub = m.group(4)  # optional `pods=<slug>` segment
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
            if url.path == "/api/fetch":
                try:
                    body = _readJsonBody(self)
                except json.JSONDecodeError as e:
                    self._send_error_json(400, f"Bad JSON body: {e}")
                    return
                # Build the spec from the request, applying server-side defaults
                # where the client didn't supply a value. We never trust the
                # client to set arbitrary host/proto values for logcli.
                try:
                    spec, site, expId, tZero, password = _buildSpecFromRequest(ctx, body)
                except ValueError as e:
                    self._send_error_json(400, str(e))
                    return
                # The password — if any — is consumed by the fetch worker
                # thread (it sets LOKI_PASSWORD in the subprocess env) and
                # never persisted, returned, or logged.
                _maybeSetLokiPassword(password)
                job = ctx.jobs.createJob(spec, expId, tZero, siteName=site.name)
                ctx.jobs.startJob(job, onComplete=_onFetchComplete(ctx))
                self._send_json({"jobId": job.jobId}, status=202)
                return
            if url.path == "/api/fetch-night":
                try:
                    body = _readJsonBody(self)
                except json.JSONDecodeError as e:
                    self._send_error_json(400, f"Bad JSON body: {e}")
                    return
                try:
                    spec, site, dayObs, password = _buildNightSpecFromRequest(ctx, body)
                except ValueError as e:
                    self._send_error_json(400, str(e))
                    return
                _maybeSetLokiPassword(password)
                job = ctx.jobs.createNightJob(spec, dayObs, siteName=site.name)
                ctx.jobs.startJob(job, onComplete=_onFetchComplete(ctx))
                self._send_json({"jobId": job.jobId}, status=202)
                return
            if url.path == "/api/fetch-range":
                try:
                    body = _readJsonBody(self)
                except json.JSONDecodeError as e:
                    self._send_error_json(400, f"Bad JSON body: {e}")
                    return
                try:
                    spec, site, startId, stopId, tZeroStart, tZeroStop, password = _buildRangeSpecFromRequest(
                        ctx, body
                    )
                except ValueError as e:
                    self._send_error_json(400, str(e))
                    return
                _maybeSetLokiPassword(password)
                job = ctx.jobs.createRangeJob(
                    spec, startId, stopId, tZeroStart, tZeroStop, siteName=site.name
                )
                ctx.jobs.startJob(job, onComplete=_onFetchComplete(ctx))
                self._send_json({"jobId": job.jobId}, status=202)
                return
            self.send_error(404)

        def do_PUT(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            if url.path == "/api/settings":
                try:
                    body = _readJsonBody(self)
                except json.JSONDecodeError as e:
                    self._send_error_json(400, f"Bad JSON body: {e}")
                    return
                # Start from whatever's currently persisted so a PUT
                # that touches only one field doesn't accidentally
                # blank out the other.
                current = appSettings.loadAppSettings()
                maxCacheBytes = current.maxCacheBytes
                if "maxCacheBytes" in body:
                    raw = body["maxCacheBytes"]
                    try:
                        maxCacheBytes = int(raw)
                    except (TypeError, ValueError):
                        self._send_error_json(400, "maxCacheBytes must be an integer")
                        return
                    if maxCacheBytes < 0:
                        self._send_error_json(400, "maxCacheBytes must be non-negative")
                        return
                cacheDir: str | None = current.cacheDir
                if "cacheDir" in body:
                    raw = body["cacheDir"]
                    if raw is None or (isinstance(raw, str) and not raw.strip()):
                        cacheDir = None
                    elif isinstance(raw, str):
                        candidate = Path(raw).expanduser()
                        try:
                            candidate.mkdir(parents=True, exist_ok=True)
                        except OSError as e:
                            self._send_error_json(400, f"Could not create cacheDir {candidate}: {e}")
                            return
                        cacheDir = str(candidate)
                    else:
                        self._send_error_json(400, "cacheDir must be a string or null")
                        return
                appSettings.saveAppSettings(
                    appSettings.AppSettings(maxCacheBytes=maxCacheBytes, cacheDir=cacheDir)
                )
                self._send_json(
                    {
                        "maxCacheBytes": maxCacheBytes,
                        "cacheDir": cacheDir,
                        "effectiveCacheRoot": str(cache_root()),
                    }
                )
                return
            self.send_error(404)

    return Handler


# ----- request body helpers -------------------------------------------------


def _buildSpecFromRequest(
    ctx: ServerContext, body: dict
) -> tuple[FetchSpec, Site, int, dt.datetime, str | None]:
    """Translate a JSON fetch request body into (FetchSpec, Site, expId, tZero, password).

    The body's ``site`` field selects which site catalog entry to pull
    ``lokiAddr`` / ``cluster`` / ``namespace`` from; the client no
    longer sets those individually. Raises ``ValueError`` for
    client-fixable mistakes (missing fields, unparseable timestamp,
    unknown site); the handler converts those into a 400 response.
    """
    from .config import DEFAULT_USERNAME, DEFAULT_WINDOW_AFTER_S, DEFAULT_WINDOW_BEFORE_S, DEFAULT_WORKERS

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

    try:
        site = ctx.siteForRequest(body.get("site"))
    except SitesConfigError as e:
        raise ValueError(str(e)) from e
    spec = FetchSpec(
        lokiAddr=site.lokiAddr,
        username=str(body.get("username") or DEFAULT_USERNAME),
        cluster=site.cluster,
        namespace=site.namespace,
        fromIso=_isoForLogcli(fromT),
        toIso=_isoForLogcli(toT),
        workers=int(body.get("workers") or DEFAULT_WORKERS),
    )
    password = body.get("password")
    if password is not None:
        password = str(password)
    return spec, site, expId, tZero, password


def _buildNightSpecFromRequest(ctx: ServerContext, body: dict) -> tuple[FetchSpec, Site, int, str | None]:
    """Translate a JSON night-fetch request body into (FetchSpec, Site, dayObs, password)."""
    from .config import DEFAULT_USERNAME, DEFAULT_WORKERS

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

    try:
        site = ctx.siteForRequest(body.get("site"))
    except SitesConfigError as e:
        raise ValueError(str(e)) from e
    spec = FetchSpec(
        lokiAddr=site.lokiAddr,
        username=str(body.get("username") or DEFAULT_USERNAME),
        cluster=site.cluster,
        namespace=site.namespace,
        fromIso=_isoForLogcli(fromT),
        toIso=_isoForLogcli(toT),
        workers=int(body.get("workers") or DEFAULT_WORKERS),
        podRegex=NIGHT_AOS_POD_REGEX,
    )
    password = body.get("password")
    if password is not None:
        password = str(password)
    return spec, site, dayObs, password


def _buildRangeSpecFromRequest(
    ctx: ServerContext, body: dict
) -> tuple[FetchSpec, Site, int, int, dt.datetime, dt.datetime, str | None]:
    """Translate a JSON range-fetch body into (FetchSpec, Site, startId,
    stopId, tZeroStartUtc, tZeroStopUtc, password).

    The window is one wide span: ``[tZeroStart - windowBefore,
    tZeroStop + windowAfter]`` with no pod filter (all pods, like a single
    exposure). The client resolves the two shutter-close anchors up front
    and passes them as ``tZeroStart`` / ``tZeroStop`` (TAI by default).
    """
    from .config import (
        DEFAULT_USERNAME,
        DEFAULT_WINDOW_AFTER_S,
        DEFAULT_WINDOW_BEFORE_S,
        DEFAULT_WORKERS,
        MAX_RANGE_SPAN,
    )

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

    try:
        site = ctx.siteForRequest(body.get("site"))
    except SitesConfigError as e:
        raise ValueError(str(e)) from e
    spec = FetchSpec(
        lokiAddr=site.lokiAddr,
        username=str(body.get("username") or DEFAULT_USERNAME),
        cluster=site.cluster,
        namespace=site.namespace,
        fromIso=_isoForLogcli(fromT),
        toIso=_isoForLogcli(toT),
        workers=int(body.get("workers") or DEFAULT_WORKERS),
    )
    password = body.get("password")
    if password is not None:
        password = str(password)
    return spec, site, startId, stopId, tZeroStart, tZeroStop, password


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


def _maybeSetLokiPassword(password: str | None) -> None:
    """If the client provided a password, set LOKI_PASSWORD in our process env.

    `fetch._run_logcli` reads LOKI_PASSWORD from `os.environ` when spawning
    logcli. Setting it here is *process-global*; we accept that risk because
    the tool is a single-user local UI. The password is never echoed back
    in any response, logged, or written to disk.
    """
    if not password:
        return
    os.environ["LOKI_PASSWORD"] = password


# ----- public entry point ---------------------------------------------------


def serve(host: str, port: int, ctx: ServerContext) -> None:
    handler = _makeHandler(ctx)
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"Serving on http://{host}:{port} (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        httpd.server_close()
