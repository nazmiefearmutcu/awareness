"""Tests for the turquoise terminal UX: outline wordmark, getting-started intro,
power-on self-test, --version, the command map, and the interactive shell."""

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
def test_big_banner_lines_are_outline_art() -> None:
    # Every glyph row is the same width; the wordmark is a fill-less outline
    # (box-drawing strokes), NOT a solid-block shadowed art.
    widths = {len(line) for line in banner._BIG_LINES}
    assert len(widths) == 1, "all banner rows must share one width"
    joined = "".join(banner._BIG_LINES)
    assert "|" in joined and "_" in joined
    assert "█" not in joined, "the turquoise theme uses an outline wordmark, no fill"


def test_render_banner_picks_tier_by_width() -> None:
    big = _render(banner.render_banner(width=120))
    assert "|_____|" in big and "█" not in big  # big outline wordmark
    compact = _render(banner.render_banner(width=60))
    assert "[__" in compact and "█" not in compact  # compact wordmark (cybermedium 'SS')
    tiny = _render(banner.render_banner(width=20))
    assert "AWARENESS" in tiny  # plain wordmark for very narrow terminals


def test_intro_has_wordmark_tagline_boot_and_getting_started() -> None:
    out = _render(banner.render_intro({"initialized": False, "version": "0.1.0"}))
    assert "|_____|" in out  # outline wordmark
    assert "Ambient capture & ingestion engine" in out  # design tagline
    assert "v0.1.0" in out  # real version threaded through
    assert "SELF TEST" in out.upper() and "READY" in out  # boot self-test + load bar
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


# ── turquoise theme — palette + boot self-test + load bar ─────────────────────
def test_theme_exposes_seven_semantic_tokens() -> None:
    # The Rich theme the CLI Console + Typer help consume, sourced from the spec.
    styles = banner.AWARENESS_THEME.styles
    for slot in ("aw.fg", "aw.hi", "aw.dim", "aw.faint", "aw.line"):
        assert slot in styles
    # Typer/rich help slots so commands, options and borders match the spec.
    for slot in ("command", "option", "switch", "metavar", "usage", "panel.border"):
        assert slot in styles
    # The canonical turquoise primary, hex-pinned to the design.
    assert banner.C_FG == "#6cf9f2"
    assert banner.C_LINE == "#379590"


def test_boot_sequence_reflects_live_state() -> None:
    idle = _render(banner.boot_sequence({"initialized": False, "api_running": False, "tail_running": False}))
    assert "POWER-ON SELF TEST" in idle.upper()
    assert "STANDBY" in idle  # api + tail idle
    assert "NEW" in idle  # storage not initialised yet
    live = _render(banner.boot_sequence({"initialized": True, "api_running": True, "tail_running": True}))
    assert "LIVE" in live  # tail running
    assert "NEW" not in live  # initialised → state.db attaches OK


def test_ready_bar_renders_percent_and_fill() -> None:
    out = _render(banner.ready_bar(100))
    assert "READY" in out and "100%" in out
    assert "█" in out and "│" in out  # dashed turquoise fill inside a thin frame


def test_command_map_uses_no_legacy_colors() -> None:
    # The map renders (categories + commands) without raising under the theme.
    out = _render(banner.render_command_map(), width=100)
    assert "▸" in out and "Service & lifecycle" in out


# ── framed boot card + emoji-free, professional getting-started ───────────────
def test_intro_has_no_emoji() -> None:
    # The launch screen is a professional operator console — no emoji / dingbats.
    out = _render(banner.render_intro({"initialized": False, "version": "0.1.0"}), width=120)
    for glyph in ("🚀", "✦", "⛁", "✅", "📦", "🎉", "🔧"):
        assert glyph not in out, f"intro must not contain {glyph!r}"


def test_getting_started_title_is_plain_text() -> None:
    out = _render(banner.getting_started_panel({"initialized": False}), width=120)
    assert "Getting started" in out
    assert "🚀" not in out


def test_intro_frames_logo_with_ready_bar_at_bottom() -> None:
    # One rounded card holds the logo at the top and the READY load bar flush at
    # the bottom (copyright rides the bottom border), all above Getting started.
    out = _render(
        banner.render_intro({"initialized": False, "version": "0.1.0", "api_port": 8085}),
        width=120,
    )
    assert "╭" in out and "╰" in out  # rounded frame corners
    i_logo = out.index("|_____|")
    i_selftest = out.lower().index("self test")
    i_ready = out.index("READY")
    i_copy = out.upper().index("SM-LINK DATA SYSTEMS")
    i_start = out.index("Getting started")
    # logo (top) → self-test (middle) → READY bar (bottom) → copyright (bottom
    # border) → the separate Getting started card. This locks the layout so a
    # refactor that reordered boot_sequence/ready_bar inside the frame would fail.
    assert i_logo < i_selftest < i_ready < i_copy < i_start


def test_boot_panel_is_a_single_bordered_frame() -> None:
    out = _render(banner.boot_panel({"initialized": False, "version": "0.1.0"}), width=120)
    assert "|_____|" in out  # wordmark inside
    assert "READY" in out and "│" in out and "█" in out  # load bar inside
    assert "SM-LINK DATA SYSTEMS" in out.upper()  # copyright on the border


def test_ready_bar_spans_requested_width() -> None:
    narrow = _render(banner.ready_bar(100, 30))
    wide = _render(banner.ready_bar(100, 80))
    # a wider request yields a wider load bar (more fill blocks)
    assert wide.count("█") > narrow.count("█")
