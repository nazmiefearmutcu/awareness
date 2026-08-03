"""M-03: unparseable ``--end`` must fail cleanly, never traceback.

Covers ``backfill submit``, ``inspect``, ``counts``, ``browse`` and
``search`` — each must raise ``typer.BadParameter`` (exit code 2) instead of
letting a raw ValueError crash the CLI.
"""

from __future__ import annotations

from typer.testing import CliRunner

from awareness.cli.main import app

runner = CliRunner()


def test_backfill_submit_bad_end_clean_error(tmp_project: Path) -> None:
    result = runner.invoke(
        app,
        ["backfill", "submit", "--start", "2026-06-01", "--end", "foo"],
    )
    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output


def test_inspect_bad_end_clean_error(tmp_project: Path) -> None:
    result = runner.invoke(
        app,
        ["inspect", "--start", "2026-06-01", "--end", "not-a-date"],
    )
    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output


def test_counts_bad_end_clean_error(tmp_project: Path) -> None:
    result = runner.invoke(
        app,
        ["counts", "--start", "2026-06-01", "--end", "not-a-date"],
    )
    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output


def test_browse_bad_end_clean_error(tmp_project: Path) -> None:
    result = runner.invoke(
        app,
        ["browse", "--start", "2026-06-01", "--end", "not-a-date"],
    )
    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output


def test_search_bad_end_clean_error(tmp_project: Path) -> None:
    result = runner.invoke(
        app,
        ["search", "testquery", "--no-interactive", "--end", "not-a-date"],
    )
    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output
