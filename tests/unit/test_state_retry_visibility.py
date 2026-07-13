"""Retry-scheduled task visibility for job/tail status surfaces."""

from __future__ import annotations

from datetime import timedelta

from awareness.schemas.doc import SourceKind
from awareness.schemas.jobs import JobKind, JobState, TaskState
from awareness.storage.state import StateDB, TaskRow, _utcnow


def _state_with_task(tmp_path, *, job_id: str = "j1", task_id: str = "t0") -> StateDB:
    state = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    state.init()
    state.create_job(JobState(job_id=job_id, kind=JobKind.BACKFILL, request={}))
    state.add_tasks(
        [
            TaskState(
                task_id=task_id,
                job_id=job_id,
                source_type=SourceKind.RSS,
                partition_key="rss:p0",
                payload={},
            )
        ]
    )
    return state


def test_count_and_list_retry_scheduled_after_fail(tmp_path) -> None:
    state = _state_with_task(tmp_path)
    [t] = state.claim_pending_tasks("j1", limit=1)
    assert t.attempts == 1
    state.fail_task(t.task_id, error="transient 503", dead_letter=False)

    assert state.count_retry_scheduled("j1") == 1
    rows = state.list_retry_scheduled_tasks("j1", limit=10)
    assert len(rows) == 1
    assert rows[0]["task_id"] == t.task_id
    assert rows[0]["attempts"] == 1
    assert rows[0]["next_attempt_at"] is not None
    assert "503" in (rows[0]["last_error"] or "")
    # Still pending → not claimable until lease expires.
    assert state.claim_pending_tasks("j1", limit=1) == []


def test_retry_scheduled_empty_when_lease_elapsed(tmp_path) -> None:
    state = _state_with_task(tmp_path)
    [t] = state.claim_pending_tasks("j1", limit=1)
    state.fail_task(t.task_id, error="boom", dead_letter=False)

    with state.session() as s:
        row = s.get(TaskRow, t.task_id)
        assert row is not None and row.next_attempt_at is not None
        row.next_attempt_at = _utcnow() - timedelta(seconds=1)
        s.commit()

    assert state.count_retry_scheduled("j1") == 0
    assert state.list_retry_scheduled_tasks("j1") == []
    again = state.claim_pending_tasks("j1", limit=1)
    assert len(again) == 1
    assert again[0].attempts == 2


def test_dead_letter_not_counted_as_retry_scheduled(tmp_path) -> None:
    state = _state_with_task(tmp_path)
    [t] = state.claim_pending_tasks("j1", limit=1)
    state.fail_task(t.task_id, error="fatal", dead_letter=True)
    assert state.count_retry_scheduled("j1") == 0
    assert state.list_retry_scheduled_tasks("j1") == []


def test_list_running_tasks_includes_attempts(tmp_path) -> None:
    state = _state_with_task(tmp_path)
    [t] = state.claim_pending_tasks("j1", limit=1)
    running = state.list_running_tasks("j1")
    assert len(running) == 1
    assert running[0]["task_id"] == t.task_id
    assert running[0]["attempts"] == 1
