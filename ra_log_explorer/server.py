"""Stdlib HTTP server: timeline UI + JSON API + on-demand fetches.

The server runs against a long-lived :class:`ServerContext` that holds:

  * the currently-loaded ``ServerState`` (or ``None`` — the "home" mode),
  * a :class:`~ra_log_explorer.jobs.JobManager` for fetch jobs the user
    kicks off from the home page,
  * a thread lock guarding ``state`` so an in-progress fetch can swap it
    in atomically.

HTTP surface (see ``architecture/architecture.md`` for the full schema):

  GET    /                                              timeline.html (home + explore SPA)
  GET    /static/*                                       static assets
  GET    /api/summary                                    current loaded exposure, or {state: null}
  GET    /api/pod/<pod>                                  full parsed log for one pod
  GET    /api/cache                                      list of cached windows on disk
  DELETE /api/cache                                      delete the entire cache
  DELETE /api/cache/<cluster>/<ns>/<slug>                delete one cached window
  GET    /api/exposure-time/<dataId>                     dataId -> shutter-close (TAI) lookup
  POST   /api/fetch                                      start a fetch; returns {jobId}
  GET    /api/fetch/<id>/status                          JSON snapshot of a fetch job
  GET    /api/fetch/<id>/progress                        SSE stream of fetch progress events
"""

from __future__ import annotations

import datetime as dt
import json
import mimetypes
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import exposureTimes, night
from . import parse as parser
from .config import (
    NIGHT_AOS_POD_REGEX,
    FetchSpec,
    cache_root,
    dayObsEndUtc,
    dayObsStartUtc,
)
from .fetch import cacheDuSizeBytes, loadPodLogPath
from .jobs import FetchJob, JobManager

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
    # Lazily populated dataId -> shutter-close UTC datetime, used to
    # turn task event timestamps into Δshutter offsets for histograms.
    shutterCloseByExpId: dict[int, dt.datetime] = field(default_factory=dict)


