"""CLI tests for ``awareness briefing --save`` / ``--list-saved``.

Saves the briefing JSON under ``{data_dir}/briefings/`` (daily file, or with
an optional ``[NAME]`` suffix) and lists saved files newest-first. Same tiny
multi-day corpus and ``--no-gdelt`` pattern as ``test_cli_briefing``; one
test stubs the GDELT bridge so the non-skipped path never touches the
network.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from awareness.cli.main import app
from awareness.config import get_settings
from awareness.gdeltx.engine import GdeltBridge
from tests.unit.test_cli_briefing import _corpus

runner = CliRunner()


def _briefings_dir() -> Path:
    settings = get_settings()
    assert settings.data_dir is not None
    return settings.data_dir / "briefings"


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def test_briefing_save_writes_daily_json(tmp_project: Path) -> None:
    _corpus(tmp_project)

    result = runner.invoke(app, ["briefing", "--days", "3", "--no-gdelt", "--save"])
    assert result.exit_code == 0, result.output

    path = _briefings_dir() / f"{_today()}.json"
    assert path.exists()
    assert "Briefing saved to" in result.output
    assert f"{_today()}.json" in result.output  # the path is printed

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert "movers" in payload
    assert "top_terms" in payload
    assert payload["top_terms"][0]["term"] == "bitcoin"
    assert {"domain": "spike.news", "count": 10} in payload["new_domains"]
    assert payload["gdelt_gaps"]["skipped"] is True


def test_briefing_save_named_suffix(tmp_project: Path) -> None:
    _corpus(tmp_project)

    result = runner.invoke(app, ["briefing", "--days", "3", "--no-gdelt", "--save", "weekly"])
    assert result.exit_code == 0, result.output

    path = _briefings_dir() / f"{_today()}-weekly.json"
    assert path.exists()
    assert f"{_today()}-weekly.json" in result.output
    # The plain daily file must NOT be created by a named save.
    assert not (_briefings_dir() / f"{_today()}.json").exists()


def test_briefing_save_with_json_mode(tmp_project: Path) -> None:
    """cron runs ``briefing --save --json``: file written AND stdout JSON."""
    _corpus(tmp_project)

    result = runner.invoke(app, ["briefing", "--days", "3", "--no-gdelt", "--save", "--json"])
    assert result.exit_code == 0, result.output

    path = _briefings_dir() / f"{_today()}.json"
    assert path.exists()
    assert f"{_today()}.json" in result.stderr  # confirmation off the JSON stream
    stdout = json.loads(result.stdout)  # stdout still a single JSON object
    assert stdout["days"] == 3
    assert stdout["top_terms"][0]["term"] == "bitcoin"
    assert json.loads(path.read_text(encoding="utf-8")) == stdout


def test_briefing_save_with_gdelt_mocked(tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _corpus(tmp_project)
    monkeypatch.setattr(GdeltBridge, "coverage_gap", lambda self, terms, window_days=7: [])

    result = runner.invoke(app, ["briefing", "--days", "3", "--save"])
    assert result.exit_code == 0, result.output

    path = _briefings_dir() / f"{_today()}.json"
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["gdelt_gaps"]["skipped"] is False
    assert payload["gdelt_gaps"]["gaps"] == []


def test_briefing_list_saved(tmp_project: Path) -> None:
    _corpus(tmp_project)

    result = runner.invoke(app, ["briefing", "--days", "3", "--no-gdelt", "--save"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["briefing", "--days", "3", "--no-gdelt", "--save", "weekly"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["briefing", "--list-saved"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Saved briefings" in out
    # The table lists path stems (no .json suffix); both files present.
    assert f"{_today()}" in out
    assert f"{_today()}-weekly" in out
    assert "KB" in out
    # Terms column: the tiny corpus has 5 distinct terms in the top-8 list.
    assert re.search(r"│\s+5\s+│", out)


def test_briefing_list_saved_empty(tmp_project: Path) -> None:
    result = runner.invoke(app, ["briefing", "--list-saved"])
    assert result.exit_code == 0, result.output
    assert "no saved briefings yet" in result.output


def test_briefing_save_empty_corpus_no_file(tmp_project: Path) -> None:
    result = runner.invoke(app, ["briefing", "--no-gdelt", "--save"])
    assert result.exit_code == 0, result.output
    assert "no corpus yet" in result.output
    briefings_dir = _briefings_dir()
    if briefings_dir.exists():
        assert list(briefings_dir.glob("*.json")) == []
