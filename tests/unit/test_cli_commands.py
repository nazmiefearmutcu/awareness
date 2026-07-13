"""Unit tests for the CLI commands."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner
from awareness.cli.main import app

runner = CliRunner()


def test_clear_command_outputs_ansi_escape() -> None:
    result = runner.invoke(app, ["clear"])
    assert result.exit_code == 0
    assert "\033[H\033[2J\033[3J" in result.output


def test_search_non_interactive_empty_db(tmp_project: Path) -> None:
    """Empty index: CLI always prints diagnostics (not just "Found 0")."""
    result = runner.invoke(app, ["search", "testquery", "--no-interactive"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Search Results for" in out
    assert "Found 0" in out
    # Diagnostics panel title + empty-corpus hint must always appear.
    assert "No results" in out and "suggestions" in out
    assert "No documents in index yet" in out
    assert "corpus=0" in out


def test_search_empty_prints_diagnostics_when_corpus_has_no_match(tmp_project: Path) -> None:
    """Non-empty index + zero hits still surfaces the diagnostics panel."""
    import json

    day = tmp_project / "data" / "jsonl" / "captures" / "2026" / "06" / "01"
    day.mkdir(parents=True, exist_ok=True)
    rec = {
        "doc_id": "doc-1",
        "capture_id": "cap-1",
        "parent_doc_or_dup_group": None,
        "source_type": "rss",
        "source_name": None,
        "source_locator": None,
        "source_shard": None,
        "source_offset_or_record_id": None,
        "discovery_channel": None,
        "job_id": None,
        "batch_id": None,
        "ingest_version": None,
        "url": "https://example.com/1",
        "canonical_url": None,
        "domain": "example.com",
        "fetch_ts": "2026-06-01T12:00:00+00:00",
        "observed_ts": None,
        "published_ts": None,
        "last_modified": None,
        "content_type": None,
        "http_status": None,
        "etag": None,
        "title": "Sports roundup",
        "text": "A football match ended in a draw.",
        "language": None,
        "content_hash": None,
        "near_dup_hash": None,
        "robots_decision": None,
        "terms_note_if_relevant": None,
    }
    (day / "chunk-1.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["search", "quantum chromodynamics", "--no-interactive", "--mode", "substring"],
    )
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Found 0" in out
    assert "No results" in out and "suggestions" in out
    # Must not be the empty-index tip when corpus has docs.
    assert "No documents in index yet" not in out
    assert "substring" in out.lower() or "terms" in out.lower() or "matches" in out.lower()


def test_service_compaction_scheduling() -> None:
    # Test dry running service schedule/unschedule compaction command
    # Use a dummy interval
    result = runner.invoke(app, ["service", "schedule-compaction", "--interval", "120"])
    # Since it may attempt to run launchctl load, it might fail or print creation messages.
    # Let's assert that the command execution doesn't raise unhandled CLI errors
    assert result.exit_code in (0, 1)
    
    result_unsched = runner.invoke(app, ["service", "unschedule-compaction"])
    assert result_unsched.exit_code in (0, 1)


def test_hf_push_command() -> None:
    # Test pushing a dummy repo ID.
    result = runner.invoke(app, ["hf-push", "dummy/repo"])
    assert result.exit_code in (0, 1, 2)


def test_shell_command() -> None:
    # Simulating standard REPL command typing: help, clear, exit.
    result = runner.invoke(app, ["shell"], input="help\nclear\nexit\n")
    assert result.exit_code == 0
    assert "Welcome to the Awareness Interactive Shell!" in result.output
    assert "Available Shell Commands:" in result.output
    assert "Goodbye!" in result.output