@dataclass
class ServerContext:
    """Long-lived per-process state shared between the handler threads.

    At most one of ``state`` and ``nightState`` is non-None at a time —
    a fresh fetch in either mode clears the other.
    """

    jobs: JobManager
    state: ServerState | None = None
    nightState: NightState | None = None


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
    window: tuple[dt.datetime, dt.datetime] | None = None
    if targeted:
        window = (
            targeted[0].t - dt.timedelta(seconds=3),
            targeted[-1].t + dt.timedelta(seconds=3),
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
        if window is None:
            # Pod has no explicit target events at all (e.g. head node only
            # references the exposure transitively via expRecord). Keep
            # untagged events so the user can still see warnings.
            relevant.append(ev)
        elif window[0] <= ev.t <= window[1]:
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


def _resolveShutterCloseForDataIds(
    expIds: Iterable[int], state: NightState
) -> tuple[dict[int, dt.datetime], int]:
    """Resolve shutter close (UTC) for each expId via the exposure-time
    cache. Each id either hits the on-disk cache (instant) or — on a
    cache miss — gets a lazy ConsDB lookup if we can read a token.

    Returns ``(resolved, nMissing)`` where ``nMissing`` is the count of
    expIds we couldn't resolve, e.g. because they aren't in ConsDB or
    we can't reach it. Caches everything we resolve back into the
    state so the second /api/summary request is instant.
    """
    out = dict(state.shutterCloseByExpId)
    nMissing = 0
    # Try ConsDB only if we have a readable token; otherwise stick to
    # the cache. Resolving 100+ ids over a flaky link could otherwise
    # block /api/summary for minutes.
    tokenPath = exposureTimes.rspTokenFilePath()
    token: str | None = None
    if tokenPath.exists():
        try:
            token = exposureTimes.readRspToken(tokenPath) or None
        except OSError:
            token = None
    for expId in expIds:
        if expId in out:
            continue
        cached = exposureTimes.lookupCached(expId)
        if cached is None and token is not None:
            try:
                cached = exposureTimes.queryIsot(expId, token)
            except (exposureTimes.ConsDbError, OSError):
                cached = None
            if cached is not None:
                exposureTimes.storeCached(expId, cached)
        if cached is None:
            nMissing += 1
            continue
        # `cached` is TAI ISO without a timezone. Convert to a UTC
        # datetime by attaching UTC then subtracting the TAI→UTC offset,
        # so that (log-utc-time - shutter-close) is a pure Δshutter in
        # the same scale as the rest of the UI.
        try:
            taiAsUtc = parser._parseTimestamp(cached + "Z")
        except ValueError:
            nMissing += 1
            continue
        out[expId] = taiAsUtc - dt.timedelta(seconds=exposureTimes.TAI_MINUS_UTC_S)
    # Cache the resolutions back on the state for cheap re-reads.
    state.shutterCloseByExpId.update(out)
    return out, nMissing


def _buildNightPayload(state: NightState) -> dict:
    """Roll the night up into the per-page payload the JS consumes."""
    stats = night.computeTopStats(state.summaries)
    errType = night.errorsByType(state.summaries)
    errPod = night.errorsByPod(state.summaries)
    firstStarts = night.firstTaskStartByDataId(state.summaries)
    czEnds = night.calcZernikesEndByDataId(state.summaries)

    # Resolve shutter closes for the dataIds we'll need for the
    # histograms and the failed-dataId Δshutter offsets.
    needIds: set[int] = set(firstStarts) | set(czEnds)
    for s in state.summaries:
        for tb in s.tracebacks:
            if tb.expId is not None:
                needIds.add(tb.expId)
    shutterCloseByExpId, nMissingShutter = _resolveShutterCloseForDataIds(needIds, state)

    # Note: shutter close from ConsDB is TAI; the per-pod log
    # timestamps are UTC. We don't subtract the 37s offset here
    # because the user already sees TAI-based labels everywhere
    # else; Δshutter is "log-utc-time minus shutter-close-tai" and is
    # close enough for histogram bucketing (the 37s offset is
    # consistent and will not change the shape).
    firstOffsets, firstNDropped = night.computeDeltaShutterOffsets(firstStarts, shutterCloseByExpId)
    czOffsets, czNDropped = night.computeDeltaShutterOffsets(czEnds, shutterCloseByExpId)
    histFirst = night.buildHistogram(
        "First task pickup (Δshutter)", "s", firstOffsets, nDroppedNoTZero=firstNDropped
    )
    histCz = night.buildHistogram("calcZernikes end (Δshutter)", "s", czOffsets, nDroppedNoTZero=czNDropped)
    failures = night.failureRows(state.summaries, shutterCloseByExpId)

    return {
        "loaded": True,
        "mode": "night",
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


# ----- cache listing --------------------------------------------------------


def _listCacheWindows() -> list[dict]:
    """Inspect the cache root and summarise each completed window.

    Skips directories without `_meta.json` (uninitialised) and those that
    still have a `.partial` flag (a crashed fetch). The result is sorted
    most-recent-fetched-first so the home page's "recent runs" list reads
    chronologically.
    """
    root = cache_root()
    rows: list[dict] = []
    if not root.exists():
        return rows
    for cluster in sorted(p for p in root.iterdir() if p.is_dir()):
        for ns in sorted(p for p in cluster.iterdir() if p.is_dir()):
            for window in sorted(p for p in ns.iterdir() if p.is_dir()):
                metaPath = window / "_meta.json"
                if not metaPath.exists() or (window / ".partial").exists():
                    continue
                try:
                    meta = json.loads(metaPath.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                spec = meta.get("spec") or {}
                rows.append(
                    {
                        "cluster": cluster.name,
                        "namespace": ns.name,
                        "windowDir": window.name,
                        "fromIso": spec.get("fromIso"),
                        "toIso": spec.get("toIso"),
                        "fetchedAt": meta.get("fetched_at"),
                        "podCount": meta.get("pod_count", 0),
                        "totalBytes": meta.get("total_bytes", 0),
                        "sizeOnDisk": cacheDuSizeBytes(window),
                    }
                )
    rows.sort(key=lambda r: r.get("fetchedAt") or "", reverse=True)
    return rows


def _cacheRootInfo() -> dict:
    root = cache_root()
    return {
        "path": str(root),
        "totalBytes": cacheDuSizeBytes(root),
    }


# A cache path component must be a "safe" basename — no path separators,
# no leading dot, no `..` traversal. The same pattern is also used to
# validate pod names elsewhere so the choice is consistent.
_PATH_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _safePathComponent(s: str) -> bool:
    return bool(_PATH_COMPONENT_RE.match(s)) and s not in (".", "..")


def _resolveCacheWindow(cluster: str, namespace: str, slug: str) -> Path | None:
    """Return the cache directory for `(cluster, namespace, slug)` if it exists.

    Validates the components first so an attacker can't escape `cache_root()`.
    Returns ``None`` when any component is unsafe or the directory doesn't exist.
    """
    if not all(_safePathComponent(c) for c in (cluster, namespace, slug)):
        return None
    path = cache_root() / cluster / namespace / slug
    if not path.exists() or not path.is_dir():
        return None
    # Final safety check: the resolved path must still live under cache_root.
    try:
        path.resolve().relative_to(cache_root().resolve())
    except ValueError:
        return None
    return path


def _deleteCacheDir(ctx: "ServerContext", target: Path) -> None:
    """Remove a single cache directory, clearing ServerState if it matched.

    Empty parent directories (the per-cluster and per-namespace ones) are
    also removed when they become empty, so a flush via repeated deletes
    leaves the same clean state as `DELETE /api/cache` followed by ``ls``.
    """
    import shutil

    with ctx.jobs.stateLock:
        if ctx.state is not None and ctx.state.cacheDir.resolve() == target.resolve():
            ctx.state = None
    shutil.rmtree(target)
    # Tidy up empty parents.
    parent = target.parent
    while parent != cache_root() and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


def _deleteCacheRoot(ctx: "ServerContext") -> None:
    """Wipe the entire cache and clear ServerState (which by definition uses it)."""
    import shutil

    with ctx.jobs.stateLock:
        ctx.state = None
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
    """Return a callback that swaps in a new ServerState / NightState
    (depending on ``job.kind``) after a fetch."""

    def cb(job: FetchJob) -> None:
        if job.cacheDir is None:
            return  # fetchAll raised; caller will see an error event
        summaries = parser.summarizeAll(job.cacheDir)
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
            )
            with ctx.jobs.stateLock:
                ctx.state = None
                ctx.nightState = newNight
            return
        assert job.expId is not None and job.tZero is not None
        newState = ServerState(
            cacheDir=job.cacheDir,
            cacheBytes=cacheDuSizeBytes(cache_root()),
            meta=job.meta,
            summaries=summaries,
            expId=job.expId,
            tZero=job.tZero,
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
            ctx.nightState = None
            ctx.state = newState

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
                with ctx.jobs.stateLock:
                    state = ctx.state
                    nightState = ctx.nightState
                if state is not None:
                    self._send_json(_buildSummaryPayload(state))
                    return
                if nightState is not None:
                    self._send_json(_buildNightPayload(nightState))
                    return
                self._send_json({"loaded": False, "cache": _cacheRootInfo()})
                return
            if path.startswith("/api/night/traceback/"):
                with ctx.jobs.stateLock:
                    nightState = ctx.nightState
                if nightState is None:
                    self._send_error_json(404, "No night loaded")
                    return
                from urllib.parse import unquote

                key = unquote(path[len("/api/night/traceback/") :])
                body = night.tracebackBody(nightState.summaries, key)
                if body is None:
                    self._send_error_json(404, f"No traceback with key {key}")
                    return
                self._send_json({"bodyKey": key, "body": body})
                return
            if path.startswith("/api/pod/"):
                with ctx.jobs.stateLock:
                    state = ctx.state
                    nightState = ctx.nightState
                pod = path[len("/api/pod/") :].split("?")[0]
                if not re.match(r"^[A-Za-z0-9._-]+$", pod):
                    self.send_error(400, "Invalid pod name")
                    return
                if state is not None:
                    self._send_json(_podDetail(state, pod))
                    return
                if nightState is not None:
                    self._send_json(_podDetailForNight(nightState, pod))
                    return
                self._send_error_json(404, "Nothing loaded")
                return
            if path == "/api/cache":
                self._send_json({"root": _cacheRootInfo(), "windows": _listCacheWindows()})
                return
            m = re.match(r"^/api/exposure-time/(\d+)$", path)
            if m:
                from urllib.parse import parse_qs

                qs = parse_qs(url.query)
                tokenFileValues = qs.get("tokenFile")
                tokenFileOverride: str | None = tokenFileValues[0] if tokenFileValues else None
                self._handle_exposure_time(int(m.group(1)), tokenFileOverride)
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
                        "expId": job.expId,
                        "tZero": job.tZero.isoformat() if job.tZero else None,
                        "dayObs": job.dayObs,
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

        def _handle_exposure_time(self, dataId: int, tokenFileOverride: str | None) -> None:
            # Cache check first: exposure end-times are immutable once
            # they exist, so a hit lets us skip the token + network call
            # entirely. This also means a user with no RSP token can
            # still resolve any dataId they (or anyone) previously
            # looked up on this machine.
            cached = exposureTimes.lookupCached(dataId)
            if cached is not None:
                self._send_json({"dataId": dataId, "tZero": cached, "scale": "TAI", "fromCache": True})
                return
            path = exposureTimes.rspTokenFilePath(tokenFileOverride)
            if not path.exists():
                self._send_error_json(
                    503,
                    f"RSP token file not found at {path}. "
                    f"Set the path in the home page Credentials card "
                    f"or via the {exposureTimes.RSP_TOKEN_FILE_ENV} env var.",
                )
                return
            try:
                token = exposureTimes.readRspToken(path)
            except OSError as e:
                self._send_error_json(503, f"Could not read RSP token file: {e}")
                return
            if not token:
                self._send_error_json(503, f"RSP token file is empty: {path}")
                return
            try:
                isot = exposureTimes.queryIsot(dataId, token)
            except exposureTimes.ConsDbError as e:
                self._send_error_json(502, f"ConsDB query failed: {e}")
                return
            except OSError as e:
                self._send_error_json(503, f"Could not reach ConsDB: {e}")
                return
            if isot is None:
                self._send_error_json(404, f"No exposure-time record for dataId={dataId}")
                return
            exposureTimes.storeCached(dataId, isot)
            self._send_json({"dataId": dataId, "tZero": isot, "scale": "TAI", "fromCache": False})

        def do_DELETE(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            path = url.path
            if path == "/api/cache":
                _deleteCacheRoot(ctx)
                self._send_json({"root": _cacheRootInfo(), "windows": _listCacheWindows()})
                return
            m = re.match(r"^/api/cache/([^/]+)/([^/]+)/([^/]+)$", path)
            if m:
                cluster, namespace, slug = m.group(1), m.group(2), m.group(3)
                target = _resolveCacheWindow(cluster, namespace, slug)
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
                    spec, expId, tZero, password = _buildSpecFromRequest(body)
                except ValueError as e:
                    self._send_error_json(400, str(e))
                    return
                # The password — if any — is consumed by the fetch worker
                # thread (it sets LOKI_PASSWORD in the subprocess env) and
                # never persisted, returned, or logged.
                _maybeSetLokiPassword(password)
                job = ctx.jobs.createJob(spec, expId, tZero)
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
                    spec, dayObs, password = _buildNightSpecFromRequest(body)
                except ValueError as e:
                    self._send_error_json(400, str(e))
                    return
                _maybeSetLokiPassword(password)
                job = ctx.jobs.createNightJob(spec, dayObs)
                ctx.jobs.startJob(job, onComplete=_onFetchComplete(ctx))
                self._send_json({"jobId": job.jobId}, status=202)
                return
            self.send_error(404)

    return Handler


# ----- request body helpers -------------------------------------------------


def _buildSpecFromRequest(body: dict) -> tuple[FetchSpec, int, dt.datetime, str | None]:
    """Translate a JSON fetch request body into (FetchSpec, expId, tZero, password).

    Raises ``ValueError`` for client-fixable mistakes (missing fields,
    unparseable timestamp); the handler converts those into a 400 response.
    """
    from .config import (
        DEFAULT_CLUSTER,
        DEFAULT_LOKI_ADDR,
        DEFAULT_NAMESPACE,
        DEFAULT_USERNAME,
        DEFAULT_WINDOW_AFTER_S,
        DEFAULT_WINDOW_BEFORE_S,
        DEFAULT_WORKERS,
    )

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
        tZero = tZeroInput - dt.timedelta(seconds=37.0)  # TAI -> UTC

    windowBefore = float(body.get("windowBefore", DEFAULT_WINDOW_BEFORE_S))
    windowAfter = float(body.get("windowAfter", DEFAULT_WINDOW_AFTER_S))
    fromT = tZero - dt.timedelta(seconds=windowBefore)
    toT = tZero + dt.timedelta(seconds=windowAfter)

    spec = FetchSpec(
        lokiAddr=str(body.get("lokiAddr") or DEFAULT_LOKI_ADDR),
        username=str(body.get("username") or DEFAULT_USERNAME),
        cluster=str(body.get("cluster") or DEFAULT_CLUSTER),
        namespace=str(body.get("namespace") or DEFAULT_NAMESPACE),
        fromIso=_isoForLogcli(fromT),
        toIso=_isoForLogcli(toT),
        workers=int(body.get("workers") or DEFAULT_WORKERS),
    )
    password = body.get("password")
    if password is not None:
        password = str(password)
    return spec, expId, tZero, password


def _buildNightSpecFromRequest(body: dict) -> tuple[FetchSpec, int, str | None]:
    """Translate a JSON night-fetch request body into (FetchSpec, dayObs, password)."""
    from .config import (
        DEFAULT_CLUSTER,
        DEFAULT_LOKI_ADDR,
        DEFAULT_NAMESPACE,
        DEFAULT_USERNAME,
        DEFAULT_WORKERS,
    )

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

    spec = FetchSpec(
        lokiAddr=str(body.get("lokiAddr") or DEFAULT_LOKI_ADDR),
        username=str(body.get("username") or DEFAULT_USERNAME),
        cluster=str(body.get("cluster") or DEFAULT_CLUSTER),
        namespace=str(body.get("namespace") or DEFAULT_NAMESPACE),
        fromIso=_isoForLogcli(fromT),
        toIso=_isoForLogcli(toT),
        workers=int(body.get("workers") or DEFAULT_WORKERS),
        podRegex=NIGHT_AOS_POD_REGEX,
    )
    password = body.get("password")
    if password is not None:
        password = str(password)
    return spec, dayObs, password


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
    import os

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
