from __future__ import annotations

from awareness.planner.planner import Planner
from awareness.schemas.jobs import JobKind, JobState, JobStatus
from awareness.storage.state import StateDB
from awareness.tail.engine import TailEngine


async def test_tail_resume_sets_running_without_nameerror(tmp_project) -> None:
    state = StateDB(f"sqlite:///{tmp_project / 'state.db'}")
    state.init()
    planner = Planner(state)
    job_id = "tail-resume-1"
    state.create_job(JobState(job_id=job_id, kind=JobKind.TAIL, request={}))

    engine = TailEngine(state, planner)
    # Resume path (explicit job_id): previously raised NameError: JobStatus.
    await engine.start(job_id=job_id, gdelt=False)
    try:
        assert state.get_job(job_id).status == JobStatus.RUNNING
    finally:
        await engine.stop(drain_seconds=2.0)
