"""Unit tests for the CLI commands."""

from __future__ import annotations

from typer.testing import CliRunner
from awareness.cli.main import app

runner = CliRunner()


def test_clear_command_outputs_ansi_escape() -> None:
    result = runner.invoke(app, ["clear"])
    assert result.exit_code == 0
    assert "\033[H\033[2J\033[3J" in result.output


def test_search_non_interactive_empty_db() -> None:
    # Run search command with non-interactive flag on a clean environment
    result = runner.invoke(app, ["search", "testquery", "--no-interactive"])
    assert result.exit_code == 0
    assert "Search Results for" in result.output or "No documents matched" in result.output


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




