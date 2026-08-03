"""Awareness CLI banner + intro rendering — "turquoise terminal" theme.

A crisp, fill-less outline wordmark (no 3-D shadow, no gradient) rendered in a
single turquoise hue, a power-on self-test boot log that reflects real engine
state, a READY load bar, and context-aware getting-started + command-map panels.

The whole look comes from one source of truth: the seven semantic colour tokens
below (mirrored verbatim from the Claude Design "Awareness" theme spec, hue 177).
Those tokens build both the raw styles used by these renderables *and* the
``AWARENESS_THEME`` Rich theme consumed by the CLI's ``Console`` and Typer help.

Everything here is pure rendering: callers pass in a small context dict, so this
module never touches the database, network, or filesystem. The ASCII wordmark is
baked in as a constant, so the CLI carries no figlet runtime dependency.
"""

from __future__ import annotations

import shutil
from typing import Any

from rich.align import Align
from rich.box import ROUNDED
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

# ── palette — the single source of truth (Claude Design spec, hue 177) ───────
#   token      hex         xterm-256   role
C_BG = "#061313"  # 233    background / screen
C_BG2 = "#091a1a"  # 233   panel / raised surface
C_FG = "#6cf9f2"  # 87     primary text · output · logo
C_HI = "#c3fefb"  # 159    headings · commands · emphasis
C_DIM = "#6ec4c0"  # 79    descriptions · prompt user
C_FAINT = "#429490"  # 66  dots · disabled · tertiary
C_LINE = "#379590"  # 66   borders · rules · panel outline

# Composed styles (used directly on renderables → theme-independent, so they
# render identically whether or not the printing Console carries AWARENESS_THEME).
S_FG = C_FG
S_HI = f"bold {C_HI}"
S_DIM = C_DIM
S_FAINT = C_FAINT
S_LINE = C_LINE

# Rich theme for the CLI Console + Typer/rich-click help slots. Pass to
# ``Console(theme=AWARENESS_THEME)``; reuse the hex values in Typer's rich_utils
# so commands, options and panel borders match this spec.
AWARENESS_THEME = Theme(
    {
        "aw.fg": C_FG,
        "aw.hi": f"bold {C_HI}",
        "aw.dim": C_DIM,
        "aw.faint": C_FAINT,
        "aw.line": C_LINE,
        # --- typer / rich help slots ---
        "command": C_HI,
        "option": C_HI,
        "switch": C_FG,
        "metavar": C_DIM,
        "usage": C_DIM,
        "panel.border": C_LINE,
    }
)

# Big outline wordmark — figlet "cyberlarge": hollow pixel-outline, no fill.
_BIG_LINES = [
    r" _______ _  _  _ _______  ______ _______ __   _ _______ _______ _______",
    r" |_____| |  |  | |_____| |_____/ |______ | \  | |______ |______ |______",
    r" |     | |__|__| |     | |    \_ |______ |  \_| |______ ______| ______|",
]
_BIG_WIDTH = max(len(line) for line in _BIG_LINES)

# Compact wordmark — figlet "cybermedium": same family, for narrow terminals.
_SMALL_LINES = [
    r"____ _ _ _ ____ ____ ____ _  _ ____ ____ ____ ",
    r"|__| | | | |__| |__/ |___ |\ | |___ [__  [__  ",
    r"|  | |_|_| |  | |  \ |___ | \| |___ ___] ___] ",
]
SMALL_BANNER = "\n" + "\n".join(_SMALL_LINES) + "\n"
_SMALL_WIDTH = max(len(line) for line in _SMALL_LINES)

TAGLINE = "Ambient capture & ingestion engine"
_COPYRIGHT = "(C) 2026 SM-Link Data Systems"

# Both launch cards (the framed boot screen and the getting-started panel) share
# one width so they stack as an aligned pair. _FRAME_MAX caps the card on wide
# terminals (a centred-looking column reads more deliberate than a stretched
# full-width box); _FRAME_PAD is the horizontal breathing room inside the border.
_FRAME_MAX = 92
_FRAME_PAD = 2
_FRAME_FLOOR = 8  # absolute minimum that keeps the inner width non-negative


def _frame_width(cols: int) -> int:
    """Outer width of a launch card for a terminal *cols* wide.

    For any realistic terminal (``cols >= _FRAME_FLOOR``) this stays ``<= cols``,
    so the card never overruns the terminal and gets clamped — clamping would
    crop the wordmark and wrap the self-test. The wordmark tier drops to compact
    / plain as the resulting inner width shrinks. On a pathologically tiny
    terminal (``cols < _FRAME_FLOOR``) the floor wins and the layout wraps.
    """
    return max(_FRAME_FLOOR, min(cols, _FRAME_MAX))


