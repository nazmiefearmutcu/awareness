"""CLI ``awareness saved`` flows: add / list / run / rm against a tiny corpus."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from awareness.cli.main import app

runner = CliRunner()


@pytest.fixture()
def wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the shared CLI console room so rich tables do not truncate cells."""
    monkeypatch.setattr(
        "awareness.cli.main.console", Console(width=180, force_terminal=False)
    )

_FULL_KEYS = (
    "doc_id", "capture_id", "parent_doc_or_dup_group", "source_type",
    "source_name", "source_locator", "source_shard",
    "source_offset_or_record_id", "discovery_channel", "job_id", "batch_id",
    "ingest_version", "url", "canonical_url", "domain", "fetch_ts",
    "observed_ts", "published_ts", "last_modified", "content_type",
    "http_status", "etag", "title", "text", "language", "content_hash",
    "near_dup_hash", "robots_decision", "terms_note_if_relevant",
)


def _write_doc(
    root: Path,
    idx: int,
    *,
    title: str,
    text: str,
    domain: str,
    source_type: str = "rss",
    language: str | None = "en",
) -> None:
    day = root / "data" / "jsonl" / "captures" / "2026" / "06" / "01"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}",
        capture_id=f"cap-{idx}",
        source_type=source_type,
        domain=domain,
        url=f"https://{domain}/{idx}",
        fetch_ts="2026-06-01T12:00:00+00:00",
        title=title,
        text=text,
        language=language,
    )
    (day / f"chunk-{idx}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def test_saved_add_and_list(tmp_project: Path, wide_console: None) -> None:
    result = runner.invoke(
        app,
        ["saved", "add", "alpha watch", "alpha", "--mode", "substring", "--limit", "5"],
    )
    assert result.exit_code == 0, result.output
    assert "Saved search" in result.output
    assert "alpha watch" in result.output

    listed = runner.invoke(app, ["saved", "list"])
    assert listed.exit_code == 0, listed.output
    assert "alpha watch" in listed.output
    assert "substring" in listed.output
    assert "5" in listed.output


def test_saved_list_empty(tmp_project: Path) -> None:
    result = runner.invoke(app, ["saved", "list"])
    assert result.exit_code == 0
    assert "No saved searches." in result.output

def test_saved_add_validation_failure(tmp_project: Path) -> None:
    result = runner.invoke(app, ["saved", "add", "", "alpha"])
    assert result.exit_code == 2
    assert "invalid saved search" in result.output


def test_saved_run_with_corpus(tmp_project: Path) -> None:
    _write_doc(tmp_project, 1, title="Alpha one", text="alpha news", domain="a.example")
    _write_doc(tmp_project, 2, title="Alpha two", text="alpha again", domain="b.example")
    added = runner.invoke(
        app, ["saved", "add", "alpha run", "alpha", "--mode", "substring"]
    )
    assert added.exit_code == 0, added.output
    saved_id = added.output.rsplit("(", 1)[1].split(")", 1)[0]

    result = runner.invoke(app, ["saved", "run", saved_id, "--limit", "10"])
    assert result.exit_code == 0, result.output
    assert "Found 2 documents" in result.output
    assert "Alpha one" in result.output
    assert "Alpha two" in result.output
    assert "a.example" in result.output


def test_saved_run_unknown_id(tmp_project: Path) -> None:
    result = runner.invoke(app, ["saved", "run", "ghost-id"])
    assert result.exit_code == 2
    assert "No saved search with id" in result.output


def test_saved_rm_roundtrip(tmp_project: Path) -> None:
    added = runner.invoke(app, ["saved", "add", "doomed", "doomed"])
    assert added.exit_code == 0, added.output
    saved_id = added.output.rsplit("(", 1)[1].split(")", 1)[0]

    assert runner.invoke(app, ["saved", "rm", saved_id]).exit_code == 0
    assert runner.invoke(app, ["saved", "rm", saved_id]).exit_code == 2
    listed = runner.invoke(app, ["saved", "list"])
    assert "doomed" not in listed.output


def test_saved_store_lives_in_data_dir(tmp_project: Path) -> None:
    runner.invoke(app, ["saved", "add", "persist me", "persist"])
    assert (tmp_project / "data" / "saved_searches.db").exists()
