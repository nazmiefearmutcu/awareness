"""Tests for the enhanced terminal UX: big shadowed banner, getting-started
intro, --version, the command map, and the full-dispatch interactive shell."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from typer.testing import CliRunner

from awareness.cli import banner
from awareness.cli.main import app

runner = CliRunner()


def _render(renderable: object, width: int = 100) -> str:
    console = Console(record=True, width=width)
    console.print(renderable)
    return console.export_text()


# ── banner module (pure rendering) ───────────────────────────────────────────
def test_big_banner_lines_are_block_art() -> None:
    # Every glyph row is the same width and made of block + box-drawing chars.
    widths = {len(line) for line in banner._BIG_LINES}
    assert len(widths) == 1, "all banner rows must share one width"
    assert "█" in "".join(banner._BIG_LINES)


def test_render_banner_picks_tier_by_width() -> None:
    assert "█" in _render(banner.render_banner(width=120))  # big shadowed wordmark
    small = _render(banner.render_banner(width=60))
    assert "█" not in small and "_" in small  # compact ASCII fallback
    tiny = _render(banner.render_banner(width=20))
    assert "AWARENESS" in tiny  # plain wordmark for very narrow terminals


def test_intro_has_wordmark_tagline_and_getting_started() -> None:
    out = _render(banner.render_intro({"initialized": False}))
    assert "█" in out
    assert "public text internet awareness engine" in out
    assert "Getting started" in out
    assert "awareness shell" in out


def test_headline_adapts_to_context() -> None:
    assert "First run" in _render(banner.getting_started_panel({"initialized": False}))
    ready = _render(banner.getting_started_panel({"initialized": True, "jobs": 0}))
    assert "ready" in ready.lower()
    busy = _render(banner.getting_started_panel({"initialized": True, "jobs": 3, "docs": 42}))
    assert "42" in busy and "3" in busy


def test_status_chips_reflect_state() -> None:
    out = _render(banner.status_chips({"api_running": True, "tail_running": False, "cloud": True}))
    assert "API" in out and "TAIL" in out and "cloud" in out


def test_command_map_lists_every_category() -> None:
    out = _render(banner.render_command_map())
    for title, _rows in banner.COMMAND_CATEGORIES:
        assert title in out
    # a few representative commands must be present
    for cmd in ("backfill submit", "tail start", "search", "shell", "compact"):
        assert cmd in out


# ── CLI surface ──────────────────────────────────────────────────────────────
def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "awareness v" in result.output


def test_no_arg_shows_getting_started(tmp_project: Path) -> None:
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "Getting started" in result.output
    assert "First run" in result.output  # fresh tmp project → not initialised


def test_commands_map_command(tmp_project: Path) -> None:
    result = runner.invoke(app, ["commands"])
    assert result.exit_code == 0
    assert "Service & lifecycle" in result.output
    assert "Explore your corpus" in result.output


# ── interactive shell dispatches the WHOLE CLI ───────────────────────────────
def test_shell_dispatches_real_command(tmp_project: Path) -> None:
    # Typing `health` inside the shell must actually run the health command.
    result = runner.invoke(app, ["shell"], input="health\nexit\n")
    assert result.exit_code == 0
    assert "Welcome to the Awareness Interactive Shell!" in result.output
    assert '"ok": true' in result.output  # health JSON proves real dispatch
    assert "Goodbye!" in result.output


def test_shell_help_for_subcommand(tmp_project: Path) -> None:
    result = runner.invoke(app, ["shell"], input="help search\nexit\n")
    assert result.exit_code == 0
    # `search --help` shows the command's own usage/description.
    assert "Search" in result.output or "Usage" in result.output


def test_shell_survives_unknown_command(tmp_project: Path) -> None:
    result = runner.invoke(app, ["shell"], input="definitelynotacommand\nexit\n")
    assert result.exit_code == 0  # the REPL must not crash …
    assert "Goodbye!" in result.output  # … and still reaches a clean exit


def test_shell_slash_quit(tmp_project: Path) -> None:
    result = runner.invoke(app, ["shell"], input="/quit\n")
    assert result.exit_code == 0
    assert "Goodbye!" in result.output


def test_shell_commands_alias_renders_map(tmp_project: Path) -> None:
    result = runner.invoke(app, ["shell"], input="commands\nexit\n")
    assert result.exit_code == 0
    assert "Service & lifecycle" in result.output


def test_shell_does_not_re_enter_itself(tmp_project: Path) -> None:
    # Typing `shell` inside the shell must NOT spawn a nested REPL.
    result = runner.invoke(app, ["shell"], input="shell\nexit\n")
    assert result.exit_code == 0
    assert result.output.count("Welcome to the Awareness Interactive Shell!") == 1
    assert "Already in the Awareness shell" in result.output


def test_headline_pluralisation_is_grammatical() -> None:
    single = _render(banner.getting_started_panel({"initialized": True, "jobs": 1, "docs": 1}))
    assert "1 document captured" in single
    assert "1 run." in single
    assert "documents" not in single and "run(s)" not in single
    plural = _render(banner.getting_started_panel({"initialized": True, "jobs": 3, "docs": 9}))
    assert "9 documents captured" in plural and "3 runs." in plural


def test_small_banner_rows_share_one_width() -> None:
    rows = [line for line in banner.SMALL_BANNER.splitlines() if line.strip()]
    assert rows, "small banner must have content"
    assert len({len(line) for line in rows}) == 1, "compact wordmark rows must align"
