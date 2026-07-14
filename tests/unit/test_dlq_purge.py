"""DLQ purge: drop queue rows without re-arming tasks."""

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
    job_id = "job-purge"
    task_id = "task-purge"
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
    state.fail_task(task_id, error="abandon me", dead_letter=True)
    state.increment_job_counters(job_id, dead_lettered=1)
    state.add_dlq(job_id, task_id, {"url": "https://example.com/feed"}, error="abandon me")
    rows = state.list_dlq()
    assert len(rows) == 1
    return job_id, task_id, int(rows[0]["id"])


def test_purge_dlq_removes_row_keeps_task_dead(tmp_path: Path) -> None:
    state = _state(tmp_path)
    job_id, task_id, dlq_id = _seed_dead_task(state)

    result = state.purge_dlq(dlq_id)
    assert result["ok"] is True
    assert result["dlq_id"] == dlq_id
    assert result["task_id"] == task_id
    assert result["job_id"] == job_id
    assert state.count_dlq() == 0
    assert state.get_dlq(dlq_id) is None

    counts = state.task_status_counts(job_id)
    assert counts.get(TaskStatus.DEAD_LETTERED.value, 0) == 1
    assert counts.get(TaskStatus.PENDING.value, 0) == 0

    # Job dead-letter counter unchanged (unlike replay).
    job = state.get_job(job_id)
    assert job is not None
    assert job.tasks_dead_lettered == 1


def test_purge_dlq_missing(tmp_path: Path) -> None:
    state = _state(tmp_path)
    result = state.purge_dlq(424242)
    assert result["ok"] is False
    assert result["reason"] == "dlq_missing"


def test_cli_dlq_purge(tmp_project: Path) -> None:
    from awareness.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    state = StateDB(settings.state_db_url)
    state.init()
    _, _, dlq_id = _seed_dead_task(state)

    res = runner.invoke(app, ["dlq", "purge", str(dlq_id), "--json"])
    assert res.exit_code == 0, res.output
    import json

    payload = json.loads(res.output[res.output.find("{") :])
    assert payload["ok"] is True
    assert payload["dlq_id"] == dlq_id
    assert state.count_dlq() == 0


def test_cli_dlq_purge_missing(tmp_project: Path) -> None:
    res = runner.invoke(app, ["dlq", "purge", "99999", "--json"])
    assert res.exit_code == 1
    assert "dlq_missing" in res.output
