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
readiness, error states — is what ``GET /api/live`` returns.
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
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from . import exposureTimes
from .config import (
    DEFAULT_WINDOW_AFTER_S,
    FetchSpec,
    currentDayObs,
    dayObsEndUtc,
    dayObsStartUtc,
    windowCachePath,
)
from .fetch import (
    CACHE_SCHEMA_VERSION,
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


def _emptyPodRecord(watermarkIso: str) -> dict[str, Any]:
    """A fresh per-pod sidecar record, anchored at ``watermarkIso``."""
    return {"watermarkIso": watermarkIso, "bytes": 0, "lines": 0, "eventBytes": 0, "eventLines": 0}


def _appendFile(src: Path, dst: Path) -> None:
    """Append ``src``'s bytes to ``dst`` (creating it if needed)."""
    with open(src, "rb") as fromFh, open(dst, "ab") as toFh:
        shutil.copyfileobj(fromFh, toFh)


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
    ) -> None:
        self._site = site
        self._username = username
        self._workers = max(1, workers)
        self._pollS = max(30.0, pollS)
        self._lagS = max(0.0, lagS)
        self._windowAfterS = windowAfterS
        self._lock = threading.Lock()
        self._stopEvent = threading.Event()
        self._thread: threading.Thread | None = None
        # Poller-thread-only state for the currently-open night.
        self._dayObs: int | None = None
        self._nightDir: Path | None = None
        self._sidecar: dict[str, Any] | None = None
        self._exposures: dict[int, exposureTimes.ExposureRecord] = {}
        self._storedExposureIds: set[int] = set()
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
        dayObs = currentDayObs(now)
        if self._dayObs is not None and dayObs != self._dayObs:
            self._finaliseNight()
            self._dayObs = None
        if self._dayObs is None:
            self._openNight(dayObs)
            # Publish before the first increment: the initial catch-up
            # can run for minutes, and until it finishes the UI should
            # show "catching up from <watermark>" rather than nothing.
            self._publishSnapshot()
        assert self._sidecar is not None
        tickStats: dict[str, Any] = {"startedAt": now.isoformat()}
        started = time.time()
        if not self._sidecar.get("finalised"):
            target = min(now - dt.timedelta(seconds=self._lagS), dayObsEndUtc(dayObs))
            newLines, activePods = self._fetchIncrement(target)
            tickStats.update({"newLines": newLines, "activePods": activePods})
        self._consdbTick(dayObs)
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

    def _openNight(self, dayObs: int) -> None:
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
                "errors": {},
                "incomplete_pods": {},
            }
            writeLiveSidecar(nightDir, sidecar)
        else:
            self._recoverNight(nightDir, sidecar)
        self._dayObs = dayObs
        self._nightDir = nightDir
        self._sidecar = sidecar
        self._exposures = {}
        self._storedExposureIds = set()
        self._consdbError = None

    def _recoverNight(self, nightDir: Path, sidecar: dict[str, Any]) -> None:
        """Reconcile on-disk files with the sidecar after a restart.

        A crash can leave a pod file longer than its recorded byte count
        (an append raced the sidecar write) or leave files the sidecar
        has never heard of. Truncating back to the recorded counts makes
        the next increment re-fetch exactly the unrecorded span, so no
        line is duplicated or lost.
        """
        pods: dict[str, dict[str, Any]] = sidecar.get("pods") or {}
        for sub, key in ((PODS_DIR_NAME, "bytes"), (PODS_EVENTS_DIR_NAME, "eventBytes")):
            subDir = nightDir / sub
            for f in subDir.glob("*.jsonl"):
                record = pods.get(f.stem)
                recorded = int(record.get(key) or 0) if record else 0
                if recorded <= 0:
                    f.unlink(missing_ok=True)
                elif f.stat().st_size > recorded:
                    with open(f, "ab") as fh:
                        fh.truncate(recorded)

    def _finaliseNight(self) -> None:
        """Top up to night end, verify per-pod totals, write ``_meta.json``.

        After this the night dir is a complete, trustworthy all-pods
        window: the sidecar stays (marked finalised) so sub-window
        requests keep being served by slicing, and the ordinary cache
        machinery handles listing, LRU eviction, and deletion.
        """
        assert self._dayObs is not None and self._nightDir is not None and self._sidecar is not None
        dayObs, nightDir, sidecar = self._dayObs, self._nightDir, self._sidecar
        if sidecar.get("finalised"):
            return
        spec = self._nightSpec(dayObs)
        nightStart = dayObsStartUtc(dayObs)
        nightEnd = dayObsEndUtc(dayObs)
        self._fetchIncrement(nightEnd)
        pods: dict[str, dict[str, Any]] = sidecar["pods"]
        errors: dict[str, str] = sidecar["errors"]
        incomplete: dict[str, str] = sidecar["incomplete_pods"]
        # Verification: the oracle never under-counts a completed window,
        # so a shortfall beyond its dedup slack (see the tolerance
        # constants above) means something slipped through the tick
        # seams — most plausibly ingestion lag beyond LIVE_LAG_S. Those
        # pods are re-fetched whole: the night is about to become a
        # permanent cache, so this is the last chance to be right.
        podExpected: dict[str, int] = {}
        for pod in sorted(pods):
            expected = countPodWindow(spec, pod, nightStart, nightEnd)
            if expected is None:
                continue
            podExpected[pod] = expected
            shortfall = expected - int(pods[pod].get("lines") or 0)
            if shortfall <= max(VERIFY_TOLERANCE_LINES, expected // VERIFY_TOLERANCE_DIVISOR):
                continue
            try:
                self._refetchWholePod(spec, pod, nightStart, nightEnd)
                errors.pop(pod, None)
                incomplete.pop(pod, None)
            except FetchError as e:
                errors[pod] = f"end-of-night refetch failed: {e}"
        (nightDir / PODS_LIST_NAME).write_text("\n".join(sorted(pods)) + "\n")
        meta = {
            "spec": asdict(spec),
            "fetchSchemaVersion": CACHE_SCHEMA_VERSION,
            "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "elapsed_s": 0.0,
            "pod_count": len(pods),
            "total_bytes": sum(int(r.get("bytes") or 0) for r in pods.values()),
            "pod_bytes": {p: int(r.get("bytes") or 0) for p, r in pods.items()},
            "pod_lines": {p: int(r.get("lines") or 0) for p, r in pods.items()},
            "pod_expected": podExpected,
            "errors": errors,
            "incomplete_pods": incomplete,
            "fetchComplete": not errors and not incomplete,
            "pod_event_lines": {p: int(r.get("eventLines") or 0) for p, r in pods.items()},
            "event_errors": {},
            "window_in_past": True,
            "fromCache": False,
            "cacheReuse": "none",
            "liveBuilt": True,
        }
        (nightDir / META_NAME).write_text(json.dumps(meta, indent=2))
        sidecar["finalised"] = True
        sidecar["watermarkIso"] = spec.toIso
        sidecar["updatedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
        writeLiveSidecar(nightDir, sidecar)
        markCacheViewed(nightDir)

    def _refetchWholePod(self, spec: FetchSpec, pod: str, fromT: dt.datetime, toT: dt.datetime) -> None:
        """Replace one pod's file with a fresh full-window fetch."""
        assert self._nightDir is not None and self._sidecar is not None
        podFile = self._nightDir / PODS_DIR_NAME / f"{pod}.jsonl"
        fd, tmpName = tempfile.mkstemp(prefix="live-refetch-", suffix=".jsonl", dir=self._nightDir)
        tmpPath = Path(tmpName)
        try:
            with os.fdopen(fd, "wb") as fh:
                lines, complete, reason = fetchPodWindowInto(spec, pod, fromT, toT, fh)
            os.replace(tmpPath, podFile)
        finally:
            tmpPath.unlink(missing_ok=True)
        record = self._sidecar["pods"].setdefault(pod, _emptyPodRecord(spec.toIso))
        record["bytes"] = podFile.stat().st_size
        record["lines"] = lines
        record["watermarkIso"] = spec.toIso
        if not complete:
            self._sidecar["incomplete_pods"][pod] = reason

    # ----- increment fetching -----------------------------------------------

    def _fetchIncrement(self, target: dt.datetime) -> tuple[int, int]:
        """Advance every pod's coverage to ``target``. Returns (lines, pods).

        The listing window is anchored at the *global* watermark (the
        minimum over per-pod watermarks) so a pod that failed last tick
        is still discovered even though its healthy peers have moved on;
        each pod then fetches from its own watermark, so nothing is
        double-fetched.
        """
        assert self._dayObs is not None and self._nightDir is not None and self._sidecar is not None
        sidecar = self._sidecar
        spec = self._nightSpec(self._dayObs)
        nightStart = dayObsStartUtc(self._dayObs)
        globalW = _parseIso(sidecar["watermarkIso"])
        if target <= globalW:
            return 0, 0
        targetIso = _fmtLogcliTime(target)
        listSpec = replace(spec, fromIso=_fmtLogcliTime(globalW), toIso=targetIso)
        active = set(listPods(listSpec))  # a listing failure aborts the tick; retried next poll
        pods: dict[str, dict[str, Any]] = sidecar["pods"]
        newLines = 0
        podsDir = self._nightDir / PODS_DIR_NAME
        with ThreadPoolExecutor(max_workers=self._workers) as ex:
            futures = {}
            for pod in sorted(active):
                fromT = _parseIso(pods[pod]["watermarkIso"]) if pod in pods else nightStart
                if fromT >= target:
                    continue
                fut = ex.submit(self._stageWindow, spec, pod, fromT, target, podsDir / f"{pod}.jsonl")
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
                    pods.setdefault(pod, _emptyPodRecord(_fmtLogcliTime(fromT)))
                    continue
                record = pods.setdefault(pod, _emptyPodRecord(sidecar["fromIso"]))
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
        self._appendEventsIncrement(spec, target)
        watermarks = [_parseIso(r["watermarkIso"]) for r in pods.values()]
        sidecar["watermarkIso"] = _fmtLogcliTime(min([target, *watermarks]))
        sidecar["updatedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
        (self._nightDir / PODS_LIST_NAME).write_text("\n".join(sorted(pods)) + "\n")
        writeLiveSidecar(self._nightDir, sidecar)
        markCacheViewed(self._nightDir)
        return newLines, len(active)

    def _stageWindow(
        self, spec: FetchSpec, pod: str, fromT: dt.datetime, toT: dt.datetime, podFile: Path
    ) -> tuple[int, int, bool, str]:
        """Fetch one pod's increment via a temp file, then append it.

        The temp-file staging is what makes a failed increment safely
        retryable: nothing reaches the pod's real file unless the whole
        window fetched, so the caller can leave the watermark alone and
        try the same span again next tick.
        """
        fd, tmpName = tempfile.mkstemp(prefix="live-inc-", suffix=".jsonl")
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

    def _appendEventsIncrement(self, spec: FetchSpec, target: dt.datetime) -> None:
        """Fetch the namespace's k8s/events increment and demux per pod.

        One chunked query per tick for the whole namespace (the stream
        is low-volume), split by the ``name`` label into the same
        per-pod files a batch fetch writes. Best-effort like the batch
        path: a failure leaves the events watermark alone (the span is
        retried next tick) and never blocks the app-log watermark.
        """
        assert self._nightDir is not None and self._sidecar is not None
        sidecar = self._sidecar
        eventsW = _parseIso(sidecar.get("eventsWatermarkIso") or sidecar["fromIso"])
        if target <= eventsW:
            return
        fd, tmpName = tempfile.mkstemp(prefix="live-events-", suffix=".jsonl")
        tmpPath = Path(tmpName)
        try:
            try:
                with os.fdopen(fd, "wb") as fh:
                    fetchEventsWindowInto(spec, eventsW, target, fh)
            except FetchError as e:
                sidecar["eventsError"] = str(e)
                return
            perPod: dict[str, list[bytes]] = {}
            with open(tmpPath, "rb") as fh:
                for raw in fh:
                    try:
                        name = json.loads(raw).get("labels", {}).get("name")
                    except json.JSONDecodeError:
                        continue
                    if name:
                        perPod.setdefault(name, []).append(raw)
            eventsDir = self._nightDir / PODS_EVENTS_DIR_NAME
            for pod, rawLines in perPod.items():
                with open(eventsDir / f"{pod}.jsonl", "ab") as out:
                    for raw in rawLines:
                        out.write(raw)
                record = sidecar["pods"].setdefault(pod, _emptyPodRecord(sidecar["fromIso"]))
                record["eventBytes"] += sum(len(raw) for raw in rawLines)
                record["eventLines"] += len(rawLines)
            sidecar["eventsWatermarkIso"] = _fmtLogcliTime(target)
            sidecar.pop("eventsError", None)
        finally:
            tmpPath.unlink(missing_ok=True)

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
        # Persist only what's new — records are immutable, and rewriting
        # the whole per-site cache file every tick for no change is
        # pointless churn on the cache volume.
        fresh = {eid: rec for eid, rec in records.items() if eid not in self._storedExposureIds}
        if fresh:
            exposureTimes.storeCachedRecords(fresh, siteName=self._site.name)
            self._storedExposureIds.update(fresh)

    def _exposureRows(self, watermark: dt.datetime | None) -> list[dict[str, Any]]:
        """Tonight's exposures, newest first, with readiness computed."""
        rows: list[dict[str, Any]] = []
        for dataId in sorted(self._exposures, reverse=True):
            record = self._exposures[dataId]
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
                    "obsEndUtc": obsEndUtc.isoformat() if obsEndUtc else None,
                    "readyAtUtc": readyAt.isoformat() if readyAt else None,
                    "ready": bool(readyAt is not None and watermark is not None and readyAt <= watermark),
                    "record": record,
                }
            )
        return rows

    # ----- snapshot ---------------------------------------------------------

    def _publishSnapshot(self) -> None:
        sidecar = self._sidecar or {}
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
            "dayObs": self._dayObs,
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
            "consdbError": self._consdbError,
            "lastTick": self._lastTick,
            "lastError": self._lastError,
            "exposures": self._exposureRows(watermark),
        }
        with self._lock:
            self._snapshot = snapshot


def _taiIsoToUtc(taiIso: str) -> dt.datetime:
    """Parse a ConsDB ``obs_end`` (TAI, no zone suffix) into aware UTC."""
    t = dt.datetime.fromisoformat(taiIso.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc) - dt.timedelta(seconds=exposureTimes.TAI_MINUS_UTC_S)
