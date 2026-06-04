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
import threading
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .config import FetchSpec
from .fetch import fetchAll

JobStatus = Literal["pending", "running", "parsing", "done", "error"]
JobKind = Literal["exposure", "night", "range"]
ProgressEvent = dict[str, Any]


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
    kind: JobKind = "exposure"
    expId: int | None = None
    tZero: dt.datetime | None = None
    dayObs: int | None = None
    # Range mode: the [startId, stopId] dataId span plus the two UTC
    # shutter-close anchors that define the fetch window.
    startId: int | None = None
    stopId: int | None = None
    tZeroStart: dt.datetime | None = None
    tZeroStop: dt.datetime | None = None
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
        self._lock = threading.Lock()
        self.stateLock = threading.Lock()

    def createJob(self, spec: FetchSpec, expId: int, tZero: dt.datetime, siteName: str = "") -> FetchJob:
        with self._lock:
            jobId = uuid.uuid4().hex[:12]
            job = FetchJob(
                jobId=jobId, spec=spec, siteName=siteName, kind="exposure", expId=expId, tZero=tZero
            )
            self._jobs[jobId] = job
            return job

    def createNightJob(self, spec: FetchSpec, dayObs: int, siteName: str = "") -> FetchJob:
        with self._lock:
            jobId = uuid.uuid4().hex[:12]
            job = FetchJob(jobId=jobId, spec=spec, siteName=siteName, kind="night", dayObs=dayObs)
            self._jobs[jobId] = job
            return job

    def createRangeJob(
        self,
        spec: FetchSpec,
        startId: int,
        stopId: int,
        tZeroStart: dt.datetime,
        tZeroStop: dt.datetime,
        siteName: str = "",
    ) -> FetchJob:
        with self._lock:
            jobId = uuid.uuid4().hex[:12]
            job = FetchJob(
                jobId=jobId,
                spec=spec,
                siteName=siteName,
                kind="range",
                startId=startId,
                stopId=stopId,
                tZeroStart=tZeroStart,
                tZeroStop=tZeroStop,
            )
            self._jobs[jobId] = job
            return job

    def getJob(self, jobId: str) -> FetchJob | None:
        with self._lock:
            return self._jobs.get(jobId)

    def listJobs(self) -> list[FetchJob]:
        with self._lock:
            return list(self._jobs.values())

    def runJob(self, job: FetchJob, onComplete: OnComplete) -> None:
        """Execute one job on the calling thread.

        Pushes events to ``job.events``; updates ``job.status`` along the way.
        Calls ``onComplete(job)`` *before* the final terminal event so any
        SSE consumer waiting on the `done`/`error` signal will only see it
        after the server state has been updated.
        """
        job.status = "running"
        job.startedAt = dt.datetime.now(dt.timezone.utc)
        job.push({"type": "start", "fromIso": job.spec.fromIso, "toIso": job.spec.toIso})

        def cb(pod: str, i: int, total: int) -> None:
            # fetchAll loops over completed futures sequentially, so this
            # callback is already serialised; no extra lock needed.
            job.push({"type": "pod-done", "pod": pod, "i": i, "total": total})

        try:
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
            job.finishedAt = dt.datetime.now(dt.timezone.utc)
            job.push(
                {
                    "type": "done",
                    "kind": job.kind,
                    "expId": job.expId,
                    "tZero": job.tZero.isoformat() if job.tZero else None,
                    "dayObs": job.dayObs,
                    "startId": job.startId,
                    "stopId": job.stopId,
                    "cacheDir": str(cacheDir),
                    "cacheReuse": meta.get("cacheReuse", "none"),
                    "podCount": meta.get("pod_count", 0),
                    "totalBytes": meta.get("total_bytes", 0),
                    "elapsedS": meta.get("elapsed_s", 0.0),
                }
            )
        except Exception as e:  # noqa: BLE001 — we surface every failure
            job.status = "error"
            job.finishedAt = dt.datetime.now(dt.timezone.utc)
            job.error = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=4)}"
            job.push({"type": "error", "error": job.error})

    def startJob(self, job: FetchJob, onComplete: OnComplete) -> None:
        """Run `runJob` in a background daemon thread."""
        thread = threading.Thread(
            target=self.runJob,
            args=(job, onComplete),
            name=f"fetchjob-{job.jobId}",
            daemon=True,
        )
        thread.start()
