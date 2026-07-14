"""DLQ list/count storage + CLI empty-state coverage."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from awareness.cli.main import app
from awareness.storage.state import StateDB

runner = CliRunner()


def _state(tmp_path: Path) -> StateDB:
    db = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    db.init()
    return db


def test_list_dlq_empty(tmp_path: Path) -> None:
    state = _state(tmp_path)
    assert state.count_dlq() == 0
    assert state.list_dlq() == []


def test_list_dlq_newest_first_and_filter(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.add_dlq("job-a", "task-1", {"url": "https://a.example/1"}, "boom-a")
    state.add_dlq("job-b", "task-2", {"url": "https://b.example/2"}, "boom-b")
    state.add_dlq("job-a", "task-3", {"partition_key": "rss:x"}, "boom-a2")

    assert state.count_dlq() == 3
    assert state.count_dlq(job_id="job-a") == 2

    all_rows = state.list_dlq(limit=10)
    assert len(all_rows) == 3
    # Newest first (highest id / later insert).
    assert [r["task_id"] for r in all_rows] == ["task-3", "task-2", "task-1"]
    assert all_rows[0]["payload"]["partition_key"] == "rss:x"
    assert all_rows[1]["error"] == "boom-b"
    assert all_rows[0]["created_at"] is not None

    only_a = state.list_dlq(job_id="job-a")
    assert {r["task_id"] for r in only_a} == {"task-1", "task-3"}

    page = state.list_dlq(limit=1, offset=1)
    assert len(page) == 1
    assert page[0]["task_id"] == "task-2"


def test_list_dlq_corrupt_payload_json(tmp_path: Path) -> None:
    """Corrupt payload_json should not raise; surfaces under payload._raw."""
    from awareness.storage.state import DLQRow  # noqa: PLC0415

    state = _state(tmp_path)
    with state.session() as s:
        s.add(
            DLQRow(
                job_id="j",
                task_id="t",
                payload_json="not-json{",
                error="x",
            )
        )
        s.commit()
    rows = state.list_dlq()
    assert len(rows) == 1
    assert rows[0]["payload"]["_raw"] == "not-json{"


def test_cli_dlq_list_empty(tmp_project: Path) -> None:
    result = runner.invoke(app, ["dlq", "list"])
    assert result.exit_code == 0, result.output
    assert "empty" in result.output.lower()


def test_cli_dlq_list_json_empty(tmp_project: Path) -> None:
    result = runner.invoke(app, ["dlq", "list", "--json"])
    assert result.exit_code == 0, result.output
    assert '"total": 0' in result.output
    assert '"items": []' in result.output


def test_cli_dlq_list_and_count(tmp_project: Path) -> None:
    from awareness.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    state = StateDB(settings.state_db_url)
    state.init()
    state.add_dlq(
        "job-cli",
        "task-cli",
        {"url": "https://example.com/x", "source_type": "rss"},
        "fetch failed permanently",
    )

    listed = runner.invoke(app, ["dlq", "list", "--limit", "5"])
    assert listed.exit_code == 0, listed.output
    assert "Dead-letter queue" in listed.output
    assert "task-cli" in listed.output

    counted = runner.invoke(app, ["dlq", "count"])
    assert counted.exit_code == 0, counted.output
    assert "1" in counted.output

    as_json = runner.invoke(app, ["dlq", "list", "--json", "--job-id", "job-cli"])
    assert as_json.exit_code == 0, as_json.output
    assert "task-cli" in as_json.output
    assert "fetch failed permanently" in as_json.output
    assert "example.com" in as_json.output
    assert '"total": 1' in as_json.output
