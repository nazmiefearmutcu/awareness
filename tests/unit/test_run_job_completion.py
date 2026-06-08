from __future__ import annotations

from awareness.planner.planner import Planner
from awareness.schemas.jobs import JobKind, JobState, JobStatus
from awareness.storage.state import StateDB
from awareness.workers.engine import WorkerEngine


def _engine(tmp_project):
    state = StateDB(f"sqlite:///{tmp_project / 'state.db'}")
    state.init()
    state.create_job(JobState(job_id="j1", kind=JobKind.BACKFILL, request={}))
    return state, WorkerEngine(state, Planner(state), concurrency=1, silent_progress=True)


def test_should_complete_logic() -> None:
    assert WorkerEngine._should_complete(drained=True, stopping=False) is True
    assert WorkerEngine._should_complete(drained=False, stopping=False) is False
    assert WorkerEngine._should_complete(drained=True, stopping=True) is False


async def test_run_job_with_zero_tasks_completes(tmp_project) -> None:
    state, engine = _engine(tmp_project)
    await engine.run_job("j1", poll_seconds=0.01)
    await engine.aclose()
    assert state.get_job("j1").status == JobStatus.COMPLETED


async def test_run_job_stopped_before_drain_does_not_complete(tmp_project) -> None:
    state, engine = _engine(tmp_project)
    engine.request_stop()  # stop before any draining
    await engine.run_job("j1", poll_seconds=0.01)
    await engine.aclose()
    assert state.get_job("j1").status == JobStatus.RUNNING
