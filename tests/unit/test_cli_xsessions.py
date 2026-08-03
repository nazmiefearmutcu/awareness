"""CLI tests for the ``awareness x`` session group (create / sessions / show).

The SessionStore is aiosqlite-backed at ``{data_dir}/xscraper.sqlite`` (same
path the consume/xrouter API uses); the CLI opens its own connection per
invocation, so CliRunner tests can drive create → list → show in sequence.
"""

from __future__ import annotations

import re
from pathlib import Path

from typer.testing import CliRunner

from awareness.cli.main import app

runner = CliRunner()


def _create_session() -> str:
    """Create one session and return its session id."""
    result = runner.invoke(
        app,
        [
            "x", "create",
            "--title", "btc watch",
            "--keywords", "bitcoin,eth",
            "--accounts", "vitalikbuterin",
            "--lookback", "2h",
            "--language", "en",
        ],
    )
    assert result.exit_code == 0, result.output
    match = re.search(r"id=([0-9a-f]{32})", result.output)
    assert match is not None, result.output
    return match.group(1)


def test_x_create_prints_id_and_query(tmp_project: Path) -> None:
    result = runner.invoke(
        app,
        ["x", "create", "--title", "btc watch", "--keywords", "bitcoin,eth"],
    )
    assert result.exit_code == 0, result.output
    assert "session created" in result.output
    assert re.search(r"id=[0-9a-f]{32}", result.output) is not None
    assert "query: " in result.output
    assert "(bitcoin OR eth)" in result.output


def test_x_sessions_lists_created_session(tmp_project: Path) -> None:
    _create_session()
    result = runner.invoke(app, ["x", "sessions"])
    assert result.exit_code == 0, result.output
    assert "X scraper sessions" in result.output
    assert "btc watch" in result.output
    assert "queued" in result.output


def test_x_show_prints_summary_and_tweets_table(tmp_project: Path) -> None:
    session_id = _create_session()
    result = runner.invoke(app, ["x", "show", session_id])
    assert result.exit_code == 0, result.output
    assert session_id in result.output
    assert "status:" in result.output
    assert "query:" in result.output
    assert "Tweets" in result.output
    assert "Username" in result.output  # empty tweet table still renders headers


def test_x_create_rejects_empty_keywords(tmp_project: Path) -> None:
    result = runner.invoke(app, ["x", "create", "--title", "bad", "--keywords", ""])
    assert result.exit_code != 0
    assert "keyword" in result.output


def test_x_show_unknown_session_fails(tmp_project: Path) -> None:
    result = runner.invoke(app, ["x", "show", "does-not-exist"])
    assert result.exit_code != 0
    assert "not found" in result.output
