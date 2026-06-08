from __future__ import annotations

from awareness.schemas.doc import SourceKind
from awareness.schemas.jobs import JobKind, JobState, TaskState, TaskStatus
from awareness.storage.state import StateDB


def _state_with_tasks(tmp_path, n: int) -> StateDB:
    state = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    state.init()
    state.create_job(JobState(job_id="j1", kind=JobKind.BACKFILL, request={}))
    state.add_tasks(
        [
            TaskState(
                task_id=f"t{i}",
                job_id="j1",
                source_type=SourceKind.RSS,
                partition_key=f"rss:p{i}",
                payload={},
            )
            for i in range(n)
        ]
    )
    return state


def test_claims_are_disjoint_and_mark_running(tmp_path) -> None:
    state = _state_with_tasks(tmp_path, 5)
    a = state.claim_pending_tasks("j1", limit=3)
    b = state.claim_pending_tasks("j1", limit=3)
    ids_a = {t.task_id for t in a}
    ids_b = {t.task_id for t in b}
    assert len(ids_a) == 3
    assert len(ids_b) == 2
    assert ids_a.isdisjoint(ids_b)
    for t in a:
        assert t.status == TaskStatus.RUNNING
        assert t.attempts == 1
    assert state.claim_pending_tasks("j1", limit=10) == []


def test_claim_does_not_reclaim_running(tmp_path) -> None:
    state = _state_with_tasks(tmp_path, 2)
    first = state.claim_pending_tasks("j1", limit=2)
    assert len(first) == 2
    assert state.claim_pending_tasks("j1", limit=2) == []
