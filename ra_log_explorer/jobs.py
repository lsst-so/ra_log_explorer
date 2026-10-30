"""In-process fetch-job manager.

A `FetchJob` is one running (or finished) call to `fetch.fetchAll`. The
manager keeps a small registry of jobs keyed by id, owns each job's
progress event log, and owns the lock that guards the server's shared
`ServerState`.

We keep this in-process rather than reaching for Celery / Redis / etc.
— the tool is single-user, single-machine, and a fetch takes ~90 s.
Stdlib `threading` + a condition variable is plenty.

Progress is modelled as an append-only event list plus a
`threading.Condition`. Any number of SSE readers can connect to the
same job (including *after* it has finished) and replay the full
history; this is friendlier than a queue, which only one reader could
ever consume.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .config import FetchSpec
from .fetch import fetchAll, loadCacheMeta

JobStatus = Literal["pending", "running", "parsing", "done", "error"]
JobKind = Literal["exposure", "night", "range"]
ProgressEvent = dict[str, Any]

log = logging.getLogger(__name__)

# Finished jobs kept for late readers of their progress (see JobManager._register).
_MAX_FINISHED_JOBS = 50


@dataclass
class FetchJob:
    """One run of `fetchAll`, with its own append-only event log.

    All three modes reuse this single shape:

    * Exposure mode: ``kind == "exposure"``, ``expId`` and ``tZero``
      set, ``dayObs`` is None.
    * Night mode: ``kind == "night"``, ``dayObs`` set, ``expId`` and
      ``tZero`` are None.
    * Range mode: ``kind == "range"``, ``startId`` / ``stopId`` and the
      two UTC window anchors ``tZeroStart`` / ``tZeroStop`` set; ``expId``
      / ``tZero`` / ``dayObs`` are None.
    """

    jobId: str
    spec: FetchSpec
    # The named site this fetch belongs to (see ``sites.py``); drives
    # which ConsDB endpoint resolves shutter-close times for the
    # resulting ServerState / NightState and which per-site exposure-
    # time cache file the resolved values land in.
    siteName: str = ""
    # The instrument this fetch is *for* (lowercase table name, e.g.
    # "lsstcam"). Part of the exposure's identity — the same dataId
    # names a different exposure per instrument — so it pins every
    # ConsDB resolution downstream of this job and scopes which pods
    # the resulting view attributes work to. None only for night jobs,
    # which are AOS and therefore LSSTCam by construction.
    instrument: str | None = None
    kind: JobKind = "exposure"
    expId: int | None = None
    tZero: dt.datetime | None = None
    dayObs: int | None = None
    # Night mode: which half of the night this fetch covers (see
    # `config.NIGHT_VIEWS`). It has to ride on the job rather than be
    # re-derived from the spec, because the client asks `/api/summary`
    # for the finished night by name the moment the `done` event lands
    # and would otherwise open the wrong tab's data.
    nightView: str = "aos"
    # Range mode: the [startId, stopId] dataId span plus the two UTC
    # shutter-close anchors that define the fetch window.
    startId: int | None = None
    stopId: int | None = None
    tZeroStart: dt.datetime | None = None
    tZeroStop: dt.datetime | None = None
    # Set for a job that rebuilds a view from a window already on disk
    # rather than fetching one: the fetch is skipped and everything after
    # it runs as for any other job.
    rebuildFrom: Path | None = None
    status: JobStatus = "pending"
    startedAt: dt.datetime | None = None
    finishedAt: dt.datetime | None = None
    cacheDir: Path | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    # Append-only list of progress events. Always read alongside the
    # condition variable; never mutate directly.
    events: list[ProgressEvent] = field(default_factory=list)
    condition: threading.Condition = field(default_factory=threading.Condition)

    def push(self, event: ProgressEvent) -> None:
        with self.condition:
            self.events.append(event)
            self.condition.notify_all()

    def isTerminal(self) -> bool:
        return self.status in ("done", "error")

    def key(self) -> tuple[object, ...]:
        """What makes two jobs the same piece of work (see :meth:`JobManager.startJob`)."""
        if self.kind == "night":
            return ("night", self.dayObs, self.nightView)
        if self.kind == "range":
            return ("range", self.instrument, self.startId, self.stopId, self.spec.fromIso, self.spec.toIso)
        return ("exposure", self.instrument, self.expId, self.spec.fromIso, self.spec.toIso)

    def describe(self) -> str:
        """Short human label for log lines, e.g. ``exposure lsstcam:2026051900722``."""
        if self.kind == "night":
            return f"night {self.dayObs} ({self.nightView})"
        if self.kind == "range":
            return f"range {self.instrument}:{self.startId}-{self.stopId}"
        return f"exposure {self.instrument}:{self.expId}"


# Callback signature for whatever wants to react to a finished fetch
# (typically: parse the cache and swap in a new ServerState).
OnComplete = Callable[["FetchJob"], None]


class JobManager:
    """Thread-safe registry of fetch jobs.

    Also exposes a single ``stateLock`` that callers use to guard the
    shared `ServerState` — keeping it here keeps everyone touching
    fetch results going through the same lock.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, FetchJob] = {}
        # Started and not yet terminal, by FetchJob.key.
        self._running: dict[tuple[object, ...], FetchJob] = {}
        self._lock = threading.Lock()
        self.stateLock = threading.Lock()

    def createJob(
        self,
        spec: FetchSpec,
        expId: int,
        tZero: dt.datetime,
        siteName: str = "",
        instrument: str | None = None,
    ) -> FetchJob:
        with self._lock:
            jobId = uuid.uuid4().hex[:12]
            job = FetchJob(
                jobId=jobId,
                spec=spec,
                siteName=siteName,
                instrument=instrument,
                kind="exposure",
                expId=expId,
                tZero=tZero,
            )
            self._register(job)
            return job

    def createNightJob(
        self, spec: FetchSpec, dayObs: int, siteName: str = "", nightView: str = "aos"
    ) -> FetchJob:
        with self._lock:
            jobId = uuid.uuid4().hex[:12]
            job = FetchJob(
                jobId=jobId,
                spec=spec,
                siteName=siteName,
                kind="night",
                dayObs=dayObs,
                nightView=nightView,
            )
            self._register(job)
            return job

    def createRangeJob(
        self,
        spec: FetchSpec,
        startId: int,
        stopId: int,
        tZeroStart: dt.datetime,
        tZeroStop: dt.datetime,
        siteName: str = "",
        instrument: str | None = None,
    ) -> FetchJob:
        with self._lock:
            jobId = uuid.uuid4().hex[:12]
            job = FetchJob(
                jobId=jobId,
                spec=spec,
                siteName=siteName,
                instrument=instrument,
                kind="range",
                startId=startId,
                stopId=stopId,
                tZeroStart=tZeroStart,
                tZeroStop=tZeroStop,
            )
            self._register(job)
            return job

    def _register(self, job: FetchJob) -> None:
        """Add ``job``, forgetting all but the newest finished jobs. Call under ``_lock``.

        A job keeps its whole progress log, one event per pod fetched and
        another per pod parsed: ~900 for a summit exposure. Kept forever,
        that is tens of megabytes a day of a long-running process. What
        is still worth keeping after a job ends is only a page coming
        back for its last events.
        """
        self._jobs[job.jobId] = job
        finished = sorted(
            (j for j in self._jobs.values() if j.isTerminal()),
            key=lambda j: j.finishedAt or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
        )
        for old in finished[: max(0, len(finished) - _MAX_FINISHED_JOBS)]:
            del self._jobs[old.jobId]

    def getJob(self, jobId: str) -> FetchJob | None:
        with self._lock:
            return self._jobs.get(jobId)

    def listJobs(self) -> list[FetchJob]:
        with self._lock:
            return list(self._jobs.values())

    def runJob(self, job: FetchJob, onComplete: OnComplete, after: FetchJob | None = None) -> None:
        """Execute one job on the calling thread, once ``after`` (if any) has finished.

        Pushes events to ``job.events``; updates ``job.status`` along the way.
        Calls ``onComplete(job)`` *before* the final terminal event so any
        SSE consumer waiting on the `done`/`error` signal will only see it
        after the server state has been updated.
        """
        try:
            if after is not None:
                with after.condition:
                    after.condition.wait_for(after.isTerminal)
            self._run(job, onComplete)
        finally:
            with self._lock:
                if self._running.get(job.key()) is job:
                    del self._running[job.key()]

    def _run(self, job: FetchJob, onComplete: OnComplete) -> None:
        job.status = "running"
        startedAt = job.startedAt = dt.datetime.now(dt.timezone.utc)
        job.push({"type": "start", "fromIso": job.spec.fromIso, "toIso": job.spec.toIso})
        if job.rebuildFrom is not None:
            log.info("Job %s: %s, rebuilding from %s", job.jobId, job.describe(), job.rebuildFrom)
        else:
            log.info(
                "Job %s: %s, window %s to %s", job.jobId, job.describe(), job.spec.fromIso, job.spec.toIso
            )

        def cb(pod: str, i: int, total: int) -> None:
            # fetchAll loops over completed futures sequentially, so this
            # callback is already serialised; no extra lock needed.
            job.push({"type": "pod-done", "pod": pod, "i": i, "total": total})

        try:
            if job.rebuildFrom is not None:
                cacheDir, meta = job.rebuildFrom, loadCacheMeta(job.rebuildFrom)
                meta["fromCache"] = True
                meta["cacheReuse"] = "rebuild"
            else:
                cacheDir, meta = fetchAll(job.spec, progress=cb)
            job.cacheDir = cacheDir
            job.meta = meta
            job.status = "parsing"
            job.push(
                {
                    "type": "parsing",
                    "cacheReuse": meta.get("cacheReuse", "none"),
                    "podCount": meta.get("pod_count", 0),
                    "totalBytes": meta.get("total_bytes", 0),
                }
            )
            onComplete(job)
            job.status = "done"
            finishedAt = job.finishedAt = dt.datetime.now(dt.timezone.utc)
            # This job's own time. The meta's elapsed_s is the fetch that
            # wrote the window, which for a rebuild or a cache hit could
            # have been minutes, days ago.
            elapsedS = (finishedAt - startedAt).total_seconds()
            podCount = meta.get("pod_count", 0)
            log.info(
                "Job %s done in %.1fs: %d pods, cache %s, %s",
                job.jobId,
                elapsedS,
                podCount,
                meta.get("cacheReuse", "none"),
                cacheDir,
            )
            if not podCount:
                log.warning("Job %s (%s) found no pods in its window", job.jobId, job.describe())
            job.push(
                {
                    "type": "done",
                    "kind": job.kind,
                    "expId": job.expId,
                    # The pin this fetch ran under. The client needs it
                    # to ask for the right state afterwards: the bare
                    # expId's slot may by then hold the other
                    # instrument's exposure of the same id.
                    "instrument": job.instrument,
                    "tZero": job.tZero.isoformat() if job.tZero else None,
                    "dayObs": job.dayObs,
                    # Same reasoning as ``instrument`` above, for nights:
                    # a dayObs alone no longer names one loaded state.
                    "nightView": job.nightView if job.kind == "night" else None,
                    "startId": job.startId,
                    "stopId": job.stopId,
                    "cacheDir": str(cacheDir),
                    "cacheReuse": meta.get("cacheReuse", "none"),
                    "podCount": meta.get("pod_count", 0),
                    "totalBytes": meta.get("total_bytes", 0),
                    "elapsedS": elapsedS,
                }
            )
        except Exception as e:  # noqa: BLE001 — we surface every failure
            job.status = "error"
            job.finishedAt = dt.datetime.now(dt.timezone.utc)
            job.error = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=4)}"
            log.exception("Job %s (%s) failed", job.jobId, job.describe())
            job.push({"type": "error", "error": job.error})

    def startJob(self, job: FetchJob, onComplete: OnComplete) -> FetchJob:
        """Run `runJob` in a background daemon thread, or join an identical job.

        Returns the job to follow: ``job`` itself, or the one already
        running for the same :meth:`FetchJob.key`, in which case ``job``
        is dropped unstarted. A reload, a second user, or a rebuild racing
        a fetch would otherwise each parse the same window, and for a
        summit night that is a minute of CPU and a second copy of the
        parsed night in memory.

        A fetch never joins a rebuild, though: a rebuild re-parses what
        is on disk, which for the night in progress is the "night so far"
        slice of the *last* fetch, and whoever asked to fetch asked for
        the night now. The fetch waits for the rebuild to finish and then
        runs, so what it fetched is what ends up published, and it takes
        the rebuild's place as the job later requests join.
        """
        after: FetchJob | None = None
        with self._lock:
            running = self._running.get(job.key())
            # Terminal but not yet forgotten: runJob drops it from _running
            # only after its last event, so it can still be here, finished.
            if running is not None and not running.isTerminal():
                if job.rebuildFrom is not None or running.rebuildFrom is None:
                    self._jobs.pop(job.jobId, None)
                    return running
                after = running
            self._running[job.key()] = job
        thread = threading.Thread(
            target=self.runJob,
            args=(job, onComplete, after),
            name=f"fetchjob-{job.jobId}",
            daemon=True,
        )
        thread.start()
        return job
