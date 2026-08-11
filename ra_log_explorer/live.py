"""Live night poller: keep the current night's logs hot on disk.

Deployed next to the data, the explorer no longer needs to treat Loki as
a distant, expensive resource. A single daemon thread polls on a
configurable interval and maintains one all-pods window directory for
the in-progress night (the ordinary cache-window path for
``[noon UTC, noon UTC + 24h)``), appending each tick's new lines to the
per-pod JSONL files. The ``_live.json`` sidecar (schema documented in
``fetch.py``) records how far the night has been durably fetched, and
``fetch.fetchAll`` serves any request window at or before that watermark
by *slicing* the night dir — so by the time an exposure is viewable at
all, viewing it costs no Loki round trip.

Correctness leans on three properties:

* **Tiling.** Every pod's increments are half-open ``[watermark, target)``
  windows fetched with the same lossless count-presized single-batch
  chunking as a batch fetch, so consecutive ticks tile the night exactly:
  no line duplicated, none dropped.
* **Idempotent appends.** Each increment lands in a temp file first and
  is appended to the pod's file only when its fetch completed; per-pod
  watermarks advance only after the append. A failed pod simply retries
  its whole missed span next tick. On startup, files are truncated back
  to the sidecar's recorded byte counts, discarding anything a crash
  half-appended.
* **Ingestion lag.** Each tick's target is ``now - lag`` rather than
  ``now``: Loki ingestion is not instantaneous, and a line stamped just
  before an increment was fetched but ingested just after would fall
  into an already-covered window and be lost forever. The lag keeps the
  fetch frontier safely behind the ingestion frontier, and the
  end-of-night verification pass (count_over_time per pod vs. lines
  appended) catches anything that slipped through anyway.

Alongside the log loop, each tick queries ConsDB for every exposure of
the current dayObs (near-free in-cluster) and computes which of them are
*ready*: shutter close plus the standard post-exposure window is at or
before the watermark, i.e. the whole default exposure view can be served
from disk. The snapshot of all of this — watermark, exposure list,
readiness, error states — is what ``GET /api/live`` returns. Exposures
are listed per *instrument*, because an exposure id is only unique
within one (see :mod:`.exposureTimes`) and LSSTCam and LATISS share ids
on any night they both observe.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import exposureTimes
from .config import (
    DEFAULT_WINDOW_AFTER_S,
    FetchSpec,
    cache_root,
    currentDayObs,
    dayObsEndUtc,
    dayObsStartUtc,
    windowCachePath,
)
from .fetch import (
    LIVE_SIDECAR_NAME,
    LIVE_SIDECAR_VERSION,
    META_NAME,
    PODS_DIR_NAME,
    PODS_EVENTS_DIR_NAME,
    PODS_LIST_NAME,
    FetchError,
    _fmtLogcliTime,
    _parseIso,
    countPodWindow,
    fetchEventsWindowInto,
    fetchPodWindowInto,
    listPods,
    liveNightMeta,
    markCacheViewed,
    readLiveSidecar,
    writeLiveSidecar,
)
from .sites import Site

# The end-of-night verification refetches a pod only when its appended
# line count falls short of the count_over_time oracle by more than
# max(VERIFY_TOLERANCE_LINES, expected / VERIFY_TOLERANCE_DIVISOR).
# The oracle is not exact against a *deduplicated* fetch: duplicate
# entries in overlapping storage chunks are deduplicated by the
# log-query path but counted blindly by metric queries — measured at
# ~0.03% (tens of lines per pod per night) on real data, stable across
# refetches. A strict != would therefore refetch nearly every pod every
# night. Real losses are bursts (a killed fetch, a flush ingested past
# the lag), which clear this bar by orders of magnitude.
VERIFY_TOLERANCE_LINES = 100
VERIFY_TOLERANCE_DIVISOR = 500  # i.e. 0.2% of the expected count

# Temp files the poller stages through, all created inside the night dir
# (see _stageWindow). Cleaned up on recovery, since a crash strands them.
_TEMP_PREFIXES = ("live-inc-", "live-events-", "live-refetch-", "_live-")


@dataclass
class _Night:
    """The night dir the poller is appending to, and its sidecar.

    Passed explicitly rather than held only on the manager so the
    end-of-night work can also run against a night the poller isn't
    currently tracking — the one a restart across noon left unfinalised.
    """

    dayObs: int
    dir: Path
    sidecar: dict[str, Any]


def _emptyPodRecord(watermarkIso: str) -> dict[str, Any]:
    """A fresh per-pod sidecar record, anchored at ``watermarkIso``."""
    return {"watermarkIso": watermarkIso, "bytes": 0, "lines": 0, "eventBytes": 0, "eventLines": 0}


def _emptyEventRecord() -> dict[str, Any]:
    """A fresh record for a name seen only in the k8s/events stream.

    Deliberately carries no ``watermarkIso``: these names make no claim
    about app-log coverage, and letting one hold the global watermark
    back would strand the whole night at its start.
    """
    return {"eventBytes": 0, "eventLines": 0}


def _appendFile(src: Path, dst: Path) -> None:
    """Append ``src``'s bytes to ``dst``, all-or-nothing.

    A partial append must not survive. The caller leaves the pod's
    watermark alone when this raises, so the same span is fetched again
    next tick and appended *after* whatever landed — and the sidecar's
    byte count, advanced only on success, would then be extended over
    the stranded bytes on that later tick. The result is a durable range
    the sidecar vouches for that contains duplicated and torn lines, in
    non-ascending time order, which the slicer's bisect quietly
    mis-answers. Finalisation can't catch it either: the line counter is
    advanced only on success too, so the count still matches the oracle.

    Truncating back to the pre-append size makes the span cleanly
    retryable, exactly as :meth:`LiveNightManager._appendEventLines`
    does for the events stream.
    """
    before = dst.stat().st_size if dst.exists() else 0
    try:
        with open(src, "rb") as fromFh, open(dst, "ab") as toFh:
            shutil.copyfileobj(fromFh, toFh)
    except BaseException:
        try:
            if before:
                with open(dst, "ab") as fh:
                    fh.truncate(before)
            else:
                dst.unlink(missing_ok=True)
        except OSError:
            # Nothing better to do than let the original error out; the
            # recorded byte count still points at the last good line, so
            # a restart's recovery truncation reaches the same place.
            pass
        raise


class LiveNightManager:
    """Owns the live night dir and the polling thread that feeds it.

    One instance per process, created at startup when live mode is
    enabled. All mutation happens on the poller thread; the only
    cross-thread surface is :meth:`snapshot`, which returns the
    immutable dict published at the end of the last tick.
    """

    def __init__(
        self,
        site: Site,
        username: str,
        workers: int,
        pollS: float,
        lagS: float,
        windowAfterS: float = DEFAULT_WINDOW_AFTER_S,
        fixedDayObs: int | None = None,
    ) -> None:
        self._site = site
        self._username = username
        self._workers = max(1, workers)
        self._pollS = max(30.0, pollS)
        self._lagS = max(0.0, lagS)
        self._windowAfterS = windowAfterS
        # Testing hook (--live-day-obs): pin the poller to one dayObs
        # instead of tracking the clock. A staged historical night then
        # plays the role of "tonight" — same code paths, real data —
        # and the noon-UTC rollover never fires.
        self._fixedDayObs = fixedDayObs
        self._lock = threading.Lock()
        self._stopEvent = threading.Event()
        self._thread: threading.Thread | None = None
        # Poller-thread-only state.
        self._night: _Night | None = None
        self._orphanDirs: list[Path] = []
        self._orphanError: str | None = None
        self._exposures: list[exposureTimes.ExposureRecord] = []
        self._storedExposureKeys: set[tuple[str, int]] = set()
        self._consdbError: str | None = None
        self._lastTick: dict[str, Any] | None = None
        self._lastError: str | None = None
        self._snapshot: dict[str, Any] = {"enabled": True, "siteName": site.name, "dayObs": None}

    # ----- public surface ---------------------------------------------------

    def start(self) -> None:
        """Start the poller thread (idempotent)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._runLoop, name="live-night-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ask the poller to exit; returns without waiting for it."""
        self._stopEvent.set()

    def snapshot(self) -> dict[str, Any]:
        """The JSON-ready state published at the end of the last tick.

        The returned dict is never mutated after publication — callers
        may serialize it without copying.
        """
        with self._lock:
            return self._snapshot

    # ----- poll loop --------------------------------------------------------

    def _runLoop(self) -> None:
        while not self._stopEvent.is_set():
            started = time.time()
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — the loop must survive anything
                self._lastError = traceback.format_exc(limit=6)
                self._publishSnapshot()
            elapsed = time.time() - started
            self._stopEvent.wait(max(1.0, self._pollS - elapsed))

    def tick(self, now: dt.datetime | None = None) -> None:
        """One full poll cycle. Public so tests can drive it directly."""
        now = now or dt.datetime.now(dt.timezone.utc)
        dayObs = self._fixedDayObs if self._fixedDayObs is not None else currentDayObs(now)
        night = self._night
        if night is not None and not _nightDirIntact(night):
            # The cache was wiped underneath us — DELETE /api/cache from
            # the home page, an LRU pass, or a hand-run rm. Without this
            # the poller would keep appending to a directory that no
            # longer exists and fail every tick until the next rollover.
            night = self._night = None
        if night is not None and night.dayObs != dayObs:
            try:
                self._finaliseNight(night)
            except Exception as e:  # noqa: BLE001 — yesterday must not block tonight
                # Finalisation does a real top-up, so Loki being unwell can
                # fail it. Opening the new night is the urgent half; the
                # orphan sweep below rediscovers this one and retries.
                self._orphanError = f"could not finalise {night.dir.name}: {e}"
            night = self._night = None
        if night is None:
            night = self._night = self._openNight(dayObs, now)
            # Publish before the first increment: the initial catch-up
            # can run for minutes, and until it finishes the UI should
            # show "catching up from <watermark>" rather than nothing.
            self._publishSnapshot()
        tickStats: dict[str, Any] = {"startedAt": now.isoformat()}
        started = time.time()
        if not night.sidecar.get("finalised"):
            target = min(now - dt.timedelta(seconds=self._lagS), dayObsEndUtc(dayObs))
            newLines, activePods = self._fetchIncrement(night, target)
            tickStats.update({"newLines": newLines, "activePods": activePods})
        self._consdbTick(dayObs)
        # At most one per tick: an orphan's top-up can be a whole night's
        # worth of fetching, and the current night comes first.
        self._finaliseOneOrphan()
        tickStats["elapsedS"] = round(time.time() - started, 3)
        self._lastTick = tickStats
        self._lastError = None
        self._publishSnapshot()

    # ----- night lifecycle --------------------------------------------------

    def _nightSpec(self, dayObs: int) -> FetchSpec:
        return FetchSpec(
            lokiAddr=self._site.lokiAddr,
            username=self._username,
            cluster=self._site.cluster,
            namespace=self._site.namespace,
            fromIso=_fmtLogcliTime(dayObsStartUtc(dayObs)),
            toIso=_fmtLogcliTime(dayObsEndUtc(dayObs)),
            workers=self._workers,
        )

    def _openNight(self, dayObs: int, now: dt.datetime) -> _Night:
        """Create or recover the night dir for ``dayObs`` and adopt it."""
        spec = self._nightSpec(dayObs)
        nightDir = windowCachePath(spec.cluster, spec.namespace, spec.fromIso, spec.toIso)
        nightDir.mkdir(parents=True, exist_ok=True)
        (nightDir / PODS_DIR_NAME).mkdir(exist_ok=True)
        (nightDir / PODS_EVENTS_DIR_NAME).mkdir(exist_ok=True)
        sidecar = readLiveSidecar(nightDir)
        if sidecar is None:
            # Fresh night — or a dir left by something other than the
            # poller (an aborted batch fetch, an older schema). The
            # poller owns the current night's path: start from scratch
            # so the watermark's guarantee is grounded in bytes we wrote.
            for sub in (PODS_DIR_NAME, PODS_EVENTS_DIR_NAME):
                shutil.rmtree(nightDir / sub, ignore_errors=True)
                (nightDir / sub).mkdir(exist_ok=True)
            (nightDir / META_NAME).unlink(missing_ok=True)
            sidecar = {
                "version": LIVE_SIDECAR_VERSION,
                "dayObs": dayObs,
                "fromIso": spec.fromIso,
                "toIso": spec.toIso,
                "watermarkIso": spec.fromIso,
                "eventsWatermarkIso": spec.fromIso,
                "finalised": False,
                "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
                "pods": {},
                "eventPods": {},
                "errors": {},
                "incomplete_pods": {},
            }
            writeLiveSidecar(nightDir, sidecar)
        else:
            _recoverNight(nightDir, sidecar)
        night = _Night(dayObs=dayObs, dir=nightDir, sidecar=sidecar)
        self._orphanDirs = self._findOrphanNights(nightDir, now)
        self._exposures = []
        self._storedExposureKeys = set()
        self._consdbError = None
        return night

    def _findOrphanNights(self, currentDir: Path, now: dt.datetime) -> list[Path]:
        """Night dirs this site left unfinalised — usually a restart across noon.

        Nothing else ever revisits a past dayObs, so without this sweep
        such a directory is stranded forever: no ``_meta.json``, which
        means the cache listing skips it, LRU eviction can't reclaim it,
        and yet ``du`` still counts its ~9 GiB against the size cap.

        Only nights whose window has actually *ended* qualify. With
        ``--live-day-obs`` pinning the poller to a historical night, the
        real current night's dir is "not the one we're tracking" without
        being finished, and topping it up to an end-time in the future
        would mark a night complete that isn't.
        """
        base = cache_root() / self._site.cluster / self._site.namespace
        out: list[Path] = []
        if not base.exists():
            return out
        for window in sorted(base.iterdir()):
            if not window.is_dir() or window == currentDir:
                continue
            sidecar = readLiveSidecar(window)
            if sidecar is None or sidecar.get("finalised"):
                continue
            dayObs = sidecar.get("dayObs")
            if isinstance(dayObs, int) and dayObsEndUtc(dayObs) <= now:
                out.append(window)
        return out

    def _finaliseOneOrphan(self) -> None:
        """Finalise one night the poller didn't see through its rollover.

        One per tick, and never allowed to break the tick: the current
        night is what people are watching. A failure goes to the back of
        the queue rather than being dropped (a night nobody finalises is
        the whole problem) or retried immediately; with a single stuck
        orphan that costs one aborted listing per tick, and it is visible
        as ``orphanError`` in ``/api/live``.
        """
        while self._orphanDirs:
            window = self._orphanDirs.pop(0)
            sidecar = readLiveSidecar(window)
            if sidecar is None or sidecar.get("finalised"):
                continue
            dayObs = sidecar.get("dayObs")
            if not isinstance(dayObs, int):
                continue
            try:
                self._finaliseNight(_Night(dayObs=dayObs, dir=window, sidecar=sidecar))
                self._orphanError = None
            except Exception as e:  # noqa: BLE001 — never break the tick over an old night
                self._orphanError = f"could not finalise {window.name}: {e}"
                self._orphanDirs.append(window)
            return

    def _finaliseNight(self, night: _Night) -> None:
        """Top up to night end, verify per-pod totals, write ``_meta.json``.

        After this the night dir is a complete, trustworthy all-pods
        window: the sidecar stays (marked finalised) so sub-window
        requests keep being served by slicing, and the ordinary cache
        machinery handles listing, LRU eviction, and deletion.
        """
        sidecar = night.sidecar
        if sidecar.get("finalised"):
            return
        spec = self._nightSpec(night.dayObs)
        nightStart = dayObsStartUtc(night.dayObs)
        nightEnd = dayObsEndUtc(night.dayObs)
        self._fetchIncrement(night, nightEnd)
        pods: dict[str, dict[str, Any]] = sidecar["pods"]
        errors: dict[str, str] = sidecar["errors"]
        incomplete: dict[str, str] = sidecar["incomplete_pods"]
        # Verification: the oracle never under-counts a completed window,
        # so a shortfall beyond its dedup slack (see the tolerance
        # constants above) means something slipped through the tick
        # seams — most plausibly ingestion lag beyond LIVE_LAG_S. Those
        # pods are re-fetched whole: the night is about to become a
        # permanent cache, so this is the last chance to be right.
        # The count queries are independent and each is a Loki round
        # trip, so they go out in parallel — the same width the tick's
        # own fetching uses. Sequentially this is ~600 round trips for a
        # busy night, minutes of wall clock, and it is not only paid at
        # the noon rollover: the orphan sweep re-runs finalisation for a
        # night it could not finish, once per tick, and a pass that
        # outlasts the poll interval would hold the *current* night's
        # watermark back while it ran.
        podExpected: dict[str, int] = {}
        with ThreadPoolExecutor(max_workers=self._workers) as ex:
            counted = dict(
                zip(
                    sorted(pods),
                    ex.map(
                        lambda pod: countPodWindow(spec, pod, nightStart, nightEnd),
                        sorted(pods),
                    ),
                )
            )
        # The refetches stay sequential and in pod order: each one rewrites
        # a file the slicer may be reading, and they are rare.
        for pod in sorted(pods):
            expected = counted.get(pod)
            if expected is None:
                continue
            podExpected[pod] = expected
            shortfall = expected - int(pods[pod].get("lines") or 0)
            if shortfall <= max(VERIFY_TOLERANCE_LINES, expected // VERIFY_TOLERANCE_DIVISOR):
                continue
            # Clear the old flags *before* the refetch, never after: the
            # refetch sets its own if it comes back unreconcilable, and
            # popping afterwards would erase exactly that — publishing a
            # night as complete while a pod is known to be short.
            errors.pop(pod, None)
            incomplete.pop(pod, None)
            try:
                self._refetchWholePod(night, spec, pod, nightStart, nightEnd)
            except FetchError as e:
                errors[pod] = f"end-of-night refetch failed: {e}"
        (night.dir / PODS_LIST_NAME).write_text("\n".join(sorted(pods)) + "\n")
        meta = liveNightMeta(sidecar, spec, podExpected=podExpected)
        (night.dir / META_NAME).write_text(json.dumps(meta, indent=2))
        sidecar["finalised"] = True
        sidecar["watermarkIso"] = spec.toIso
        sidecar["updatedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
        writeLiveSidecar(night.dir, sidecar)
        markCacheViewed(night.dir)

    def _refetchWholePod(
        self, night: _Night, spec: FetchSpec, pod: str, fromT: dt.datetime, toT: dt.datetime
    ) -> None:
        """Replace one pod's file with a fresh full-window fetch.

        Slicing is suspended for the duration. Zeroing the record alone
        is not enough: a slicer that reads a zeroed record doesn't wait,
        it *skips the pod* — and since finalisation has already cleared
        that pod's error flags, the resulting slice is written with
        ``fetchComplete: true``, misses a pod entirely, and then serves
        every later identical request as an exact hit. The refetch takes
        minutes over a whole night, and it runs exactly when people are
        opening the night that just ended, so this is a wide window to
        leave open.
        """
        sidecar = night.sidecar
        podFile = night.dir / PODS_DIR_NAME / f"{pod}.jsonl"
        record = sidecar["pods"].setdefault(pod, _emptyPodRecord(spec.toIso))
        # Publish "nothing durable here" before swapping the file out.
        # A request thread slicing this pod could otherwise pair the old
        # byte count with the new inode and copy a prefix of the refetch.
        record["bytes"] = 0
        record["lines"] = 0
        sidecar["rewritingPod"] = pod
        writeLiveSidecar(night.dir, sidecar)
        try:
            fd, tmpName = tempfile.mkstemp(prefix="live-refetch-", suffix=".jsonl", dir=night.dir)
            tmpPath = Path(tmpName)
            try:
                with os.fdopen(fd, "wb") as fh:
                    lines, complete, reason = fetchPodWindowInto(spec, pod, fromT, toT, fh)
                os.replace(tmpPath, podFile)
            finally:
                tmpPath.unlink(missing_ok=True)
            record["bytes"] = podFile.stat().st_size
            record["lines"] = lines
            record["watermarkIso"] = spec.toIso
            if not complete:
                sidecar["incomplete_pods"][pod] = reason
        finally:
            # Cleared even when the refetch failed: the pod's record then
            # honestly reads zero bytes, and the caller records the
            # error, so slicing may resume against a night that is
            # visibly short rather than silently so.
            sidecar.pop("rewritingPod", None)
            writeLiveSidecar(night.dir, sidecar)

    # ----- increment fetching -----------------------------------------------

    def _fetchIncrement(self, night: _Night, target: dt.datetime) -> tuple[int, int]:
        """Advance every pod's coverage to ``target``. Returns (lines, pods).

        The listing window is anchored at the *global* watermark (the
        minimum over per-pod watermarks) so a pod that failed last tick
        is still discovered even though its healthy peers have moved on;
        each pod then fetches from its own watermark, so nothing is
        double-fetched.
        """
        sidecar = night.sidecar
        spec = self._nightSpec(night.dayObs)
        nightStart = dayObsStartUtc(night.dayObs)
        globalW = _parseIso(sidecar["watermarkIso"])
        if target <= globalW:
            return 0, 0
        targetIso = _fmtLogcliTime(target)
        listSpec = replace(spec, fromIso=_fmtLogcliTime(globalW), toIso=targetIso)
        active = set(listPods(listSpec))  # a listing failure aborts the tick; retried next poll
        pods: dict[str, dict[str, Any]] = sidecar["pods"]
        newLines = 0
        podsDir = night.dir / PODS_DIR_NAME
        with ThreadPoolExecutor(max_workers=self._workers) as ex:
            futures = {}
            for pod in sorted(active):
                fromT = _parseIso(pods[pod]["watermarkIso"]) if pod in pods else nightStart
                if fromT >= target:
                    continue
                fut = ex.submit(self._stageWindow, night, spec, pod, fromT, target, podsDir / f"{pod}.jsonl")
                futures[fut] = (pod, fromT)
            for fut in as_completed(futures):
                pod, fromT = futures[fut]
                try:
                    nbytes, lines, complete, reason = fut.result()
                except Exception as e:  # noqa: BLE001 — contain per-pod failures
                    # Watermark deliberately not advanced: the whole
                    # missed span is retried next tick. A pod failing on
                    # its *first* fetch needs a record anchored at the
                    # attempted start, or nothing would hold the global
                    # watermark back over its missed span.
                    sidecar["errors"][pod] = str(e)
                    _adoptPodRecord(sidecar, pod, _fmtLogcliTime(fromT))
                    continue
                record = _adoptPodRecord(sidecar, pod, sidecar["fromIso"])
                record["bytes"] += nbytes
                record["lines"] += lines
                record["watermarkIso"] = targetIso
                newLines += lines
                sidecar["errors"].pop(pod, None)
                if not complete:
                    sidecar["incomplete_pods"][pod] = reason
        # Pods absent from the listing emitted nothing in the listed
        # window, so their coverage advances for free — without this, one
        # short-lived pod would pin the global watermark at its death.
        # That holds for previously-errored pods too: the listing window
        # starts at the global watermark, which their stale per-pod
        # watermark is what's holding down, so it covers their whole
        # missed span — absence proves the span empty and the error moot.
        for pod, record in pods.items():
            if pod not in active and _parseIso(record["watermarkIso"]) < target:
                record["watermarkIso"] = targetIso
                sidecar["errors"].pop(pod, None)
        self._appendEventsIncrement(night, spec, target)
        # Only app-log pods have a watermark to contribute; names seen
        # solely in the k8s/events stream live in ``eventPods`` and make
        # no coverage claim (see _emptyEventRecord).
        watermarks = [_parseIso(r["watermarkIso"]) for r in pods.values()]
        sidecar["watermarkIso"] = _fmtLogcliTime(min([target, *watermarks]))
        sidecar["updatedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
        (night.dir / PODS_LIST_NAME).write_text("\n".join(sorted(pods)) + "\n")
        writeLiveSidecar(night.dir, sidecar)
        markCacheViewed(night.dir)
        return newLines, len(active)

    def _stageWindow(
        self,
        night: _Night,
        spec: FetchSpec,
        pod: str,
        fromT: dt.datetime,
        toT: dt.datetime,
        podFile: Path,
    ) -> tuple[int, int, bool, str]:
        """Fetch one pod's increment via a temp file, then append it.

        The temp-file staging is what makes a failed increment safely
        retryable: nothing reaches the pod's real file unless the whole
        window fetched, so the caller can leave the watermark alone and
        try the same span again next tick.

        The temp lives in the night dir — i.e. on the cache volume —
        rather than the system temp dir, which under the deployment's
        read-only root filesystem is a memory-backed tmpfs. A first tick
        after a restart stages a pod's whole missed span, which for a
        busy pod mid-night is hundreds of MB, times ``workers`` at once.
        """
        fd, tmpName = tempfile.mkstemp(prefix="live-inc-", suffix=".jsonl", dir=night.dir)
        tmpPath = Path(tmpName)
        try:
            with os.fdopen(fd, "wb") as fh:
                lines, complete, reason = fetchPodWindowInto(spec, pod, fromT, toT, fh)
            nbytes = tmpPath.stat().st_size
            if nbytes:
                _appendFile(tmpPath, podFile)
            return nbytes, lines, complete, reason
        finally:
            tmpPath.unlink(missing_ok=True)

    def _appendEventsIncrement(self, night: _Night, spec: FetchSpec, target: dt.datetime) -> None:
        """Fetch the namespace's k8s/events increment and demux per name.

        One chunked query per tick for the whole namespace (the stream
        is low-volume), split by the ``name`` label into the same
        per-pod files a batch fetch writes. The query is deliberately
        *not* narrowed to pods: the stream also carries ReplicaSet, Job
        and Deployment events, and keeping them costs almost nothing
        while a too-narrow filter would silently drop lifecycle context
        we might later want. Nothing downstream is confused by them —
        ``parse.classifyK8sEvent`` ignores any event whose involved
        object isn't a Pod, and ``summarizeAll`` enumerates pods from
        ``pods/``, so a name with no app logs is never even opened.

        Best-effort like the batch path: a failure leaves the events
        watermark alone (so the span is retried next tick) and never
        blocks the app-log watermark.
        """
        sidecar = night.sidecar
        eventsW = _parseIso(sidecar.get("eventsWatermarkIso") or sidecar["fromIso"])
        if target <= eventsW:
            return
        fd, tmpName = tempfile.mkstemp(prefix="live-events-", suffix=".jsonl", dir=night.dir)
        tmpPath = Path(tmpName)
        try:
            try:
                with os.fdopen(fd, "wb") as fh:
                    fetchEventsWindowInto(spec, eventsW, target, fh)
            except Exception as e:  # noqa: BLE001 — auxiliary stream; never fail the tick
                sidecar["eventsError"] = str(e)
                return
            perName: dict[str, list[bytes]] = {}
            unfiled = 0
            with open(tmpPath, "rb") as fh:
                for raw in fh:
                    try:
                        name = json.loads(raw).get("labels", {}).get("name")
                    except json.JSONDecodeError:
                        unfiled += 1
                        continue
                    if name:
                        perName.setdefault(name, []).append(raw)
                    else:
                        unfiled += 1
            # A line with no ``name`` label can't be filed under a pod, so
            # it is dropped — but the watermark still advances past it, and
            # a dropped Pod event is a missing POD_* marker on somebody's
            # timeline. Whether live Loki ever emits one is not something
            # this repo can answer: the captured corpus is already demuxed,
            # so every line in it lost the label on the way in. Counting
            # them cumulatively makes the question answerable from the
            # deployment's own /api/live instead of by assumption.
            if unfiled:
                sidecar["eventsUnfiled"] = int(sidecar.get("eventsUnfiled") or 0) + unfiled
            try:
                self._appendEventLines(night, perName)
            except OSError as e:
                sidecar["eventsError"] = f"events append failed, span retried next tick: {e}"
                return
            sidecar["eventsWatermarkIso"] = _fmtLogcliTime(target)
            sidecar.pop("eventsError", None)
        finally:
            tmpPath.unlink(missing_ok=True)

    def _appendEventLines(self, night: _Night, perName: dict[str, list[bytes]]) -> None:
        """Append the demuxed event lines, all-or-nothing.

        The events watermark covers the whole namespace in one span, so
        unlike the per-pod app-log path there is no per-name watermark to
        leave behind on a partial failure. Rolling the appends back keeps
        the span cleanly retryable instead of duplicating every line that
        did land — which would show up as duplicate POD_* markers on the
        timeline for the rest of the night.
        """
        eventsDir = night.dir / PODS_EVENTS_DIR_NAME
        undo: list[tuple[Path, int, dict[str, Any], int, int]] = []
        try:
            for name, rawLines in sorted(perName.items()):
                path = eventsDir / f"{name}.jsonl"
                record = _eventRecordFor(night.sidecar, name)
                undo.append(
                    (
                        path,
                        path.stat().st_size if path.exists() else 0,
                        record,
                        int(record.get("eventBytes") or 0),
                        int(record.get("eventLines") or 0),
                    )
                )
                with open(path, "ab") as out:
                    for raw in rawLines:
                        out.write(raw)
                record["eventBytes"] += sum(len(raw) for raw in rawLines)
                record["eventLines"] += len(rawLines)
        except OSError:
            for path, size, record, eventBytes, eventLines in undo:
                if size:
                    with open(path, "ab") as fh:
                        fh.truncate(size)
                else:
                    path.unlink(missing_ok=True)
                record["eventBytes"] = eventBytes
                record["eventLines"] = eventLines
            raise

    # ----- ConsDB + readiness -----------------------------------------------

    def _consdbTick(self, dayObs: int) -> None:
        """Refresh tonight's exposure list from ConsDB (best-effort)."""
        try:
            token = exposureTimes.loadTokenForSite(self._site)
        except OSError as e:
            self._consdbError = f"token unreadable: {e}"
            return
        try:
            records = exposureTimes.queryExposureRecordsForDayObs(
                dayObs, token, consdbUrl=self._site.consdbUrl
            )
        except exposureTimes.ConsDbError as e:
            self._consdbError = str(e)
            return
        except OSError as e:  # DNS failure, connection refused, ...
            self._consdbError = str(e)
            return
        self._consdbError = None
        self._exposures = records
        # Persist only when something new turned up — records are
        # immutable, and rewriting the whole per-site cache file every
        # tick for no change is pointless churn on the cache volume. The
        # *whole* list goes in, not just the new records: the bare-id
        # keys hold the probe-order winner across every instrument, which
        # can't be decided from a subset.
        keys = {k for k in (_exposureKey(r) for r in records) if k is not None}
        if keys - self._storedExposureKeys:
            exposureTimes.storeCachedRecordList(records, siteName=self._site.name)
            self._storedExposureKeys = keys

    def _exposureRows(self, watermark: dt.datetime | None) -> list[dict[str, Any]]:
        """Tonight's exposures, newest first, with readiness computed.

        One row per (instrument, exposure id) — ids collide across
        instruments on any night both observe, so collapsing to the id
        alone would hide one instrument's exposures entirely.
        """
        rows: list[dict[str, Any]] = []
        ordered = sorted(
            self._exposures,
            key=lambda r: (
                -(exposureTimes.recordExposureId(r) or 0),
                exposureTimes.recordInstrument(r) or "",
            ),
        )
        for record in ordered:
            dataId = exposureTimes.recordExposureId(record)
            if dataId is None:
                continue
            obsEndTai = exposureTimes.obsEnd(record)
            obsEndUtc: dt.datetime | None = None
            if obsEndTai is not None:
                try:
                    obsEndUtc = _taiIsoToUtc(obsEndTai)
                except ValueError:
                    obsEndUtc = None
            readyAt = obsEndUtc + dt.timedelta(seconds=self._windowAfterS) if obsEndUtc is not None else None
            rows.append(
                {
                    "dataId": dataId,
                    "instrument": exposureTimes.recordInstrument(record),
                    "obsEndUtc": obsEndUtc.isoformat() if obsEndUtc else None,
                    "readyAtUtc": readyAt.isoformat() if readyAt else None,
                    "ready": bool(readyAt is not None and watermark is not None and readyAt <= watermark),
                    "record": record,
                }
            )
        return rows

    # ----- snapshot ---------------------------------------------------------

    def _publishSnapshot(self) -> None:
        night = self._night
        sidecar = night.sidecar if night is not None else {}
        watermarkIso = sidecar.get("watermarkIso")
        watermark = _parseIso(watermarkIso) if watermarkIso else None
        pods: dict[str, dict[str, Any]] = sidecar.get("pods") or {}
        # "Catching up" = the watermark is further behind now than two
        # healthy poll cycles could explain — the UI shows a distinct
        # state so a fresh deployment's first big backfill reads as
        # progress, not breakage.
        catchingUp = False
        if watermark is not None and not sidecar.get("finalised"):
            behindS = (dt.datetime.now(dt.timezone.utc) - watermark).total_seconds()
            catchingUp = behindS > 2 * self._pollS + self._lagS
        snapshot = {
            "enabled": True,
            "siteName": self._site.name,
            "dayObs": night.dayObs if night is not None else None,
            "nightStart": sidecar.get("fromIso"),
            "nightEnd": sidecar.get("toIso"),
            "watermark": watermarkIso,
            "updatedAt": sidecar.get("updatedAt"),
            "finalised": bool(sidecar.get("finalised")),
            "catchingUp": catchingUp,
            "pollSeconds": self._pollS,
            "lagSeconds": self._lagS,
            "windowAfterSeconds": self._windowAfterS,
            "nPods": len(pods),
            "totalBytes": sum(int(r.get("bytes") or 0) for r in pods.values()),
            "totalLines": sum(int(r.get("lines") or 0) for r in pods.values()),
            "errors": dict(sidecar.get("errors") or {}),
            "incompletePods": dict(sidecar.get("incomplete_pods") or {}),
            "eventsError": sidecar.get("eventsError"),
            # Cumulative k8s/events lines that carried no ``name`` label
            # and so could not be filed under a pod. Expected to stay 0;
            # a non-zero value means POD_* markers are going missing.
            "eventsUnfiled": int(sidecar.get("eventsUnfiled") or 0),
            "consdbError": self._consdbError,
            "orphanError": self._orphanError,
            "lastTick": self._lastTick,
            "lastError": self._lastError,
            "exposures": self._exposureRows(watermark),
        }
        with self._lock:
            self._snapshot = snapshot


def _nightDirIntact(night: _Night) -> bool:
    """True while the night dir the poller adopted still exists on disk."""
    return night.dir.is_dir() and (night.dir / LIVE_SIDECAR_NAME).exists()


def _adoptPodRecord(sidecar: dict[str, Any], pod: str, watermarkIso: str) -> dict[str, Any]:
    """Get or create ``pods[pod]``, absorbing any event-only record.

    A name first seen in the k8s/events stream lives in ``eventPods``
    until it emits app logs; when it does, its event counters move across
    so the pod has one record again and nothing is double-counted.
    """
    record = sidecar["pods"].get(pod)
    if record is None:
        record = _emptyPodRecord(watermarkIso)
        carried = (sidecar.get("eventPods") or {}).pop(pod, None)
        if carried:
            record["eventBytes"] = int(carried.get("eventBytes") or 0)
            record["eventLines"] = int(carried.get("eventLines") or 0)
        sidecar["pods"][pod] = record
    return record


def _eventRecordFor(sidecar: dict[str, Any], name: str) -> dict[str, Any]:
    """The record that counts ``name``'s k8s/events bytes.

    An app-log pod keeps its counters on its own ``pods`` record; anything
    else gets an ``eventPods`` record, which carries no watermark and so
    cannot hold the night's coverage back.
    """
    record = sidecar["pods"].get(name)
    if record is not None:
        return record
    return sidecar.setdefault("eventPods", {}).setdefault(name, _emptyEventRecord())


def _recoverNight(nightDir: Path, sidecar: dict[str, Any]) -> None:
    """Reconcile on-disk files with the sidecar after a restart.

    A crash can leave a pod file longer than its recorded byte count
    (an append raced the sidecar write) or leave files the sidecar
    has never heard of. Truncating back to the recorded counts makes
    the next increment re-fetch exactly the unrecorded span, so no
    line is duplicated or lost. Staged temp files are stranded by the
    same crash and are simply removed.
    """
    for f in nightDir.iterdir():
        if f.is_file() and f.name.startswith(_TEMP_PREFIXES):
            f.unlink(missing_ok=True)
    pods: dict[str, dict[str, Any]] = sidecar.get("pods") or {}
    eventPods: dict[str, dict[str, Any]] = sidecar.get("eventPods") or {}
    for sub, key, tables in (
        (PODS_DIR_NAME, "bytes", (pods,)),
        (PODS_EVENTS_DIR_NAME, "eventBytes", (pods, eventPods)),
    ):
        subDir = nightDir / sub
        for f in subDir.glob("*.jsonl"):
            record = next((t[f.stem] for t in tables if f.stem in t), None)
            recorded = int(record.get(key) or 0) if record else 0
            if recorded <= 0:
                f.unlink(missing_ok=True)
            elif f.stat().st_size > recorded:
                with open(f, "ab") as fh:
                    fh.truncate(recorded)


def _exposureKey(record: exposureTimes.ExposureRecord) -> tuple[str, int] | None:
    """``(instrument, exposureId)`` — an exposure's real identity, or None."""
    instrument = exposureTimes.recordInstrument(record)
    dataId = exposureTimes.recordExposureId(record)
    if instrument is None or dataId is None:
        return None
    return instrument, dataId


def _taiIsoToUtc(taiIso: str) -> dt.datetime:
    """Parse a ConsDB ``obs_end`` (TAI, no zone suffix) into aware UTC."""
    t = dt.datetime.fromisoformat(taiIso.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc) - dt.timedelta(seconds=exposureTimes.TAI_MINUS_UTC_S)