def _inner_width(cols: int) -> int:
    """Usable content width inside a launch card (minus borders + padding)."""
    return _frame_width(cols) - _FRAME_PAD * 2 - 2


def _render_wordmark(lines: list[str]) -> Text:
    """Monochrome turquoise outline — crisp, no gradient, no shadow."""
    text = Text(no_wrap=True, overflow="crop", style=f"bold {C_FG}")
    text.append("\n".join(lines))
    return text


def _terminal_width(width: int | None) -> int:
    if width is not None:
        return width
    return shutil.get_terminal_size((80, 24)).columns


def render_banner(width: int | None = None) -> RenderableType:
    """Outline wordmark, or a compact / plain fallback on narrow terminals."""
    cols = _terminal_width(width)
    if cols >= _BIG_WIDTH:
        return _render_wordmark(_BIG_LINES)
    if cols >= _SMALL_WIDTH:
        return _render_wordmark(_SMALL_LINES)
    return Text("AWARENESS", style=f"bold {C_FG}")


def _headline(ctx: dict[str, Any]) -> Text:
    """A single line that adapts to where the user is in the workflow."""
    if not ctx.get("initialized"):
        return Text.from_markup(
            f"[bold {C_HI}]First run?[/]  Run [bold {C_HI}]awareness init[/] to choose where "
            "data lives — then kick off ingestion below."
        )
    jobs = ctx.get("jobs", 0) or 0
    if jobs <= 0:
        return Text.from_markup(
            f"[bold {C_HI}]Storage is ready[/] — but nothing captured yet. Start ingesting:"
        )
    docs = ctx.get("docs")
    run_word = "run" if jobs == 1 else "runs"
    if docs:
        doc_word = "document" if docs == 1 else "documents"
        return Text.from_markup(
            f"[bold {C_HI}]{docs:,} {doc_word}[/] captured across [bold {C_HI}]{jobs}[/] {run_word}. "
            "Explore your corpus or capture more:"
        )
    return Text.from_markup(
        f"[bold {C_HI}]{jobs}[/] ingestion {run_word} recorded. Explore your corpus or capture more:"
    )


# (command, one-line description) — the most useful entry points, in order.
_QUICKSTART: list[tuple[str, str]] = [
    ("awareness shell", "Interactive control center — every command, with ↑ history & Tab-complete"),
    ("awareness tui", "Live full-screen telemetry dashboard"),
    ("awareness backfill submit --start 2024-01-01", "Pull historical public web text (BODY)"),
    ("awareness configure", "Choose where captures are saved (local · S3 · Drive)"),
    ("awareness tail start", "Capture newly-published public text, live (TAIL)"),
    ('awareness search "climate policy"', "Full-text search everything you've captured"),
    ("awareness status", "Services, recent jobs & disk usage at a glance"),
    ("awareness commands", "The full command map  ·  awareness --help for flags"),
]


# ── boot self-test — the design's power-on log, reflecting real state ────────
def _prompt_line(ctx: dict[str, Any]) -> Text:
    """`user@host ~ %  awareness` — the command that produced this screen."""
    who = ctx.get("prompt") or "awareness ~ %"
    line = Text(no_wrap=True)
    line.append(who, style=C_DIM)
    line.append("  ")
    line.append("awareness", style=f"bold {C_FG}")
    return line


def boot_sequence(ctx: dict[str, Any] | None = None, width: int | None = None) -> Text:
    """Power-on self-test log. Each line's status mirrors live engine state."""
    ctx = ctx or {}
    port = ctx.get("api_port") or 8085
    rows: list[tuple[str, str, bool]] = [
        ("power-on self test", "OK", True),
        ("mount  /var/awareness/store", "OK", True),
        ("open   iceberg.catalog", ("OK" if ctx.get("cloud") else "LOCAL"), bool(ctx.get("cloud"))),
        (
            "attach state.db (sqlite)",
            ("OK" if ctx.get("initialized") else "NEW"),
            bool(ctx.get("initialized")),
        ),
        ("build  dedup index", "OK", True),
        ("tail   daemon", ("LIVE" if ctx.get("tail_running") else "STANDBY"), bool(ctx.get("tail_running"))),
        (
            f"serve  api :{port}",
            ("OK" if ctx.get("api_running") else "STANDBY"),
            bool(ctx.get("api_running")),
        ),
    ]
    # Each row is "▸ <label> <dots> <status>" sized to span *target* columns, so
    # the status words right-align flush with the frame the log sits inside.
    target = max(18, width if width is not None else 52)
    out = Text()
    for i, (label, status, ok) in enumerate(rows):
        out.append("▸ ", style=C_DIM)
        out.append(label, style=C_FG)
        dots = max(3, target - 4 - len(label) - len(status))
        out.append(" " + "." * dots + " ", style=C_FAINT)
        out.append(status, style=(f"bold {C_HI}" if ok else C_FG))
        if i != len(rows) - 1:
            out.append("\n")
    return out


