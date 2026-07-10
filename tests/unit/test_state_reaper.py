from __future__ import annotations

from datetime import timedelta

from awareness.schemas.doc import SourceKind
from awareness.schemas.jobs import JobKind, JobState, TaskState
from awareness.storage.state import StateDB, TaskRow, _utcnow


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


def test_reaper_requeues_only_stale_running(tmp_path) -> None:
    state = _state_with_tasks(tmp_path, 2)
    claimed = state.claim_pending_tasks("j1", limit=2)  # both RUNNING, started_at≈now
    with state.session() as s:
        r = s.get(TaskRow, claimed[0].task_id)
        r.started_at = _utcnow() - timedelta(seconds=10_000)
        s.commit()
    requeued = state.requeue_orphaned_running("j1", older_than_seconds=900, max_retries=3)
    assert requeued == 1
    counts = state.task_status_counts("j1")
    assert counts.get("pending") == 1  # the stale one came back
    assert counts.get("running") == 1  # the fresh one stayed RUNNING
