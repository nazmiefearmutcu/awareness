"""Awareness CLI banner + intro rendering.

A big "ANSI Shadow" wordmark with a 3-D drop-shadow colourisation (bright
gradient glyph faces over dim extrusion edges), plus a context-aware
"getting started" panel and a categorised command map. The ASCII art is baked
in as a constant, so the CLI carries no figlet runtime dependency.

Everything here is pure rendering: callers pass in a small context dict, so
this module never touches the database, network, or filesystem.
"""

from __future__ import annotations

import shutil
from typing import Any

from rich.align import Align
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Big shadowed wordmark — figlet "ANSI Shadow" font, baked in (see scripts).
_BIG_LINES = [
    ' █████╗ ██╗    ██╗ █████╗ ██████╗ ███████╗███╗   ██╗███████╗███████╗███████╗',
    '██╔══██╗██║    ██║██╔══██╗██╔══██╗██╔════╝████╗  ██║██╔════╝██╔════╝██╔════╝',
    '███████║██║ █╗ ██║███████║██████╔╝█████╗  ██╔██╗ ██║█████╗  ███████╗███████╗',
    '██╔══██║██║███╗██║██╔══██║██╔══██╗██╔══╝  ██║╚██╗██║██╔══╝  ╚════██║╚════██║',
    '██║  ██║╚███╔███╔╝██║  ██║██║  ██║███████╗██║ ╚████║███████╗███████║███████║',
    '╚═╝  ╚═╝ ╚══╝╚══╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═══╝╚══════╝╚══════╝╚══════╝',
    '                                                                            ',
]
_BIG_WIDTH = max(len(line) for line in _BIG_LINES)

# Compact wordmark for narrow terminals (kept for inline mid-session reprints).
SMALL_BANNER = '\n   ___      ___   ___ ___ _  _ ___ ___ ___ \n  /_\\ \\    / /_\\ | _ \\ __| \\| | __/ __/ __|\n / _ \\ \\/\\/ / _ \\|   / _|| .` | _|\\__ \\__ \\\n/_/ \\_\\_/\\_/_/ \\_\\_|_\\___|_|\\_|___|___/___/\n'

TAGLINE = "public text internet awareness engine"

_SMALL_WIDTH = max((len(line) for line in SMALL_BANNER.splitlines()), default=0)

# Vertical gradient applied top→bottom across the glyph faces.
_FACE_GRADIENT = ["#67e8f9", "#22d3ee", "#06b6d4", "#0ea5e9", "#3b82f6", "#6366f1"]
# Box-drawing characters figlet uses for the 3-D extrusion → render as shadow.
_SHADOW_CHARS = set("╔╗╚╝║═")
_FACE_CHARS = {"█"}
_SHADOW_STYLE = "grey37"


def _render_big_text() -> Text:
    """Per-character colourisation: bright gradient faces, dim shadow edges."""
    text = Text(no_wrap=True, overflow="crop")
    last = len(_BIG_LINES) - 1
    for i, line in enumerate(_BIG_LINES):
        face = _FACE_GRADIENT[min(i, len(_FACE_GRADIENT) - 1)]
        for ch in line:
            if ch in _FACE_CHARS:
                text.append(ch, style=f"bold {face}")
            elif ch in _SHADOW_CHARS:
                text.append(ch, style=_SHADOW_STYLE)
            else:
                text.append(ch)
        if i != last:
            text.append("\n")
    return text


def _terminal_width(width: int | None) -> int:
    if width is not None:
        return width
    return shutil.get_terminal_size((80, 24)).columns


def render_banner(width: int | None = None) -> RenderableType:
    """Big shadowed wordmark, or the compact fallback on narrow terminals."""
    cols = _terminal_width(width)
    if cols >= _BIG_WIDTH:
        return Align.center(_render_big_text(), width=cols)
    if cols >= _SMALL_WIDTH:
        return Align.center(Text(SMALL_BANNER, style="bold cyan"), width=cols)
    return Align.center(Text("✦ AWARENESS ✦", style="bold cyan"), width=cols)


def _headline(ctx: dict[str, Any]) -> Text:
    """A single line that adapts to where the user is in the workflow."""
    if not ctx.get("initialized"):
        return Text.from_markup(
            "[bold]First run?[/bold]  Run [bold cyan]awareness init[/bold cyan] to choose where "
            "data lives — then kick off ingestion below."
        )
    jobs = ctx.get("jobs", 0) or 0
    if jobs <= 0:
        return Text.from_markup(
            "[bold]Storage is ready[/bold] — but nothing captured yet. Start ingesting:"
        )
    docs = ctx.get("docs")
    run_word = "run" if jobs == 1 else "runs"
    if docs:
        doc_word = "document" if docs == 1 else "documents"
        return Text.from_markup(
            f"[bold]{docs:,} {doc_word}[/bold] captured across [bold]{jobs}[/bold] {run_word}. "
            "Explore your corpus or capture more:"
        )
    return Text.from_markup(
        f"[bold]{jobs}[/bold] ingestion {run_word} recorded. Explore your corpus or capture more:"
    )


# (command, one-line description) — the most useful entry points, in order.
_QUICKSTART: list[tuple[str, str]] = [
    ("awareness shell", "Interactive control center — every command, with ↑ history & Tab-complete"),
    ("awareness tui", "Live full-screen telemetry dashboard"),
    ("awareness backfill submit --start 2024-01-01", "Pull historical public web text (BODY)"),
    ("awareness tail start", "Capture newly-published public text, live (TAIL)"),
    ('awareness search "climate policy"', "Full-text search everything you've captured"),
    ("awareness status", "Services, recent jobs & disk usage at a glance"),
    ("awareness commands", "The full command map  ·  awareness --help for flags"),
]


