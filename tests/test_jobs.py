"""Tests for `ra_log_explorer.jobs`.

We monkeypatch `fetchAll` so the tests don't touch the network — we only
care here that the job manager threads events correctly, transitions
status through the expected states, and surfaces errors as a terminal
event.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from ra_log_explorer import jobs
from ra_log_explorer.config import FetchSpec

ProgressCb = Callable[[str, int, int], None]


def _spec() -> FetchSpec:
    return FetchSpec(
        lokiAddr="http://example.invalid",
        username="u",
        cluster="c",
        namespace="n",
        fromIso="2026-05-20T08:45:00Z",
        toIso="2026-05-20T08:50:00Z",
    )


def _tZero() -> dt.datetime:
    return dt.datetime(2026, 5, 20, 8, 45, 39, tzinfo=dt.timezone.utc)


def test_createJob_returns_unique_ids() -> None:
    mgr = jobs.JobManager()
    j1 = mgr.createJob(_spec(), 1, _tZero())
    j2 = mgr.createJob(_spec(), 2, _tZero())
    assert j1.jobId != j2.jobId
    assert mgr.getJob(j1.jobId) is j1
    assert mgr.getJob(j2.jobId) is j2


def test_getJob_returns_None_for_unknown() -> None:
    mgr = jobs.JobManager()
    assert mgr.getJob("doesnotexist") is None


def test_runJob_pushes_events_in_order(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Happy path: 3 pods fetched, parsing, done. Verify event ordering.

    Also pins the contract that ``onComplete`` fires *before* the
    terminal ``done`` event is pushed — the SSE consumer relies on
    `/api/summary` already reflecting the new state by the time it
    sees ``done``.
    """
    pods = ["pod-a", "pod-b", "pod-c"]

    def fakeFetchAll(
        spec: FetchSpec,
        progress: ProgressCb | None = None,
        forceRefresh: bool = False,
    ) -> tuple[Path, dict]:
        for i, p in enumerate(pods, 1):
            if progress is not None:
                progress(p, i, len(pods))
        return tmp_path, {
            "spec": {},
            "cacheReuse": "none",
            "pod_count": len(pods),
            "total_bytes": 1234,
            "elapsed_s": 0.01,
        }

    monkeypatch.setattr(jobs, "fetchAll", fakeFetchAll)

    mgr = jobs.JobManager()
    job = mgr.createJob(_spec(), 2026051900722, _tZero())
    # Capture the event-log length *when onComplete fires*. If onComplete
    # ran before the `done` event was pushed, this snapshot will be
    # strictly less than len(job.events) at the end of runJob.
    eventCountAtCompleteTime: list[int] = []

    def onComplete(j: jobs.FetchJob) -> None:
        eventCountAtCompleteTime.append(len(j.events))

    mgr.runJob(job, onComplete=onComplete)

    types = [ev["type"] for ev in job.events]
    assert types == ["start", "pod-done", "pod-done", "pod-done", "parsing", "done"]
    # Status path: pending->running->parsing->done. Final is done.
    assert job.status == "done"
    assert job.error is None
    # onComplete fired exactly once, with the job ID we created.
    assert len(eventCountAtCompleteTime) == 1
    # And it fired before the terminal `done` event landed in the log:
    # the snapshot at onComplete time must be strictly less than the
    # final event count.
    assert eventCountAtCompleteTime[0] < len(job.events)
    assert job.events[eventCountAtCompleteTime[0]]["type"] == "done"


def test_runJob_propagates_fetchAll_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fakeFetchAll(
        spec: FetchSpec,
        progress: ProgressCb | None = None,
        forceRefresh: bool = False,
    ) -> tuple[Path, dict]:
        raise RuntimeError("kapow")

    monkeypatch.setattr(jobs, "fetchAll", fakeFetchAll)
    mgr = jobs.JobManager()
    job = mgr.createJob(_spec(), 1, _tZero())

    onCompleteCalled: list[bool] = []

    def onComplete(j: jobs.FetchJob) -> None:
        onCompleteCalled.append(True)

    mgr.runJob(job, onComplete=onComplete)
    assert job.status == "error"
    assert job.error is not None
    assert "RuntimeError" in job.error
    assert "kapow" in job.error
    # onComplete is not invoked on the error path: the server has nothing
    # to swap into ServerState.
    assert onCompleteCalled == []
    # The terminal event must be of type 'error' so SSE consumers know
    # to stop waiting.
    assert job.events[-1]["type"] == "error"


