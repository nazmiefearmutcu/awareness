"""CLI tests for the ``awareness x export`` command."""

from __future__ import annotations

import csv
import re
from pathlib import Path

from typer.testing import CliRunner

from awareness.cli.main import app

runner = CliRunner()

_HEADER = ["tweet_id", "created_at", "username", "text", "likes", "retweets", "lang", "source"]


def _create_and_simulate(n_tweets: int = 12) -> str:
    """Create a session via the CLI and simulate *n_tweets*; return its id."""
    created = runner.invoke(app, ["x", "create", "--title", "export watch", "--keywords", "bitcoin"])
    assert created.exit_code == 0, created.output
    match = re.search(r"id=([0-9a-f]{32})", created.output)
    assert match is not None, created.output
    session_id = match.group(1)
    simulated = runner.invoke(app, ["x", "simulate", session_id, "--count", str(n_tweets), "--seed", "5"])
    assert simulated.exit_code == 0, simulated.output
    return session_id


def test_x_export_writes_default_csv(tmp_project: Path) -> None:
    session_id = _create_and_simulate()
    result = runner.invoke(app, ["x", "export", session_id])
    assert result.exit_code == 0, result.output
    assert "Wrote 12 rows to " in result.output
    out = tmp_project / "data" / f"x_export_{session_id}.csv"
    assert out.exists()
    with out.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == _HEADER
    assert len(rows) == 13
    assert "bitcoin" in rows[1][3]


def test_x_export_custom_out_and_limit(tmp_project: Path) -> None:
    session_id = _create_and_simulate(30)
    custom = tmp_project / "custom" / "tweets.csv"
    result = runner.invoke(app, ["x", "export", session_id, "--out", str(custom), "--limit", "5"])
    assert result.exit_code == 0, result.output
    assert "Wrote 5 rows to " in result.output
    with custom.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    assert len(rows) == 6  # header + 5 rows


def test_x_export_unknown_session_fails(tmp_project: Path) -> None:
    result = runner.invoke(app, ["x", "export", "does-not-exist"])
    assert result.exit_code == 2
    assert "not found" in result.output