def status_chips(ctx: dict[str, Any]) -> Text:
    """Compact API/Tail/storage status line."""
    def chip(label: str, on: bool) -> tuple[str, str]:
        dot = "●" if on else "○"
        style = "bold green" if on else "grey50"
        return (f"{dot} {label}", style)

    text = Text(justify="center")
    for i, (lbl, on) in enumerate(
        [("API", ctx.get("api_running", False)), ("TAIL", ctx.get("tail_running", False))]
    ):
        label, style = chip(lbl, on)
        if i:
            text.append("    ")
        text.append(label, style=style)
    store = "cloud + local" if ctx.get("cloud") else "local"
    text.append("    ")
    text.append(f"⛁ {store}", style="grey50")
    return text


def getting_started_panel(ctx: dict[str, Any] | None = None, width: int | None = None) -> Panel:
    """A 'try one of these' panel tailored to the user's current state."""
    ctx = ctx or {}
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold cyan", no_wrap=True)
    grid.add_column(style="white")
    for cmd, desc in _QUICKSTART:
        grid.add_row(cmd, Text(desc, style="dim"))

    body = Group(_headline(ctx), Text(), grid)
    return Panel(
        body,
        title="[bold]🚀  Getting started[/bold]",
        title_align="left",
        border_style="cyan",
        padding=(1, 2),
        expand=False,
    )


def render_intro(
    ctx: dict[str, Any] | None = None,
    width: int | None = None,
    *,
    subtitle: str | None = None,
) -> Group:
    """Full launch experience: banner + tagline + (optional subtitle) + getting started."""
    ctx = ctx or {}
    cols = _terminal_width(width)
    parts: list[RenderableType] = [
        Text(),
        render_banner(cols),
        Align.center(Text(TAGLINE, style="italic dim cyan")),
        Align.center(status_chips(ctx)),
        Text(),
    ]
    if subtitle:
        parts.append(Align.center(Text(subtitle, style="green")))
        parts.append(Text())
    parts.append(getting_started_panel(ctx, cols))
    return Group(*parts)


# ── full command map (used by `commands` and the shell `help`) ───────────────
COMMAND_CATEGORIES: list[tuple[str, list[tuple[str, str]]]] = [
    ("Service & lifecycle", [
        ("start", "Start API server (+ live tail) in the background"),
        ("stop", "Stop the background API server & tail"),
        ("restart", "Stop then start the API server"),
        ("status", "Services, recent jobs & disk usage"),
        ("health", "Quick JSON liveness probe"),
        ("dashboard", "Open the web dashboard in a browser"),
        ("tui", "Live full-screen terminal dashboard"),
        ("logs", "View / follow API & app logs"),
        ("service …", "Install & manage the macOS launchd agent"),
    ]),
    ("Ingest — BODY (historical)", [
        ("backfill submit", "Queue a historical date-range crawl"),
        ("backfill run", "Run a queued backfill job to completion"),
        ("backfill status", "Inspect a backfill job"),
        ("compact", "Fold JSONL staging into the Iceberg warehouse"),
    ]),
    ("Ingest — TAIL (live)", [
        ("tail start", "Capture newly-published text until stopped"),
        ("tail stop", "Request a running tail to stop"),
        ("tail status", "Show tail daemon state"),
        ("tail check-seeds", "Validate feeds/sitemaps in tail_seeds.yaml"),
    ]),
    ("Explore your corpus", [
        ("search", "Full-text search captured documents"),
        ("browse", "Page through captures & read full text"),
        ("inspect", "Tabular query by date / domain / source"),
        ("counts", "Aggregate counts by source & domain"),
        ("export", "Export to a JSONL file or a folder of .txt"),
        ("hf-push", "Publish captures to a Hugging Face dataset"),
    ]),
    ("Deduplication", [
        ("dedup check", "Test a URL / text / file against the index"),
        ("dedup-stats", "Dump dedup index statistics"),
    ]),
    ("Config & cloud", [
        ("init", "Initialise storage layout & choose data dir"),
        ("config show", "Show current configuration"),
        ("config set", "Persist a config value to awareness.yaml"),
        ("config interactive", "Edit configuration interactively"),
        ("cloud auth-gdrive", "Authorise Google Drive storage"),
        ("cloud status", "Show cloud storage integration status"),
    ]),
    ("Insight", [
        ("stats", "Detailed storage / DB / ingestion metrics"),
        ("metrics", "In-process counters & histograms snapshot"),
    ]),
    ("Interactive", [
        ("shell", "Full REPL — any command, history & Tab-complete"),
        ("commands", "Show this command map"),
        ("clear", "Clear the screen"),
    ]),
]


def render_command_map() -> Group:
    """A categorised, two-column reference of every command."""
    blocks: list[RenderableType] = []
    for title, rows in COMMAND_CATEGORIES:
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="bold cyan", no_wrap=True, min_width=20)
        grid.add_column(style="white")
        for cmd, desc in rows:
            grid.add_row(cmd, Text(desc, style="dim"))
        blocks.append(Text())
        blocks.append(Text(f"▸ {title}", style="bold magenta"))
        blocks.append(grid)
    return Group(*blocks)