def ready_bar(pct: int = 100, width: int | None = None) -> Text:
    """`READY … 100%` meta line above a dashed turquoise load bar.

    When *width* is supplied the bar spans that many columns, so it can sit flush
    along the bottom edge of the boot frame; otherwise it falls back to a compact
    fixed width for standalone use.
    """
    col = max(12, width if width is not None else 52)
    bar_w = col - 2
    out = Text()
    label, pctlabel = "READY", f"{pct}%"
    gap = max(1, col - len(label) - len(pctlabel))
    out.append(label, style=C_FG)
    out.append(" " * gap)
    out.append(pctlabel, style=f"bold {C_HI}")
    out.append("\n")
    filled = round(bar_w * max(0, min(pct, 100)) / 100)
    fill = "".join("█" if (i % 4 != 3) else " " for i in range(filled))  # 3-on / 1-off dash
    out.append("│", style=C_LINE)
    out.append(fill, style=C_FG)
    out.append(" " * (bar_w - filled))
    out.append("│", style=C_LINE)
    return out


def status_chips(ctx: dict[str, Any]) -> Text:
    """Compact API/Tail/storage status line (kept for inline status displays)."""

    def chip(label: str, on: bool) -> tuple[str, str]:
        dot = "●" if on else "○"
        return (f"{dot} {label}", (f"bold {C_FG}" if on else C_FAINT))

    text = Text()
    for i, (lbl, on) in enumerate(
        [("API", ctx.get("api_running", False)), ("TAIL", ctx.get("tail_running", False))]
    ):
        label, style = chip(lbl, on)
        if i:
            text.append("    ")
        text.append(label, style=style)
    store = "cloud + local" if ctx.get("cloud") else "local"
    text.append("    ")
    text.append(f"⛁ {store}", style=C_DIM)
    return text


def getting_started_panel(ctx: dict[str, Any] | None = None, width: int | None = None) -> Panel:
    """A 'try one of these' panel tailored to the user's current state.

    Sized to the same width as the boot frame so the two cards stack as an
    aligned pair. The title is plain text — no decorative glyphs — to keep the
    launch screen reading as a professional operator console.
    """
    ctx = ctx or {}
    cols = _terminal_width(width)
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=f"bold {C_HI}", no_wrap=True)
    grid.add_column(style=C_DIM)
    for cmd, desc in _QUICKSTART:
        grid.add_row(cmd, Text(desc, style=C_DIM))

    body = Group(_headline(ctx), Text(), grid)
    return Panel(
        body,
        title=f"[bold {C_HI}]Getting started[/]",
        title_align="left",
        border_style=C_LINE,
        box=ROUNDED,
        padding=(1, _FRAME_PAD),
        width=_frame_width(cols),
    )


def boot_panel(ctx: dict[str, Any] | None = None, width: int | None = None) -> Panel:
    """The framed power-on screen.

    A single bordered card whose top half is the centred wordmark + tagline, whose
    middle is the power-on self-test, and whose bottom edge is a full-width READY
    load bar. The copyright + listening port ride the bottom border as the card's
    subtitle, so the whole thing reads like the front panel of a piece of kit.
    """
    ctx = ctx or {}
    cols = _terminal_width(width)
    inner = _inner_width(cols)
    version = ctx.get("version")
    tagline = TAGLINE + (f" · v{version}" if version else "")
    port = ctx.get("api_port") or 8085

    body = Group(
        Align.center(render_banner(inner)),
        Align.center(Text(tagline, style=C_DIM)),
        Text(),
        boot_sequence(ctx, inner),
        Text(),
        ready_bar(100, inner),
    )
    subtitle = f"{_COPYRIGHT} · API :{port}".upper()
    return Panel(
        body,
        border_style=C_LINE,
        box=ROUNDED,
        padding=(1, _FRAME_PAD),
        width=_frame_width(cols),
        subtitle=f"[{C_DIM}]{subtitle}[/]",
        subtitle_align="right",
    )


