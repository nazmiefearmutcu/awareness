"""DLQ replay: re-arm dead-lettered tasks + CLI coverage."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from awareness.cli.main import app
from awareness.schemas.doc import SourceKind
from awareness.schemas.jobs import JobKind, JobState, JobStatus, TaskState, TaskStatus
from awareness.storage.state import StateDB

runner = CliRunner()


def _state(tmp_path: Path) -> StateDB:
    db = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    db.init()
    return db


def _seed_dead_task(state: StateDB) -> tuple[str, str, int]:
    """Create a job + dead-lettered task + DLQ row. Returns job_id, task_id, dlq_id."""
    job_id = "job-replay"
    task_id = "task-replay"
    state.create_job(
        JobState(
            job_id=job_id,
            kind=JobKind.TAIL,
            status=JobStatus.RUNNING,
            request={},
            tasks_total=0,
        )
    )
    state.add_tasks(
        [
            TaskState(
                task_id=task_id,
                job_id=job_id,
                source_type=SourceKind.RSS,
                partition_key="rss:https://example.com/feed",
                payload={"url": "https://example.com/feed"},
                status=TaskStatus.PENDING,
            )
        ]
    )
    state.fail_task(task_id, error="permanent boom", dead_letter=True)
    state.increment_job_counters(job_id, dead_lettered=1)
    state.add_dlq(job_id, task_id, {"url": "https://example.com/feed"}, error="permanent boom")
    rows = state.list_dlq()
    assert len(rows) == 1
    return job_id, task_id, int(rows[0]["id"])


def test_replay_dlq_rearms_task_and_removes_row(tmp_path: Path) -> None:
    state = _state(tmp_path)
    job_id, task_id, dlq_id = _seed_dead_task(state)

    result = state.replay_dlq(dlq_id, reset_attempts=True)
    assert result["ok"] is True
    assert result["task_id"] == task_id
    assert result["job_id"] == job_id
    assert result["previous_status"] == TaskStatus.DEAD_LETTERED.value
    assert result["attempts"] == 0

    assert state.count_dlq() == 0
    assert state.get_dlq(dlq_id) is None

    # Task is claimable again.
    counts = state.task_status_counts(job_id)
    assert counts.get(TaskStatus.PENDING.value, 0) == 1
    assert counts.get(TaskStatus.DEAD_LETTERED.value, 0) == 0

    job = state.get_job(job_id)
    assert job is not None
    assert job.tasks_dead_lettered == 0


def test_replay_dlq_missing(tmp_path: Path) -> None:
    state = _state(tmp_path)
    result = state.replay_dlq(99999)
    assert result["ok"] is False
    assert result["reason"] == "dlq_missing"


def test_replay_dlq_task_missing(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.add_dlq("job-x", "gone-task", {"url": "https://x"}, "err")
    dlq_id = state.list_dlq()[0]["id"]
    result = state.replay_dlq(dlq_id)
    assert result["ok"] is False
    assert result["reason"] == "task_missing"
    # DLQ row kept so operator can inspect.
    assert state.count_dlq() == 1


def test_replay_dlq_keep_attempts(tmp_path: Path) -> None:
    state = _state(tmp_path)
    _, task_id, dlq_id = _seed_dead_task(state)
    # Bump attempts on the dead task via fail path simulation.
    from awareness.storage.state import TaskRow  # noqa: PLC0415

    with state.session() as s:
        row = s.get(TaskRow, task_id)
        assert row is not None
        row.attempts = 3
        s.commit()

    result = state.replay_dlq(dlq_id, reset_attempts=False)
    assert result["ok"] is True
    assert result["attempts"] == 3


def test_cli_dlq_replay(tmp_project: Path) -> None:
    from awareness.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    state = StateDB(settings.state_db_url)
    state.init()
    _, task_id, dlq_id = _seed_dead_task(state)

    ok = runner.invoke(app, ["dlq", "replay", str(dlq_id)])
    assert ok.exit_code == 0, ok.output
    assert "Replayed" in ok.output
    assert task_id in ok.output
    assert state.count_dlq() == 0

    missing = runner.invoke(app, ["dlq", "replay", "99999", "--json"])
    assert missing.exit_code == 1
    assert "dlq_missing" in missing.output