def test_isTerminal() -> None:
    job = jobs.FetchJob(jobId="x", spec=_spec(), expId=1, tZero=_tZero())
    assert not job.isTerminal()
    job.status = "running"
    assert not job.isTerminal()
    job.status = "parsing"
    assert not job.isTerminal()
    job.status = "done"
    assert job.isTerminal()
    job.status = "error"
    assert job.isTerminal()


def test_startJob_runs_in_thread(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import threading

    started: list[bool] = []
    fetchEvent = threading.Event()

    def fakeFetchAll(
        spec: FetchSpec,
        progress: ProgressCb | None = None,
        forceRefresh: bool = False,
    ) -> tuple[Path, dict]:
        started.append(True)
        fetchEvent.wait(timeout=2.0)  # block until the test releases us
        return tmp_path, {"spec": {}, "cacheReuse": "none", "pod_count": 0, "total_bytes": 0}

    monkeypatch.setattr(jobs, "fetchAll", fakeFetchAll)
    mgr = jobs.JobManager()
    job = mgr.createJob(_spec(), 1, _tZero())
    mgr.startJob(job, onComplete=lambda j: None)

    # startJob is non-blocking: we should be back here while the fake
    # fetchAll is still waiting on its event.
    for _ in range(40):
        if started:
            break
        time.sleep(0.01)
    assert started == [True], "background fetch thread didn't start"

    # Job should be in running or parsing state, not yet terminal.
    assert job.status in ("running", "pending")

    # Release the fetch; wait for the job to finish.
    fetchEvent.set()
    for _ in range(100):
        if job.isTerminal():
            break
        time.sleep(0.01)
    assert job.status == "done"


def test_fetchjob_push_event_notifies_waiters() -> None:
    """A waiter on job.condition wakes up when push() runs."""
    job = jobs.FetchJob(jobId="x", spec=_spec(), expId=1, tZero=_tZero())
    received: list[dict] = []
    import threading

    def waiter() -> None:
        with job.condition:
            while not received and not job.isTerminal():
                job.condition.wait(timeout=2.0)
                received.extend(job.events)

    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    time.sleep(0.05)  # give the waiter time to call wait()
    job.push({"type": "hello"})
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert received == [{"type": "hello"}]


def test_createNightJob_distinct_from_exposure_job() -> None:
    """Night jobs carry ``dayObs`` and ``kind='night'``, no expId / tZero.
    Without this distinction the per-tab routing in the server would
    misclassify night fetches as exposure fetches."""
    mgr = jobs.JobManager()
    job = mgr.createNightJob(_spec(), 20260521)
    assert job.kind == "night"
    assert job.dayObs == 20260521
    assert job.expId is None
    assert job.tZero is None
    # Job is registered alongside exposure jobs in the same id space.
    assert mgr.getJob(job.jobId) is job


def test_runJob_populates_cacheDir_and_meta_on_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After a successful run, the FetchJob carries the cacheDir + meta
    the caller would otherwise have to fish out of fetchAll directly.
    The SSE consumer + onComplete callback both rely on this."""

    def fakeFetchAll(
        spec: FetchSpec,
        progress: ProgressCb | None = None,
        forceRefresh: bool = False,
    ) -> tuple[Path, dict]:
        return tmp_path, {"spec": {}, "cacheReuse": "exact", "pod_count": 7, "total_bytes": 999}

    monkeypatch.setattr(jobs, "fetchAll", fakeFetchAll)
    mgr = jobs.JobManager()
    job = mgr.createJob(_spec(), 1, _tZero())
    mgr.runJob(job, onComplete=lambda j: None)
    assert job.cacheDir == tmp_path
    assert job.meta["pod_count"] == 7
    assert job.meta["cacheReuse"] == "exact"
    assert job.startedAt is not None and job.finishedAt is not None
    assert job.finishedAt >= job.startedAt


def test_jobManager_stateLock_is_a_real_lock() -> None:
    """A second acquire on the same lock from the same thread must block
    (it's a plain Lock, not RLock). The server depends on this so that a
    deeply nested re-entry would deadlock visibly rather than silently
    interleave."""
    mgr = jobs.JobManager()
    with mgr.stateLock:
        acquired = mgr.stateLock.acquire(blocking=False)
        try:
            assert acquired is False
        finally:
            if acquired:
                mgr.stateLock.release()