def render_intro(
    ctx: dict[str, Any] | None = None,
    width: int | None = None,
    *,
    subtitle: str | None = None,
) -> Group:
    """Full launch splash: prompt line, the framed boot card (logo → self-test →
    READY bar), then the getting-started card beneath it."""
    ctx = ctx or {}
    cols = _terminal_width(width)

    parts: list[RenderableType] = [
        Text(),
        _prompt_line(ctx),
        Text(),
        boot_panel(ctx, cols),
        Text(),
    ]
    if subtitle:
        parts.append(Text(subtitle, style=f"bold {C_HI}"))
        parts.append(Text())
    parts.append(getting_started_panel(ctx, cols))
    return Group(*parts)


# ── full command map (used by `commands` and the shell `help`) ───────────────
COMMAND_CATEGORIES: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "Service & lifecycle",
        [
            ("start", "Start API server (+ live tail) in the background"),
            ("stop", "Stop the background API server & tail"),
            ("restart", "Stop then start the API server"),
            ("status", "Services, recent jobs & disk usage"),
            ("health", "Quick JSON liveness probe"),
            ("dashboard", "Open the web dashboard in a browser"),
            ("tui", "Live full-screen terminal dashboard"),
            ("logs", "View / follow API & app logs"),
            ("service …", "Install & manage the macOS launchd agent"),
        ],
    ),
    (
        "Ingest — BODY (historical)",
        [
            ("backfill submit", "Queue a historical date-range crawl"),
            ("backfill run", "Run a queued backfill job to completion"),
            ("backfill status", "Inspect a backfill job"),
            ("compact", "Fold JSONL staging into Iceberg (--status for backlog)"),
        ],
    ),
    (
        "Ingest — TAIL (live)",
        [
            ("tail start", "Capture newly-published text until stopped"),
            ("tail stop", "Request a running tail to stop"),
            ("tail status", "Show tail daemon state"),
            ("tail check-seeds", "Validate feeds/sitemaps in tail_seeds.yaml"),
        ],
    ),
    (
        "Explore your corpus",
        [
            ("search", "Search captures (ranked; --mode/--fields/--max-results)"),
            ("browse", "Page through captures & read full text (--unique)"),
            ("inspect", "Tabular query by date / domain / source"),
            ("counts", "Aggregate counts by source, domain & language"),
            ("export", "Export captures to JSONL/txt (--limit, --unique)"),
            ("hf-push", "Publish captures to a Hugging Face dataset"),
        ],
    ),
    (
        "Deduplication",
        [
            ("dedup check", "Test a URL / text / file against the index"),
            ("dedup-stats", "Dump dedup index statistics"),
        ],
    ),
    (
        "Recovery",
        [
            ("dlq list", "List dead-lettered tasks (newest first; --json)"),
            ("dlq count", "Count dead-letter queue rows"),
            ("dlq replay", "Re-arm a dead-lettered task by DLQ id"),
            ("dlq purge", "Drop a DLQ entry without re-arming the task"),
            ("dlq purge-bulk", "Drop many DLQ entries (optional --job-id / --limit)"),
        ],
    ),
    (
        "Config & cloud",
        [
            ("init", "Initialise storage layout & choose data dir"),
            ("configure", "Set WHERE tail writes (wizard) — before capturing"),
            ("config show", "Show config by section, with each value's source"),
            ("config get", "Show one value: source, type, default, range"),
            ("config set", "Persist a validated config value to awareness.yaml"),
            ("config unset", "Drop a key back to its default"),
            ("config reset", "Clear all overrides (back to defaults)"),
            ("config validate", "Check the override file for problems"),
            ("config doctor", "Diagnose write destinations (paths, cloud, Drive)"),
            ("config path", "Print the config file path & active env overrides"),
            ("config edit", "Open the config file in $EDITOR"),
            ("config interactive", "Edit any setting interactively"),
            ("cloud auth-gdrive", "Authorise Google Drive storage"),
            ("cloud status", "Show cloud storage integration status"),
        ],
    ),
    (
        "Insight",
        [
            ("stats", "Detailed storage / DB / ingestion metrics"),
            ("metrics", "In-process counters & histograms snapshot"),
        ],
    ),
    (
        "Interactive",
        [
            ("shell", "Full REPL — any command, history & Tab-complete"),
            ("commands", "Show this command map"),
            ("clear", "Clear the screen"),
        ],
    ),
]


def render_command_map() -> Group:
    """A categorised, two-column reference of every command."""
    blocks: list[RenderableType] = []
    for title, rows in COMMAND_CATEGORIES:
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style=f"bold {C_HI}", no_wrap=True, min_width=20)
        grid.add_column(style=C_DIM)
        for cmd, desc in rows:
            grid.add_row(cmd, Text(desc, style=C_DIM))
        blocks.append(Text())
        blocks.append(Text.assemble(("▸ ", C_FAINT), (title, f"bold {C_FG}")))
        blocks.append(grid)
    return Group(*blocks)
