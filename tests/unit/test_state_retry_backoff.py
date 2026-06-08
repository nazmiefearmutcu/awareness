from __future__ import annotations

from datetime import timedelta

from awareness.schemas.doc import SourceKind
from awareness.schemas.jobs import JobKind, JobState, TaskState
from awareness.storage.state import StateDB, TaskRow, _utcnow


def _state_with_one_task(tmp_path) -> StateDB:
    state = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    state.init()
    state.create_job(JobState(job_id="j1", kind=JobKind.BACKFILL, request={}))
    state.add_tasks(
        [TaskState(task_id="t0", job_id="j1", source_type=SourceKind.RSS, partition_key="rss:p0", payload={})]
    )
    return state


def test_failed_task_backs_off_then_becomes_claimable(tmp_path) -> None:
    state = _state_with_one_task(tmp_path)
    [t] = state.claim_pending_tasks("j1", limit=1)
    state.fail_task(t.task_id, error="boom", dead_letter=False)

    # Pending again, but the backoff lease is in the future → not yet claimable.
    assert state.claim_pending_tasks("j1", limit=1) == []

    # Backdate the lease into the past → claimable again.
    with state.session() as s:
        row = s.get(TaskRow, t.task_id)
        assert row.next_attempt_at is not None
        row.next_attempt_at = _utcnow() - timedelta(seconds=1)
        s.commit()
    again = state.claim_pending_tasks("j1", limit=1)
    assert len(again) == 1
