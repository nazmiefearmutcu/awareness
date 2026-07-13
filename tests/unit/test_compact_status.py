"""compact --status: inspect pending staging manifests without compacting."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from awareness.cli.main import app
from awareness.storage.state import StateDB

runner = CliRunner()


def _state(tmp_path: Path) -> StateDB:
    db = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    db.init()
    return db


def test_pending_manifest_summary_empty(tmp_path: Path) -> None:
    state = _state(tmp_path)
    summary = state.pending_manifest_summary()
    assert summary["pending_count"] == 0
    assert summary["total_records"] == 0
    assert summary["total_bytes"] == 0
    assert summary["manifests"] == []


def test_pending_manifest_summary_aggregates(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.add_manifest("/data/a.jsonl.gz", records=10, bytes_=1000)
    state.add_manifest("/data/b.jsonl.gz", records=5, bytes_=500)
    # Compact one — should drop out of pending.
    pending = state.list_pending_manifests()
    assert len(pending) == 2
    state.mark_manifest_compacted(pending[0]["id"])

    summary = state.pending_manifest_summary()
    assert summary["pending_count"] == 1
    assert summary["total_records"] == 5
    assert summary["total_bytes"] == 500
    assert len(summary["manifests"]) == 1
    row = summary["manifests"][0]
    assert row["path"] == "/data/b.jsonl.gz"
    assert "committed_at" in row


def test_list_pending_includes_committed_at(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.add_manifest("/data/c.jsonl.gz", records=1, bytes_=10)
    rows = state.list_pending_manifests()
    assert len(rows) == 1
    assert rows[0].get("committed_at") is not None


def test_cli_compact_status_json(tmp_path: Path, monkeypatch) -> None:
    state = _state(tmp_path)
    state.add_manifest(str(tmp_path / "chunk.jsonl.gz"), records=3, bytes_=300)

    monkeypatch.setenv("AWARENESS_STATE_DB", f"sqlite:///{tmp_path / 'state.db'}")
    # Point bootstrap at our temp state via settings override when possible.
    from awareness.config import settings as settings_mod

    def _settings():
        s = settings_mod.Settings()
        s.state_db_url = f"sqlite:///{tmp_path / 'state.db'}"
        return s

    monkeypatch.setattr("awareness.cli.main.get_settings", _settings)
    monkeypatch.setattr(
        "awareness.cli.main._bootstrap",
        lambda: (state, None),
    )

    result = runner.invoke(app, ["compact", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["pending_count"] == 1
    assert data["total_records"] == 3
    assert data["total_bytes"] == 300


def test_cli_compact_status_table(tmp_path: Path, monkeypatch) -> None:
    state = _state(tmp_path)
    state.add_manifest("/tmp/x.jsonl.gz", records=2, bytes_=200)
    monkeypatch.setattr(
        "awareness.cli.main._bootstrap",
        lambda: (state, None),
    )
    result = runner.invoke(app, ["compact", "--status"])
    assert result.exit_code == 0, result.output
    assert "pending compaction" in result.output.lower() or "manifest" in result.output.lower()
    assert "2" in result.output
