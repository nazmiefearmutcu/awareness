"""DLQ bulk purge: drop many queue rows without re-arming tasks."""

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


def _seed_dead(
    state: StateDB, *, job_id: str, task_id: str, error: str = "fail"
) -> int:
    if state.get_job(job_id) is None:
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
                partition_key=f"rss:{task_id}",
                payload={"url": f"https://example.com/{task_id}"},
                status=TaskStatus.PENDING,
            )
        ]
    )
    state.fail_task(task_id, error=error, dead_letter=True)
    state.increment_job_counters(job_id, dead_lettered=1)
    state.add_dlq(job_id, task_id, {"url": f"https://example.com/{task_id}"}, error=error)
    rows = state.list_dlq(job_id=job_id, limit=1000)
    # Newest for this seed is first among matching; find by task_id.
    for row in rows:
        if row.get("task_id") == task_id:
            return int(row["id"])
    raise AssertionError(f"DLQ row for {task_id} not found")


def test_purge_dlq_bulk_all(tmp_path: Path) -> None:
    state = _state(tmp_path)
    _seed_dead(state, job_id="j1", task_id="t1")
    _seed_dead(state, job_id="j1", task_id="t2")
    _seed_dead(state, job_id="j2", task_id="t3")
    assert state.count_dlq() == 3

    result = state.purge_dlq_bulk()
    assert result["ok"] is True
    assert result["purged"] == 3
    assert result["remaining"] == 0
    assert state.count_dlq() == 0

    # Tasks stay dead-lettered.
    counts = state.task_status_counts("j1")
    assert counts.get(TaskStatus.DEAD_LETTERED.value, 0) == 2
    job = state.get_job("j1")
    assert job is not None
    assert job.tasks_dead_lettered == 2


def test_purge_dlq_bulk_by_job(tmp_path: Path) -> None:
    state = _state(tmp_path)
    _seed_dead(state, job_id="j-a", task_id="ta1")
    _seed_dead(state, job_id="j-a", task_id="ta2")
    _seed_dead(state, job_id="j-b", task_id="tb1")

    result = state.purge_dlq_bulk(job_id="j-a")
    assert result["purged"] == 2
    assert result["job_id"] == "j-a"
    assert result["remaining"] == 0
    assert state.count_dlq(job_id="j-a") == 0
    assert state.count_dlq(job_id="j-b") == 1
    assert state.count_dlq() == 1


def test_purge_dlq_bulk_limit(tmp_path: Path) -> None:
    state = _state(tmp_path)
    for i in range(5):
        _seed_dead(state, job_id="j-lim", task_id=f"tl{i}")
    assert state.count_dlq() == 5

    result = state.purge_dlq_bulk(limit=2)
    assert result["purged"] == 2
    assert result["limit"] == 2
    assert result["remaining"] == 3
    assert state.count_dlq() == 3


def test_purge_dlq_bulk_empty(tmp_path: Path) -> None:
    state = _state(tmp_path)
    result = state.purge_dlq_bulk()
    assert result == {
        "ok": True,
        "purged": 0,
        "job_id": None,
        "limit": None,
        "remaining": 0,
    }


def test_cli_dlq_purge_bulk(tmp_project: Path) -> None:
    from awareness.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    state = StateDB(settings.state_db_url)
    state.init()
    _seed_dead(state, job_id="job-bulk", task_id="task-bulk-1")
    _seed_dead(state, job_id="job-bulk", task_id="task-bulk-2")

    res = runner.invoke(
        app, ["dlq", "purge-bulk", "--job-id", "job-bulk", "--yes", "--json"]
    )
    assert res.exit_code == 0, res.output
    import json

    payload = json.loads(res.output[res.output.find("{") :])
    assert payload["ok"] is True
    assert payload["purged"] == 2
    assert payload["remaining"] == 0
    assert state.count_dlq(job_id="job-bulk") == 0


def test_cli_dlq_purge_bulk_empty(tmp_project: Path) -> None:
    res = runner.invoke(app, ["dlq", "purge-bulk", "--yes", "--json"])
    assert res.exit_code == 0, res.output
    import json

    payload = json.loads(res.output[res.output.find("{") :])
    assert payload["purged"] == 0
