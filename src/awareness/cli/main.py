"""``awareness`` CLI.

Subcommands:
    backfill submit     — submit a BODY job
    backfill run        — run pending tasks to completion (in-process)
    backfill status     — show job state
    tail start          — start TAIL daemon (foreground)
    tail stop           — stop the running TAIL
    tail status         — show tail state
    status              — overall system status
    health              — quick liveness check
    inspect             — query stored captures by date range
    dedup-stats         — dedup metrics
    metrics             — counters/histograms
    quality             — corpus quality report
    feeds               — feed-health report
    init                — initialize storage layout
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import select
import signal
import smtplib
import socket
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import click
import typer
import yaml
from rich import print as rprint
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from awareness.cli import banner
from awareness.cli.export_util import export_fold_key_sql, query_export_captures, write_export_jsonl
from awareness.config import get_settings, reset_settings
from awareness.config import schema as cfg_schema
from awareness.dedup.engine import DEFAULT_NEAR_THRESHOLD
from awareness.obs.logging import configure_logging, get_logger
from awareness.obs.metrics import get_metrics
from awareness.planner.planner import Planner
from awareness.savedsearch.store import SavedSearchStore
from awareness.schemas.doc import SourceKind
from awareness.schemas.jobs import BackfillRequest
from awareness.storage.duckdb_index import DuckDbIndex
from awareness.storage.state import StateDB
from awareness.tail.engine import TailEngine
from awareness.util.lang import PRIMARY_LANGUAGE_SQL, append_language_filter
from awareness.util.timeutil import coerce_relative_end, inclusive_end, to_utc, utcnow
from awareness.workers.engine import WorkerEngine

app = typer.Typer(no_args_is_help=False, help="Awareness — public text internet awareness engine")
backfill_app = typer.Typer(no_args_is_help=True, help="BODY: historical backfill")
tail_app = typer.Typer(no_args_is_help=True, help="TAIL: live capture")
service_app = typer.Typer(no_args_is_help=True, help="Manage launchd daemon service on macOS")
config_app = typer.Typer(no_args_is_help=True, help="Configure Awareness settings")
cloud_app = typer.Typer(no_args_is_help=True, help="Configure cloud storage integrations (Google Drive, S3)")
dedup_app = typer.Typer(no_args_is_help=True, help="Deduplication inspection & checks")
dlq_app = typer.Typer(no_args_is_help=True, help="Dead-letter queue: inspect failed tasks")
alerts_app = typer.Typer(no_args_is_help=True, help="Alert rules: keyword/spike thresholds + webhooks")
x_app = typer.Typer(no_args_is_help=True, help="X scraper sessions: create, list, inspect")
saved_app = typer.Typer(no_args_is_help=True, help="Saved searches: bookmark queries, list, re-run")

app.add_typer(backfill_app, name="backfill")
app.add_typer(tail_app, name="tail")
app.add_typer(service_app, name="service")
app.add_typer(config_app, name="config")
app.add_typer(cloud_app, name="cloud")
app.add_typer(dedup_app, name="dedup")
app.add_typer(dlq_app, name="dlq")
app.add_typer(alerts_app, name="alerts")
app.add_typer(x_app, name="x")
app.add_typer(saved_app, name="saved")

# Wire the alert-rule CLI (created by the alerts feature team) into the app.
from awareness.alerts import cli as _alerts_cli  # noqa: E402

for _cmd in list(_alerts_cli.app.registered_commands):
    _alerts_cli.app.registered_commands.remove(_cmd)
    alerts_app.registered_commands.append(_cmd)
del _alerts_cli, _cmd


@alerts_app.command(name="run-once")
def alerts_run_once() -> None:
    """Evaluate all active alert rules once (delivering webhooks) and exit."""
    from awareness.alerts.runner import create_default_runner  # noqa: PLC0415

    settings = get_settings()

    def _index() -> DuckDbIndex:
        return DuckDbIndex(
            db_path=settings.duckdb_path(),
            jsonl_dir=settings.staging_jsonl_dir(),
            iceberg_warehouse=settings.iceberg_warehouse,
        )

    try:
        firings = asyncio.run(create_default_runner(_index).evaluate_once())
    except RuntimeError as exc:
        rprint(f"[yellow]index not ready: {exc}[/yellow]")
        raise typer.Exit(code=2) from exc
    if firings:
        console.print(
            f"Fired {len(firings)} alert rule(s): "
            + ", ".join(f.rule_name for f in firings)
        )
    else:
        console.print("No alert firings.")


# ── saved searches ────────────────────────────────────────────────────────
def _saved_store() -> SavedSearchStore:
    """SavedSearchStore at ``<data_dir>/saved_searches.db``."""
    settings = get_settings()
    assert settings.data_dir is not None
    return SavedSearchStore(settings.data_dir / "saved_searches.db")


@saved_app.command(name="list")
def saved_list() -> None:
    """List all saved searches (pinned first, then most recently run)."""
    store = _saved_store()
    try:
        saved = store.list()
    finally:
        store.close()
    if not saved:
        console.print("No saved searches.")
        return
    table = Table(title="Saved searches")
    for col in ("ID", "Name", "Query", "Mode", "Fields", "Limit", "Pinned", "Updated"):
        table.add_column(col)
    for s in saved:
        table.add_row(
            s.id,
            s.name,
            s.query,
            s.mode,
            s.fields,
            str(s.limit),
            "yes" if s.pinned else "no",
            s.updated_at.isoformat(timespec="minutes"),
        )
    console.print(table)


@saved_app.command(name="add")
def saved_add(
    name: str = typer.Argument(..., help="Name for the saved search"),
    query: str = typer.Argument(..., help="Search query to bookmark"),
    mode: str = typer.Option(
        "auto", "--mode", "-m", help="Match mode: auto | fts | prefix | substring"
    ),
    fields: str = typer.Option(
        "title,text", "--fields", "-f", help="Comma-list of columns to match"
    ),
    limit: int = typer.Option(10, "--limit", "-l", help="Results per run"),
) -> None:
    """Save a search for later re-runs."""
    store = _saved_store()
    try:
        try:
            saved = store.create(name=name, query=query, mode=mode, fields=fields, limit=limit)
        except ValueError as exc:
            console.print(f"[red]invalid saved search: {exc}[/red]")
            raise typer.Exit(code=2) from exc
    finally:
        store.close()
    console.print(
        f"Saved search [bold cyan]{saved.name}[/bold cyan] ({saved.id}) "
        f"mode={saved.mode} limit={saved.limit}"
    )


@saved_app.command(name="rm")
def saved_rm(saved_id: str = typer.Argument(..., help="Saved search id to delete")) -> None:
    """Delete a saved search by id."""
    store = _saved_store()
    try:
        deleted = store.delete(saved_id)
    finally:
        store.close()
    if not deleted:
        console.print(f"[yellow]No saved search with id {saved_id}[/yellow]")
        raise typer.Exit(code=2)
    console.print(f"Deleted saved search {saved_id}")


@saved_app.command(name="run")
def saved_run(
    saved_id: str = typer.Argument(..., help="Saved search id to run"),
    limit: int = typer.Option(0, "--limit", "-l", help="Results per page (0 = saved limit)"),
) -> None:
    """Run a saved search against the corpus and print results (rich table).

    Mirrors the non-interactive ``search`` output; bumps the saved search's
    ``updated_at`` as a last-run marker.
    """
    settings = get_settings()
    store = _saved_store()
    idx = DuckDbIndex(
        db_path=settings.duckdb_path(),
        jsonl_dir=settings.staging_jsonl_dir(),
        iceberg_warehouse=settings.iceberg_warehouse,
    )
    try:
        saved = store.get(saved_id)
        if saved is None:
            console.print(f"[yellow]No saved search with id {saved_id}[/yellow]")
            raise typer.Exit(code=2)
        res_limit = limit if limit > 0 else saved.limit
        fields = [f.strip().lower() for f in saved.fields.split(",") if f.strip()]
        try:
            res = idx.search(
                saved.query,
                limit=res_limit,
                offset=0,
                mode=saved.mode,
                fields=fields,
            )
        except RuntimeError as exc:
            console.print(f"[yellow]index not ready: {exc}[/yellow]")
            raise typer.Exit(code=2) from exc
        store.touch(saved.id)
    finally:
        idx.close()
        store.close()

    total = res["total"]
    rows = res["rows"]
    ranked = res["ranked"]
    used_mode = res.get("mode", saved.mode)
    rprint(
        f"[bold cyan]Saved search:[/bold cyan] '{saved.name}' — '{saved.query}' "
        f"(Found {total} documents, showing top {len(rows)}, "
        f"Mode: {used_mode}, Ranked: {ranked})"
    )
    rprint("-" * 80)
    if total == 0:
        rprint(f"[yellow]No documents matched query '{saved.query}'.[/yellow]")
        return
    for r in rows:
        title = r["title"] or "No Title"
        score_str = f" [score: {r['score']:.4f}]" if r["score"] is not None else ""
        highlighted_title = highlight_tokens(title, saved.query)
        rprint(f"[bold white]• {highlighted_title}[/bold white]{score_str}")
        rprint(
            f"  [dim]Domain: {r['domain'] or 'N/A'} | Captured: {r['fetch_ts']} | Source: {r['source_type'] or 'N/A'}[/dim]"
        )
        if r.get("snippet"):
            highlighted_snippet = highlight_tokens(r["snippet"], saved.query)
            rprint(f'  [italic]"{highlighted_snippet}"[/italic]')
        rprint()

logger = get_logger("cli")
console = Console(theme=banner.AWARENESS_THEME)


def _install_typer_theme() -> None:
    """Recolour Typer's rich help (usage, options/commands panels) to match the
    Awareness turquoise spec — same hex values as ``banner.AWARENESS_THEME``."""
    try:
        import typer.rich_utils as ru
    except Exception:
        return
    overrides = {
        "STYLE_USAGE": banner.C_DIM,
        "STYLE_USAGE_COMMAND": f"bold {banner.C_HI}",
        "STYLE_OPTION": banner.C_HI,
        "STYLE_COMMANDS_TABLE_FIRST_COLUMN": banner.C_HI,
        "STYLE_SWITCH": banner.C_FG,
        "STYLE_METAVAR": banner.C_DIM,
        "STYLE_METAVAR_SEPARATOR": banner.C_FAINT,
        "STYLE_HELPTEXT": banner.C_FG,
        "STYLE_HELPTEXT_FIRST_LINE": banner.C_FG,
        "STYLE_OPTION_HELP": banner.C_DIM,
        "STYLE_OPTION_DEFAULT": banner.C_DIM,
        "STYLE_OPTIONS_PANEL_BORDER": banner.C_LINE,
        "STYLE_COMMANDS_PANEL_BORDER": banner.C_LINE,
        "STYLE_OPTIONS_TABLE_LEADING": "",
        "STYLE_COMMANDS_TABLE_LEADING": "",
    }
    for name, value in overrides.items():
        if hasattr(ru, name):
            setattr(ru, name, value)


_install_typer_theme()


def _get_yaml_config_path() -> Path:
    """Path of the YAML override file.

    Must agree with how :mod:`awareness.config.settings` *reads* the override
    (it honours ``AW_CONFIG_FILE`` then ``AW_PROJECT_ROOT``); otherwise a
    ``config set`` could write a file the engine never loads back — and tests
    that isolate via ``AW_PROJECT_ROOT`` would scribble on the real repo file.
    """
    env_path = os.environ.get("AW_CONFIG_FILE")
    if env_path:
        return Path(env_path)
    env_root = os.environ.get("AW_PROJECT_ROOT")
    root = Path(env_root).resolve() if env_root else Path(__file__).resolve().parents[3]
    return root / "configs" / "awareness.yaml"


def _coerce_val(val: str) -> Any:
    val_lower = val.lower()
    if val_lower == "true":
        return True
    if val_lower == "false":
        return False
    try:
        return int(val)
    except ValueError:
        try:
            return float(val)
        except ValueError:
            return val


def _read_yaml_data() -> dict[str, Any]:
    """Load the YAML override file as a dict (empty on missing/invalid)."""
    path = _get_yaml_config_path()
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_yaml_data(data: dict[str, Any]) -> None:
    """Persist the whole override mapping atomically (write-tmp-then-rename)."""
    path = _get_yaml_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=True, allow_unicode=True)
        tmp.replace(path)
    except Exception:
        # Never leave a half-written .tmp behind (e.g. cross-device rename, perms).
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _set_yaml_values(values: dict[str, Any]) -> None:
    """Merge already-typed values (bool/int/float/str) into the override file."""
    data = _read_yaml_data()
    data.update(values)
    _write_yaml_data(data)


def _unset_yaml_value(key: str) -> bool:
    """Remove ``key`` from the override file. Returns True if it was present."""
    data = _read_yaml_data()
    if key in data:
        del data[key]
        _write_yaml_data(data)
        return True
    return False


def _update_yaml_config(key: str, value: Any) -> None:
    """Back-compat single-key writer (used by ``init``)."""
    _set_yaml_values({key: _coerce_val(str(value))})


def _app_version() -> str:
    try:
        from importlib.metadata import version

        return version("awareness")
    except Exception:
        from awareness import __version__

        return __version__


def _version_callback(value: bool) -> None:
    if value:
        rprint(
            f"[bold {banner.C_HI}]awareness[/] [{banner.C_DIM}]v{_app_version()}[/]  "
            f"[{banner.C_FAINT}]·[/]  [{banner.C_DIM}]public text internet awareness engine[/]"
        )
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        None,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show the Awareness version and exit.",
    ),
) -> None:
    """Awareness — public text internet awareness engine"""
    if ctx.invoked_subcommand is None:
        console.print(banner.render_intro(_quickstart_context()))
        raise typer.Exit()


def _bootstrap() -> tuple[StateDB, Planner]:
    settings = get_settings()
    configure_logging(level=settings.log_level, json=settings.log_json, log_dir=settings.log_dir)
    state = StateDB(settings.state_db_url or "sqlite:///awareness.sqlite")
    state.init()
    return state, Planner(state)


def _light_state() -> StateDB:
    """Open the state DB for a read-only peek WITHOUT configuring stdout logging
    or building the adapter registry — keeps the intro & shell prompt noise-free."""
    settings = get_settings()
    state = StateDB(settings.state_db_url or "sqlite:///awareness.sqlite")
    state.init()
    return state


def _default_api_port() -> int:
    """The port the API status indicators probe (honours AW_API_PORT, else 8085)."""
    try:
        return int(os.environ.get("AW_API_PORT", "8085"))
    except (TypeError, ValueError):
        return 8085


# M-01: user-facing --source aliases → canonical SourceKind. Mirrors the TUI
# mapping (see the "Create New Job" flow) so the CLI and TUI accept the same
# spellings (CC-WET, common_crawl_wet, wet, FW, …) instead of tracebacking.
_SOURCE_ALIASES: dict[str, SourceKind] = {
    "cc_wet": SourceKind.COMMON_CRAWL_WET,
    "common_crawl_wet": SourceKind.COMMON_CRAWL_WET,
    "wet": SourceKind.COMMON_CRAWL_WET,
    "fineweb": SourceKind.FINEWEB,
    "fw": SourceKind.FINEWEB,
    "gdelt": SourceKind.GDELT,
    "rss": SourceKind.RSS,
    "sitemap": SourceKind.SITEMAP,
    "common_crawl_index": SourceKind.COMMON_CRAWL_INDEX,
    "common_crawl_warc": SourceKind.COMMON_CRAWL_WARC,
    "fineweb_2": SourceKind.FINEWEB_2,
    "atom": SourceKind.ATOM,
    "tail_recrawl": SourceKind.TAIL_RECRAWL,
}


def _resolve_source_kind(raw: str) -> SourceKind:
    """Normalize a ``--source`` value (lowercase, ``-``→``_``, aliases).

    Raises :class:`typer.BadParameter` with the valid list on failure so the
    CLI reports a clean error instead of a ValueError traceback (M-01).
    """
    cleaned = str(raw).strip().lower().replace("-", "_")
    kind = _SOURCE_ALIASES.get(cleaned)
    if kind is not None:
        return kind
    try:
        return SourceKind(cleaned)
    except ValueError:
        canonical = sorted(f"{alias} ({kind.value})" for alias, kind in _SOURCE_ALIASES.items())
        raise typer.BadParameter(f"Unknown --source {raw!r}. Valid values: {', '.join(canonical)}") from None


def _coerce_end_checked(end: str) -> datetime:
    """``coerce_relative_end`` with CLI-friendly error conversion (M-03)."""
    try:
        return coerce_relative_end(end)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


class _StdinLineReader:
    """Non-blocking stdin reader for interactive run/tail commands (M-04).

    The old implementation parked an executor thread on ``sys.stdin.readline()``
    — with a TTY that stays open (no Enter pressed) the thread blocks forever,
    so ``backfill run`` / ``tail start`` hung at exit until the user hit Enter.
    This reader polls stdin with ``select`` (0.2s) from a daemon thread and
    dispatches complete lines on the event loop via ``call_soon_threadsafe``.
    ``stop()`` unblocks the thread within one poll interval, so the process
    exits promptly once the engine finishes.
    """

    POLL_SEC = 0.2

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        handler: Any,
    ) -> None:
        self._loop = loop
        self._handler = handler
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="awareness-stdin-reader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([sys.stdin], [], [], self.POLL_SEC)
            except (OSError, ValueError, TypeError, InterruptedError):
                return
            if not ready:
                continue
            try:
                line = sys.stdin.readline()
            except (ValueError, OSError):
                return
            if not line:
                return
            self._loop.call_soon_threadsafe(self._handler, line.rstrip("\n"))


def _quickstart_context() -> dict[str, Any]:
    """Cheap signals powering the intro headline + status chips.

    Never raises — the banner must render even on a pristine or broken setup.
    """
    ctx: dict[str, Any] = {
        "initialized": False,
        "jobs": 0,
        "docs": None,
        "api_running": False,
        "tail_running": False,
        "cloud": False,
        "version": _app_version(),
        "prompt": "awareness ~ %",
        "api_port": _default_api_port(),
    }
    try:
        import getpass
        import socket

        host = socket.gethostname().split(".")[0]
        ctx["prompt"] = f"{getpass.getuser()}@{host} ~ %"
    except Exception:
        pass
    try:
        settings = get_settings()
        ctx["cloud"] = bool(settings.enable_iceberg) and _is_cloud_path(settings.iceberg_warehouse)
        state_file = settings.data_dir / "state" / "awareness.sqlite" if settings.data_dir else None
        ctx["initialized"] = bool(state_file and state_file.exists())
        ctx["api_running"] = _get_api_pid() is not None or _is_port_active("127.0.0.1", _default_api_port())
        if ctx["initialized"]:
            try:
                state = _light_state()
                metrics = _query_db_metrics(state)
                ctx["jobs"] = metrics.get("jobs_count", 0)
                ctx["docs"] = metrics.get("total_docs_emitted", 0)
                ctx["tail_running"] = bool(state.get_tail().get("running"))
            except Exception:
                pass
    except Exception:
        pass
    return ctx


@app.command()
def init(
    interactive: bool = typer.Option(
        True,
        "--interactive/--no-interactive",
        help="Prompt user for directory path (auto-disabled when stdin is not a TTY)",
    ),
) -> None:
    """Initialize storage paths, state DB, Iceberg catalog (idempotent)."""
    settings = get_settings()

    # Scripts / CI / piped stdin must never hang on a confirm prompt.
    if interactive and not sys.stdin.isatty():
        interactive = False

    if interactive:
        rprint("[bold cyan]Awareness Environment Initialization[/bold cyan]")
        current_dir = settings.data_dir or (settings.project_root / "data")
        rprint(f"Current local data save directory: [yellow]{current_dir}[/yellow]")

        change = typer.confirm(
            "Would you like to choose a different local directory for data storage?",
            default=False,
        )
        if change:
            new_path = typer.prompt(
                "Enter the absolute path to store data files",
                default=str(current_dir),
            )
            new_path_resolved = Path(new_path).resolve()
            try:
                _update_yaml_config("data_dir", str(new_path_resolved))
                reset_settings()
                settings = get_settings()
                rprint(f"[green]✔ Local data directory updated to: {new_path_resolved}[/green]")
            except Exception as e:
                rprint(f"[red]Failed to update data directory configuration: {e}[/red]")

    state, _ = _bootstrap()
    # Touch Iceberg if enabled.
    if settings.enable_iceberg:
        try:
            from awareness.storage.iceberg import IcebergWriter  # noqa: PLC0415

            assert settings.iceberg_catalog_db is not None
            assert settings.iceberg_warehouse is not None
            w = IcebergWriter(catalog_db=settings.iceberg_catalog_db, warehouse=settings.iceberg_warehouse)
            w.ensure_table()
            rprint("[green]Iceberg table ready[/green]")
        except Exception as exc:
            rprint(f"[yellow]Iceberg init skipped:[/yellow] {exc}")
    # Materialize the DuckDB search-index file so init leaves a complete
    # storage layout (the file previously only appeared on the first query).
    # An empty corpus is fine — health_snapshot() builds the views and a
    # 0-row captures table; failure (e.g. offline extension install) only
    # warns and never fails init.
    try:
        idx = DuckDbIndex(
            db_path=settings.duckdb_path(),
            jsonl_dir=settings.staging_jsonl_dir(),
            iceberg_warehouse=settings.iceberg_warehouse,
        )
        idx.health_snapshot()
        idx.close()
        rprint(f"[green]DuckDB index:[/green] {settings.duckdb_path()}")
    except Exception as exc:
        rprint(f"[yellow]DuckDB index init skipped:[/yellow] {exc}")
    rprint(f"[green]State DB:[/green] {state.url}")
    rprint(f"[green]Data dir:[/green] {settings.data_dir}")


@app.command()
def health() -> None:
    """Quick liveness check."""
    state, _ = _bootstrap()
    settings = get_settings()
    info = {
        "ok": True,
        "state_db": state.url,
        "data_dir": str(settings.data_dir),
        "iceberg_warehouse": str(settings.iceberg_warehouse),
        "tail": state.get_tail(),
    }
    print(json.dumps(info, indent=2))


def _is_port_active(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect((host, port))
            return True
        except (TimeoutError, ConnectionRefusedError):
            return False


def _pid_matches_pid_file(pid: int, pid_file: Path) -> bool | None:
    """Verify the process at ``pid`` started around the pid file's write time.

    L-04 (PID-reuse kill guard): ``os.kill(pid, 0)`` only proves *a* process
    exists — a stale pid file can point at an unrelated process that recycled
    the number. We compare ``ps -o lstart`` against the pid-file mtime:

    * ``True``  — process started at/after the file was written (plausibly the
      API server we launched);
    * ``False`` — process started clearly BEFORE the file → recycled PID;
    * ``None``  — verdict unavailable (ps missing/failed) — callers must NOT
      signal on ``None``.
    """
    try:
        mtime = pid_file.stat().st_mtime
    except OSError:
        return None
    try:
        res = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0 or not (res.stdout or "").strip():
        return None
    try:
        import dateutil.parser  # noqa: PLC0415

        started = dateutil.parser.parse(res.stdout.strip())
        if started.tzinfo is None:
            started = started.replace(tzinfo=datetime.now().astimezone().tzinfo)
    except (ValueError, TypeError, OverflowError):
        return None
    # 1s slack for filesystem mtime granularity.
    return started.timestamp() >= mtime - 1.0


def _get_api_pid() -> int | None:
    """Return the live API server PID, cleaning a stale pid file if needed."""
    settings = get_settings()
    pid_file = settings.data_dir / "state" / "api.pid"
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        verdict = _pid_matches_pid_file(pid, pid_file)
        if verdict is False:
            # Recycled PID — the process is not our server.
            pid_file.unlink(missing_ok=True)
            return None
        # True, or None (ps unavailable) → keep current optimistic behavior.
        return pid
    except (ValueError, OSError):
        # Process gone or unreadable pid — drop the file so status stays honest.
        try:
            pid_file.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def _trigger_tail_start(host: str, port: int, silent: bool = False) -> None:
    import httpx

    url = f"http://{host}:{port}/tail/start"
    try:
        r = httpx.post(url, timeout=5.0)
        if r.status_code == 200:
            if not silent:
                rprint("[green]✔ Live Tail daemon started successfully (in-process)[/green]")
        elif not silent:
            rprint(
                f"[yellow]⚠ Failed to start tail daemon via API (status {r.status_code}): {r.text}[/yellow]"
            )
    except Exception as e:
        if not silent:
            rprint(f"[yellow]⚠ Could not connect to API to start tail: {e}[/yellow]")


@app.command()
def start(
    host: str = typer.Option("127.0.0.1", "--host", help="Host address to bind to"),
    port: int = typer.Option(
        _default_api_port, "--port", help="Port to bind to (default: AW_API_PORT or 8085)"
    ),
    tail: bool = typer.Option(True, "--tail/--no-tail", help="Start the live tail daemon in-process"),
    fg: bool = typer.Option(False, "--fg", help="Run in foreground (blocking)"),
    data_dir: Path = typer.Option(None, "--data-dir", "-d", help="Custom local data directory"),
    to_cloud: bool = typer.Option(False, "--to-cloud", help="Enable cloud S3 storage (Iceberg)"),
    to_local: bool = typer.Option(True, "--to-local/--no-to-local", help="Enable local JSONL/SQLite storage"),
    warehouse: str = typer.Option(
        None, "--warehouse", help="S3 bucket / warehouse path (e.g. s3://bucket/path)"
    ),
) -> None:
    """Start the background Awareness API server (and optional tail daemon)."""
    console.print(banner.render_banner())

    if data_dir:
        os.environ["AW_DATA_DIR"] = str(data_dir.resolve())
    if to_cloud:
        os.environ["AW_ENABLE_ICEBERG"] = "True"
        if warehouse:
            os.environ["AW_ICEBERG_WAREHOUSE"] = warehouse
        elif not os.environ.get("AW_ICEBERG_WAREHOUSE"):
            os.environ["AW_ICEBERG_WAREHOUSE"] = "s3://awareness/warehouse"
    if not to_local:
        os.environ["AW_ENABLE_JSONL_STAGING"] = "False"

    reset_settings()
    if _is_port_active(host, port):
        pid = _get_api_pid()
        if pid:
            rprint(
                f"[yellow]Awareness API is already running in background (PID {pid}) on http://{host}:{port}[/yellow]"
            )
        else:
            rprint(
                f"[yellow]Port {port} is already in use. Awareness API might be running under a different manager (e.g. launchd).[/yellow]"
            )
        if tail:
            _trigger_tail_start(host, port)
        return

    if fg:
        rprint(f"[green]Starting Awareness API on http://{host}:{port} in foreground...[/green]")
        os.environ["AW_API_HOST"] = host
        os.environ["AW_API_PORT"] = str(port)
        if tail:

            def trigger() -> None:
                import time

                # L-03: the API takes a moment to bind; POSTing /tail/start
                # immediately races startup and is silently lost. Poll the
                # port (cap ~30s) before triggering, warn on timeout.
                deadline = time.time() + 30.0
                while time.time() < deadline:
                    if _is_port_active(host, port):
                        _trigger_tail_start(host, port, silent=True)
                        return
                    time.sleep(0.5)
                rprint(
                    "[yellow]⚠ API did not come up within 30s — live tail daemon "
                    "not started. Check the API log for errors.[/yellow]"
                )

            threading.Thread(target=trigger, daemon=True).start()
        from awareness.api.server import run as run_server

        run_server()
        return

    settings = get_settings()
    log_dir = settings.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    api_log_path = log_dir / "api.log"
    state_dir = settings.data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    pid_file = state_dir / "api.pid"

    rprint(f"[green]Starting Awareness API on http://{host}:{port} in background...[/green]")
    rprint(f"Logging to [cyan]{api_log_path}[/cyan]")

    env = os.environ.copy()
    env["AW_API_HOST"] = host
    env["AW_API_PORT"] = str(port)

    with open(api_log_path, "a", encoding="utf-8") as lf:
        proc = subprocess.Popen(
            [sys.executable, "-c", "from awareness.api.server import run; run()"],
            env=env,
            stdout=lf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    pid_file.write_text(str(proc.pid))

    started = False
    for _ in range(15):
        import time

        time.sleep(0.5)
        if _is_port_active(host, port):
            started = True
            break
        if proc.poll() is not None:
            rprint("[red]API server process exited unexpectedly during startup.[/red]")
            if api_log_path.exists():
                lines = api_log_path.read_text().splitlines()[-5:]
                rprint("[red]Last log lines:[/red]")
                for line in lines:
                    rprint(f"  {line}")
            return

    if started:
        rprint(f"[green]✔ Awareness API successfully started (PID {proc.pid})[/green]")
        rprint(f"Dashboard available at: [bold][blue]http://{host}:{port}/[/blue][/bold]")
        if tail:
            _trigger_tail_start(host, port)
        rprint(
            "\n[dim]Next:[/dim] "
            "[bold cyan]awareness shell[/bold cyan] (control center) · "
            "[bold cyan]awareness status[/bold cyan] · "
            "[bold cyan]awareness tail status[/bold cyan] · "
            "[bold cyan]awareness logs -f[/bold cyan]"
        )
    else:
        rprint("[yellow]⚠ API server started but port check timed out. It may still be booting.[/yellow]")


def _launchd_label(port: int) -> str:
    return f"com.awareness.api.{port}"


def _launchd_plist_path(port: int) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_launchd_label(port)}.plist"


def _launchd_belongs_to_project(plist_path: Path, project_root: Path) -> bool:
    """Return True only when the launchd unit's WorkingDirectory matches this project.

    Prevents ``awareness stop`` under an isolated AW_PROJECT_ROOT from unloading
    the user's system-wide LaunchAgent for a different install.
    """
    if not plist_path.exists():
        return False
    try:
        import plistlib

        with plist_path.open("rb") as fh:
            data = plistlib.load(fh)
        wd = data.get("WorkingDirectory")
        if not wd:
            return False
        return Path(str(wd)).resolve() == project_root.resolve()
    except Exception:
        return False


@app.command()
def stop(
    host: str = typer.Option("127.0.0.1", "--host", help="Host used for residual port checks"),
    port: int = typer.Option(
        _default_api_port, "--port", help="API port / launchd label port (default: AW_API_PORT or 8085)"
    ),
) -> None:
    """Stop the background Awareness API server (which also stops the tail daemon)."""
    settings = get_settings()
    pid_file = settings.data_dir / "state" / "api.pid"
    label = _launchd_label(port)
    plist_path = _launchd_plist_path(port)

    launchd_active = False
    # Only manage launchd for THIS project root — never unload a foreign install.
    if _launchd_belongs_to_project(plist_path, settings.project_root):
        try:
            res = subprocess.run(["launchctl", "list"], capture_output=True, text=True, check=False)
            if label in (res.stdout or ""):
                launchd_active = True
        except Exception:
            pass

    if launchd_active:
        rprint(
            f"[yellow]Detected {label} running via launchd for this project. "
            "Unloading it to stop completely...[/yellow]"
        )
        if plist_path.exists():
            subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True, check=False)
        else:
            subprocess.run(["launchctl", "stop", label], capture_output=True, check=False)
        rprint("[green]✔ Stopped and unloaded launchd service[/green]")

    if not pid_file.exists():
        if not launchd_active:
            rprint("[yellow]No background API server process found (PID file does not exist).[/yellow]")
            if _is_port_active(host, port):
                rprint(f"[yellow]Note: Port {port} is active. Another process might be holding it.[/yellow]")
        return

    try:
        pid = int(pid_file.read_text().strip())
    except ValueError:
        rprint("[red]Invalid PID in PID file.[/red]")
        pid_file.unlink(missing_ok=True)
        return

    import time

    try:
        os.kill(pid, 0)
    except OSError:
        rprint(f"[yellow]Process {pid} not found. Cleaning stale PID file.[/yellow]")
        pid_file.unlink(missing_ok=True)
        return

    # L-04: PID-reuse guard — never signal a process we cannot tie to the
    # pid file's write time.
    verdict = _pid_matches_pid_file(pid, pid_file)
    if verdict is False:
        rprint(
            f"[yellow]PID {pid} was started before the pid file was written — "
            "it is a recycled PID, not our API server. Removing the stale "
            "pid file; nothing was killed.[/yellow]"
        )
        pid_file.unlink(missing_ok=True)
        return
    if verdict is None:
        rprint(
            f"[yellow]Could not verify that PID {pid} is the API server "
            "(ps unavailable). Removing the stale pid file; nothing was "
            "killed. If the API is still running, stop it manually.[/yellow]"
        )
        pid_file.unlink(missing_ok=True)
        return

    rprint(f"[green]Stopping background API server (PID {pid})...[/green]")
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(10):
            time.sleep(0.5)
            try:
                os.kill(pid, 0)
            except OSError:
                rprint("[green]✔ Stopped API server cleanly.[/green]")
                pid_file.unlink(missing_ok=True)
                return
        rprint("[yellow]Process did not exit. Force killing (SIGKILL)...[/yellow]")
        os.kill(pid, signal.SIGKILL)
        pid_file.unlink(missing_ok=True)
        rprint("[green]✔ Force killed process.[/green]")
    except Exception as e:
        rprint(f"[red]Error stopping process: {e}[/red]")


@app.command()
def dashboard(
    host: str = typer.Option("127.0.0.1", "--host", help="Host address"),
    port: int = typer.Option(_default_api_port, "--port", help="Port (default: AW_API_PORT or 8085)"),
) -> None:
    """Open the Awareness dashboard in your default browser."""
    url = f"http://{host}:{port}/"
    rprint(f"[green]Opening dashboard in browser: {url}[/green]")
    webbrowser.open(url)


@app.command()
def logs(
    lines: int = typer.Option(50, "--lines", "-n", help="Number of lines to show"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow log output"),
    service: str = typer.Argument("api", help="Log file to view ('api' or 'app')"),
) -> None:
    """View API or application logs."""
    settings = get_settings()
    if service == "api":
        log_path = settings.log_dir / "api.log"
    elif service == "app":
        log_path = settings.log_dir / "awareness.log"
    else:
        rprint(f"[red]Unknown log service '{service}'. Choose 'api' or 'app'.[/red]")
        return

    if not log_path.exists():
        rprint(f"[yellow]Log file not found at: {log_path}[/yellow]")
        return

    if follow:
        rprint(f"[green]Tailing {log_path}... (Ctrl-C to exit)[/green]")
        import time

        try:
            with open(log_path, encoding="utf-8") as f:
                f.seek(0, 2)
                while True:
                    line = f.readline()
                    if not line:
                        time.sleep(0.1)
                        continue
                    print(line, end="")
        except KeyboardInterrupt:
            print()
            return
    else:
        try:
            with open(log_path, encoding="utf-8") as f:
                content = f.read().splitlines()
                last_lines = content[-lines:]
                for line in last_lines:
                    print(line)
        except Exception as e:
            rprint(f"[red]Error reading logs: {e}[/red]")


@service_app.command("install")
def service_install(
    port: int = typer.Option(
        _default_api_port, "--port", help="API port for the LaunchAgent label (default: AW_API_PORT or 8085)"
    ),
) -> None:
    """Install and load the API server as a macOS Launch Agent."""
    import plistlib

    settings = get_settings()
    root = settings.project_root.resolve()
    label = _launchd_label(port)
    plist_path = _launchd_plist_path(port)
    plist_dir = plist_path.parent

    venv_python = root / ".venv" / "bin" / "python"
    if not venv_python.exists():
        venv_python = Path(sys.executable)

    plist_data = {
        "Label": label,
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {
            "PYTHONPATH": str(root / "src"),
            "AW_API_HOST": "127.0.0.1",
            "AW_API_PORT": str(port),
            "AW_PROJECT_ROOT": str(root),
        },
        "ProgramArguments": [
            str(venv_python),
            "-c",
            "from awareness.api.server import run; run()",
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": f"/tmp/{label}.launch.out",
        "StandardErrorPath": f"/tmp/{label}.launch.err",
    }

    try:
        plist_dir.mkdir(parents=True, exist_ok=True)
        with open(plist_path, "wb") as f:
            plistlib.dump(plist_data, f)
        rprint(f"[green]✔ Plist file created at: {plist_path}[/green]")
        subprocess.run(["launchctl", "load", str(plist_path)], check=True)
        rprint(f"[green]✔ Service {label} loaded successfully via launchctl.[/green]")
    except Exception as e:
        rprint(f"[red]Error installing service: {e}[/red]")


@service_app.command("uninstall")
def service_uninstall(
    port: int = typer.Option(
        _default_api_port, "--port", help="API port for the LaunchAgent label (default: AW_API_PORT or 8085)"
    ),
) -> None:
    """Unload and remove the macOS Launch Agent plist."""
    plist_path = _launchd_plist_path(port)
    label = _launchd_label(port)

    if plist_path.exists():
        try:
            subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True, check=False)
            rprint(f"[green]✔ Service {label} unloaded via launchctl.[/green]")
            plist_path.unlink()
            rprint("[green]✔ Plist file removed.[/green]")
        except Exception as e:
            rprint(f"[red]Error uninstalling service: {e}[/red]")
    else:
        rprint(f"[yellow]Service plist file not found for {label}.[/yellow]")


@service_app.command("schedule-compaction")
def service_schedule_compaction(
    interval: int = typer.Option(60, "--interval", "-i", help="Compaction interval in minutes"),
) -> None:
    """Schedule automatic staging files compaction using macOS Launch Agent (every N minutes)."""
    import plistlib

    settings = get_settings()
    root = settings.project_root
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_path = plist_dir / "com.awareness.compact.plist"

    venv_python = root / ".venv" / "bin" / "python"
    if not venv_python.exists():
        venv_python = Path(sys.executable)

    interval_seconds = interval * 60

    plist_data = {
        "Label": "com.awareness.compact",
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {"PYTHONPATH": str(root / "src")},
        "ProgramArguments": [str(venv_python), "-m", "awareness.cli.main", "compact"],
        "StartInterval": interval_seconds,
        "StandardOutPath": "/tmp/awareness-compact.launch.out",
        "StandardErrorPath": "/tmp/awareness-compact.launch.err",
    }

    try:
        plist_dir.mkdir(parents=True, exist_ok=True)
        with open(plist_path, "wb") as f:
            plistlib.dump(plist_data, f)
        rprint(f"[green]✔ Plist file created at: {plist_path}[/green]")
        # Unload if it is already loaded
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        subprocess.run(["launchctl", "load", str(plist_path)], check=True)
        rprint(
            f"[green]✔ Auto-compaction scheduled successfully via launchctl every {interval} minutes.[/green]"
        )
    except Exception as e:
        rprint(f"[red]Error scheduling compaction service: {e}[/red]")


@service_app.command("unschedule-compaction")
def service_unschedule_compaction() -> None:
    """Stop and remove the automatic compaction Launch Agent plist."""
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.awareness.compact.plist"

    if plist_path.exists():
        try:
            subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
            rprint("[green]✔ Compaction service unloaded via launchctl.[/green]")
            plist_path.unlink()
            rprint("[green]✔ Plist file removed.[/green]")
        except Exception as e:
            rprint(f"[red]Error unscheduling compaction service: {e}[/red]")
    else:
        rprint("[yellow]Compaction service plist file not found.[/yellow]")


def _is_cloud_path(p: Any) -> bool:
    if not p:
        return False
    p_str = str(p)
    return p_str.startswith(("s3://", "s3a://", "gs://", "gcs://"))


def _get_path_size(p: Path | str | None) -> tuple[int, int]:
    if p is None:
        return 0, 0
    try:
        path = Path(p)
        if not path.exists():
            return 0, 0
        if path.is_file():
            return path.stat().st_size, 1
        total_size = 0
        file_count = 0
        for root, dirs, files in os.walk(path):
            for f in files:
                fp = Path(root) / f
                try:
                    if fp.is_file() and not fp.is_symlink():
                        total_size += fp.stat().st_size
                        file_count += 1
                except Exception:
                    pass
        return total_size, file_count
    except Exception:
        return 0, 0


def _format_size(size_bytes: int) -> str:
    if size_bytes == 0:
        return "0 B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    import math

    try:
        i = int(math.floor(math.log(size_bytes, 1024)))
        p = math.pow(1024, i)
        s = round(size_bytes / p, 2)
        return f"{s} {size_name[i]}"
    except Exception:
        return f"{size_bytes} B"


def _format_duration(seconds: float) -> str:
    """Human-readable duration for compact --status age columns."""
    s = max(0.0, float(seconds))
    if s < 60:
        return f"{int(s)}s"
    if s < 3600:
        return f"{int(s // 60)}m"
    if s < 86400:
        hours = int(s // 3600)
        mins = int((s % 3600) // 60)
        return f"{hours}h{mins:02d}m" if mins else f"{hours}h"
    days = int(s // 86400)
    hours = int((s % 86400) // 3600)
    return f"{days}d{hours}h" if hours else f"{days}d"


def _query_db_metrics(state: StateDB) -> dict[str, Any]:
    from sqlalchemy import func, select

    from awareness.storage.state import DedupNearRow, DedupRow, DLQRow, JobRow, ManifestRow, TaskRow

    stats_dict = {
        "total_bytes_processed": 0,
        "total_docs_emitted": 0,
        "total_docs_dedup_dropped": 0,
        "jobs_count": 0,
        "tasks_count": 0,
        "tasks_by_status": {},
        "dedup_content_count": 0,
        "dedup_near_count": 0,
        "manifests_count": 0,
        "manifests_compacted_count": 0,
        "dlq_count": 0,
    }

    try:
        with state.session() as s:
            job_agg = s.execute(
                select(
                    func.count(JobRow.job_id),
                    func.sum(JobRow.bytes_processed),
                    func.sum(JobRow.docs_emitted),
                    func.sum(JobRow.docs_dedup_dropped),
                )
            ).first()
            if job_agg:
                stats_dict["jobs_count"] = job_agg[0] or 0
                stats_dict["total_bytes_processed"] = job_agg[1] or 0
                stats_dict["total_docs_emitted"] = job_agg[2] or 0
                stats_dict["total_docs_dedup_dropped"] = job_agg[3] or 0

            task_rows = s.execute(
                select(TaskRow.status, func.count(TaskRow.task_id)).group_by(TaskRow.status)
            ).all()
            stats_dict["tasks_count"] = sum(count for status, count in task_rows)
            stats_dict["tasks_by_status"] = {status: count for status, count in task_rows}

            stats_dict["dedup_content_count"] = s.scalar(select(func.count(DedupRow.content_hash))) or 0
            stats_dict["dedup_near_count"] = s.scalar(select(func.count(DedupNearRow.id))) or 0

            manifest_rows = s.execute(
                select(ManifestRow.compacted_at.isnot(None), func.count(ManifestRow.id)).group_by(
                    ManifestRow.compacted_at.isnot(None)
                )
            ).all()
            for is_compacted, count in manifest_rows:
                if is_compacted:
                    stats_dict["manifests_compacted_count"] = count
                else:
                    stats_dict["manifests_count"] = count

            stats_dict["dlq_count"] = s.scalar(select(func.count(DLQRow.id))) or 0
    except Exception as e:
        logger.error(f"Error querying state DB metrics: {e}")

    return stats_dict


@app.command()
def status(
    detailed: bool = typer.Option(
        False, "--detailed", "-d", help="Show detailed storage sizes and DB record counts"
    ),
) -> None:
    """Show overall system status: tail + recent jobs."""
    state, _ = _bootstrap()
    settings = get_settings()
    # Clear phantom RUNNING tail jobs left by crashed processes.
    try:
        state.reconcile_orphan_tail_jobs()
    except Exception:
        pass
    jobs = state.list_jobs(limit=10)

    rprint("[bold]Recent Jobs:[/bold]")
    table = Table("job_id", "kind", "status", "tasks", "docs", "dedup_dropped", "started")
    for j in jobs:
        table.add_row(
            j.job_id,
            j.kind.value,
            j.status.value,
            f"{j.tasks_completed}/{j.tasks_total}",
            str(j.docs_emitted),
            str(j.docs_dedup_dropped),
            j.started_at.isoformat() if j.started_at else "-",
        )
    console.print(table)
    rprint(f"[bold]Tail DB Status:[/bold] {state.get_tail()}")

    rprint("\n[bold]Service Status:[/bold]")
    api_port = _default_api_port()
    pid = _get_api_pid()
    if pid:
        rprint(f"  API Server:  [green]RUNNING[/green] (PID {pid}) on http://127.0.0.1:{api_port}")
    elif _is_port_active("127.0.0.1", api_port):
        rprint(f"  API Server:  [green]RUNNING[/green] (Port {api_port} active, managed externally)")
    else:
        rprint("  API Server:  [red]STOPPED[/red]")

    tail_info = state.get_tail()
    if tail_info.get("running"):
        rprint(f"  Live Tail:   [green]ACTIVE[/green] (Job: {tail_info.get('job_id')})")
    else:
        rprint("  Live Tail:   [red]INACTIVE[/red]")

    # Query database and storage metrics
    db_metrics = _query_db_metrics(state)

    # Calculate disk sizes
    total_local_bytes = 0
    total_local_files = 0
    dirs_to_check = {
        "Staging JSONL": settings.staging_jsonl_dir(),
        "Iceberg Warehouse": settings.iceberg_warehouse,
        "SQLite State DB": settings.data_dir / "state" if settings.data_dir else None,
        "DuckDB Metadata": settings.duckdb_path(),
    }
    for label, path in dirs_to_check.items():
        if path and not _is_cloud_path(path):
            size, count = _get_path_size(path)
            total_local_bytes += size
            total_local_files += count

    rprint("\n[bold]Ingested Data & Disk Usage:[/bold]")
    rprint(
        f"  • Total Raw Data Processed:     [green]{_format_size(db_metrics['total_bytes_processed'])}[/green] ({db_metrics['total_bytes_processed']:,} bytes)"
    )
    rprint(
        f"  • Total Space Occupied on Disk:  [bold green]{_format_size(total_local_bytes)}[/bold green] ({total_local_bytes:,} bytes)"
    )
    rprint(f"  • Total Unique Docs Ingested:    [bold]{db_metrics['total_docs_emitted']:,}[/bold]")
    if db_metrics["total_docs_emitted"] + db_metrics["total_docs_dedup_dropped"] > 0:
        total_docs = db_metrics["total_docs_emitted"] + db_metrics["total_docs_dedup_dropped"]
        dedup_ratio = (db_metrics["total_docs_dedup_dropped"] / total_docs) * 100
        rprint(
            f"  • Ingestion Deduplication Ratio: [cyan]{dedup_ratio:.2f}%[/cyan] ({db_metrics['total_docs_dedup_dropped']:,} docs dropped)"
        )

    # Staging backlog age (same summary as compact --status / GET /staging).
    try:
        staging = state.pending_manifest_summary()
    except Exception:
        staging = None
    if staging is not None:
        pending_n = int(staging.get("pending_count") or 0)
        if pending_n:
            age = staging.get("oldest_age_seconds")
            age_s = _format_duration(float(age)) if age is not None else "—"
            recs = int(staging.get("total_records") or 0)
            b = int(staging.get("total_bytes") or 0)
            rprint(
                f"  • Staging pending compaction:   [yellow]{pending_n:,}[/yellow] "
                f"manifests · {recs:,} rows · {_format_size(b)} · oldest {age_s}"
            )
        else:
            rprint("  • Staging pending compaction:   [green]0[/green] (caught up)")

    if detailed:
        rprint("\n[bold]Detailed Component Sizes:[/bold]")
        det_table = Table("Component", "Files", "Size on Disk")
        for label, path in dirs_to_check.items():
            if path and not _is_cloud_path(path):
                size, count = _get_path_size(path)
                det_table.add_row(label, f"{count:,}", _format_size(size))
            else:
                det_table.add_row(label, "-", "[blue]Cloud URI[/blue]")

        # Also add Cache, WARC, DLQ, Logs
        other_dirs = {
            "Checkpoints": settings.checkpoint_dir,
            "Cache": settings.cache_dir,
            "WARC Cache": settings.warc_cache_dir,
            "Dead Letter Queue": settings.dlq_dir(),
            "Logs": settings.log_dir,
        }
        for label, path in other_dirs.items():
            if path and not _is_cloud_path(path):
                size, count = _get_path_size(path)
                det_table.add_row(label, f"{count:,}", _format_size(size))

        console.print(det_table)


@app.command(name="dedup-stats")
def dedup_stats() -> None:
    """Print dedup index statistics (index rows + process skip counters).

    Matches GET /dedup-stats: durable SQLite dedup index counts plus live
    process metrics for URL fetch-gate skips and tight near-dup drops.
    """
    state, _ = _bootstrap()
    stats: dict[str, Any] = dict(state.dedup_stats())
    m = get_metrics()
    stats["fetch_skipped_seen"] = int(m.counter_sum("tail.fetch_skipped_seen"))
    stats["tight_near_skipped"] = int(m.counter_sum("dedup.tight_near_skipped"))
    print(json.dumps(stats, indent=2))


@app.command(name="stats")
def stats(
    json_format: bool = typer.Option(False, "--json", help="Output stats in raw JSON format"),
    detailed: bool = typer.Option(True, "--detailed/--summary", help="Show deep disk storage breakdowns"),
) -> None:
    """Print detailed storage, database, and ingestion performance statistics."""
    state, _ = _bootstrap()
    settings = get_settings()

    # 1. Query State DB
    db_metrics = _query_db_metrics(state)

    # 2. Disk sizes (unless cloud)
    local_storage = {}

    dirs_to_check = {
        "Staging JSONL (staging_jsonl_dir)": settings.staging_jsonl_dir(),
        "Iceberg Warehouse (iceberg_warehouse)": settings.iceberg_warehouse,
        "DuckDB Metadata (duckdb_path)": settings.duckdb_path(),
        "SQLite State DB (state_db_url)": settings.data_dir / "state" if settings.data_dir else None,
        "Checkpoints (checkpoint_dir)": settings.checkpoint_dir,
        "Cache (cache_dir)": settings.cache_dir,
        "WARC Cache (warc_cache_dir)": settings.warc_cache_dir,
        "Dead Letter Queue (dlq_dir)": settings.dlq_dir(),
        "Logs (log_dir)": settings.log_dir,
    }

    total_local_bytes = 0
    total_local_files = 0

    for label, path in dirs_to_check.items():
        if path and not _is_cloud_path(path):
            size, count = _get_path_size(path)
            local_storage[label] = {"path": str(path), "size": size, "files": count}
            total_local_bytes += size
            total_local_files += count
        else:
            local_storage[label] = {"path": str(path), "size": 0, "files": 0, "cloud": True}

    # Calculate Dedup Efficiency
    total_docs = db_metrics["total_docs_emitted"] + db_metrics["total_docs_dedup_dropped"]
    dedup_ratio = 0.0
    if total_docs > 0:
        dedup_ratio = (db_metrics["total_docs_dedup_dropped"] / total_docs) * 100.0

    metrics_data = {
        "database": db_metrics,
        "storage": {
            "breakdown": local_storage,
            "total_local_bytes": total_local_bytes,
            "total_local_files": total_local_files,
        },
        "performance": {
            "total_docs": total_docs,
            "dedup_ratio_pct": round(dedup_ratio, 2),
        },
    }

    if json_format:
        print(json.dumps(metrics_data, indent=2))
        return

    # Standard terminal output using rich
    console.print(banner.render_banner())
    rprint(f"[{banner.C_LINE}]════════════════════════════════════════════════════════════════[/]")
    rprint(f"[bold {banner.C_HI}]       AWARENESS ENGINE — INGESTION & STORAGE PERFORMANCE       [/]")
    rprint(f"[{banner.C_LINE}]════════════════════════════════════════════════════════════════[/]\n")

    # Ingestion Volume Section
    rprint("[bold white]1. Ingestion Volume & Performance[/bold white]")
    rprint(
        f"  • Total Raw Data Processed (Uncompressed): [green]{_format_size(db_metrics['total_bytes_processed'])}[/green] ({db_metrics['total_bytes_processed']:,} bytes)"
    )
    rprint(
        f"  • Total Unique Documents Emitted:        [bold green]{db_metrics['total_docs_emitted']:,}[/bold green]"
    )
    rprint(
        f"  • Deduplication Dropped Documents:      [yellow]{db_metrics['total_docs_dedup_dropped']:,}[/yellow]"
    )
    rprint(f"  • Total Ingested Document Volume:        [bold]{total_docs:,}[/bold]")
    rprint(f"  • Ingestion Deduplication Ratio:         [cyan]{dedup_ratio:.2f}%[/cyan]")
    rprint()

    # State DB Summary
    rprint("[bold white]2. State Database Statistics (SQLite)[/bold white]")
    rprint(f"  • Total Jobs Run:                        [cyan]{db_metrics['jobs_count']}[/cyan]")
    rprint(f"  • Total Subtasks Created:                [cyan]{db_metrics['tasks_count']}[/cyan]")
    for status_val, count in db_metrics["tasks_by_status"].items():
        rprint(f"    - {status_val.upper()}: {' ' * (20 - len(status_val))}{count}")
    rprint(f"  • Exact Duplicate Content Index Rows:   [cyan]{db_metrics['dedup_content_count']:,}[/cyan]")
    rprint(f"  • Near-Duplicate Simhash Segment Rows:  [cyan]{db_metrics['dedup_near_count']:,}[/cyan]")
    rprint(
        f"  • Total Manifests Tracked:               [cyan]{db_metrics['manifests_count'] + db_metrics['manifests_compacted_count']}[/cyan] ({db_metrics['manifests_compacted_count']} compacted)"
    )
    rprint(f"  • Dead Letter Queue (DLQ) Rows:         [red]{db_metrics['dlq_count']}[/red]")
    rprint()

    # Disk Storage Summary
    rprint("[bold white]3. Local Disk Storage & Directory Sizes[/bold white]")
    rprint(f"  • Total Storage Directory:              [yellow]{settings.data_dir}[/yellow]")
    rprint(f"  • Total Local Files Managed:            [bold]{total_local_files:,}[/bold]")
    rprint(
        f"  • Total Space Occupied on Disk:          [bold green]{_format_size(total_local_bytes)}[/bold green]"
    )
    rprint()

    if detailed:
        table = Table(title="Storage Directory Breakdown", show_header=True, header_style="bold magenta")
        table.add_column("Directory / Component", style="cyan")
        table.add_column("Files", justify="right", style="green")
        table.add_column("Size on Disk", justify="right", style="bold green")
        table.add_column("Path / Configuration", style="white")

        for name, data in local_storage.items():
            if data.get("cloud"):
                table.add_row(name, "-", "[blue]Cloud URI (N/A)[/blue]", data["path"])
            else:
                table.add_row(
                    name.split(" (")[0], f"{data['files']:,}", _format_size(data["size"]), data["path"]
                )
        console.print(table)
        rprint()

        # Calculate compression/compaction efficiency
        if total_local_bytes > 0:
            compression_ratio = db_metrics["total_bytes_processed"] / total_local_bytes
            rprint(
                f"  [bold]Storage Efficiency Ratio[/bold] (Raw Size / Disk Size): [bold green]{compression_ratio:.2f}x[/bold green]"
            )
            rprint(
                "  [dim]*Higher is better. Values > 1.0 indicate efficient storage compression/deduplication.[/dim]\n"
            )


@app.command()
def metrics(
    format: str | None = typer.Option(
        None,
        "--format",
        "-f",
        help=(
            "Output format: table, json, or prometheus/prom/text. "
            "Default: table on a TTY, json when piped (script-safe)."
        ),
    ),
    limit: int = typer.Option(
        40,
        "--limit",
        "-n",
        help="Max rows per section in table mode (counters/gauges/histograms).",
    ),
    prefix: str = typer.Option(
        "",
        "--prefix",
        "-p",
        help=(
            "Only include metrics whose name starts with this prefix "
            "(e.g. 'http.', 'gdelt.', 'jsonl.'). Applies to all formats."
        ),
    ),
) -> None:
    """Show in-process metrics (table, JSON snapshot, or Prometheus text).

    Interactive terminals default to a compact Rich table. Piped/non-TTY
    stdout defaults to JSON (backward-compatible for scripts). Force either
    with ``--format``; use ``prometheus`` for the same exposition as
    ``GET /metrics?format=prometheus``. Pass ``--prefix`` to narrow output
    (useful when scraping a single subsystem).
    """
    if format is None:
        fmt = "table" if sys.stdout.isatty() else "json"
    else:
        fmt = format.strip().lower()
    m = get_metrics()
    pfx = prefix.strip() or None
    if fmt in ("prometheus", "prom", "text", "exposition"):
        # Trailing newline already included by render_prometheus.
        sys.stdout.write(m.render_prometheus(prefix=pfx))
        return
    if fmt in ("json", "snapshot", "raw"):
        print(json.dumps(m.snapshot(prefix=pfx), indent=2))
        return
    if fmt not in ("table", "human", "pretty", "tui"):
        raise typer.BadParameter(f"Unknown --format {format!r}; use table, json, or prometheus")
    _print_metrics_table(m.snapshot(prefix=pfx), limit=max(1, int(limit)))


def _format_metric_duration(sec: float) -> str:
    """Format histogram latency seconds for table mode (ms when sub-second)."""
    try:
        v = float(sec)
    except (TypeError, ValueError):
        return "—"
    if v != v or v < 0:  # NaN / negative
        return "—"
    if v < 0.001:
        return f"{v * 1_000_000:.0f}µs"
    if v < 1.0:
        return f"{v * 1000:.0f}ms"
    if v < 10.0:
        return f"{v:.2f}s"
    return f"{v:.1f}s"


def _hist_is_seconds(name: str) -> bool:
    n = (name or "").lower()
    return n.endswith("_seconds") or n.endswith(".seconds") or "seconds" in n


def summarize_fineweb_metrics_table(snap: dict[str, Any]) -> dict[str, Any] | None:
    """Aggregate FineWeb process metrics for the CLI table summary strip.

    Returns None when the snapshot has no ``fineweb.*`` series so callers can
    skip the strip entirely.
    """
    counters = list(snap.get("counters") or [])
    histograms = list(snap.get("histograms") or [])
    admitted = 0.0
    filtered = 0.0
    seen = 0.0
    load_attempts = 0.0
    load_ok = 0.0
    by_reason: dict[str, float] = {}
    has_fineweb = False
    for c in counters:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "")
        if not name.startswith("fineweb."):
            continue
        has_fineweb = True
        val = float(c.get("value") or 0)
        labels = c.get("labels") or {}
        if name == "fineweb.rows_admitted":
            admitted += val
        elif name == "fineweb.rows_filtered":
            filtered += val
            reason = str(labels.get("reason") or "unknown")
            by_reason[reason] = by_reason.get(reason, 0.0) + val
        elif name == "fineweb.rows_seen":
            seen += val
        elif name == "fineweb.load_attempts":
            load_attempts += val
            if labels.get("outcome") == "ok":
                load_ok += val
    weighted_p95 = 0.0
    hist_count = 0
    for h in histograms:
        if not isinstance(h, dict):
            continue
        name = str(h.get("name") or "")
        if not name.startswith("fineweb."):
            continue
        has_fineweb = True
        if name != "fineweb.load_seconds":
            continue
        n = int(h.get("count") or 0)
        if n <= 0:
            continue
        p95 = float(h.get("p95") or 0.0)
        hist_count += n
        weighted_p95 += p95 * n
    if not has_fineweb:
        return None
    top_reason = None
    if by_reason:
        top_reason = max(by_reason.items(), key=lambda kv: kv[1])[0]
    return {
        "admitted": int(admitted),
        "filtered": int(filtered),
        "seen": int(seen),
        "load_attempts": int(load_attempts),
        "load_ok": int(load_ok),
        "load_p95": (weighted_p95 / hist_count) if hist_count else None,
        "top_filter": top_reason,
    }


def format_fineweb_summary_line(summary: dict[str, Any]) -> str:
    """Render a single operator-facing FineWeb summary line (no Rich markup)."""
    bits = [
        f"admitted={summary.get('admitted', 0)}",
        f"filtered={summary.get('filtered', 0)}",
        f"seen={summary.get('seen', 0)}",
    ]
    attempts = int(summary.get("load_attempts") or 0)
    ok = int(summary.get("load_ok") or 0)
    if attempts:
        bits.append(f"load={ok}/{attempts} ok")
    p95 = summary.get("load_p95")
    if p95 is not None:
        bits.append(f"load_p95={_format_metric_duration(float(p95))}")
    top = summary.get("top_filter")
    if top:
        bits.append(f"top_filter={top}")
    return "FineWeb  " + "  ".join(bits)


def summarize_wet_parse_metrics_table(snap: dict[str, Any]) -> dict[str, Any] | None:
    """Aggregate CC-WET download/parse process metrics for the CLI table strip.

    Returns None when the snapshot has no relevant ``cc_wet.*`` series.
    """
    counters = list(snap.get("counters") or [])
    histograms = list(snap.get("histograms") or [])
    records_seen = 0.0
    parse_emitted = 0.0
    download_attempts = 0.0
    download_ok = 0.0
    download_cache_hits = 0.0
    has_wet = False
    for c in counters:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "")
        if name not in (
            "cc_wet.records_seen",
            "cc_wet.shard_parse_emitted",
            "cc_wet.shard_download_attempts",
        ):
            continue
        has_wet = True
        val = float(c.get("value") or 0)
        labels = c.get("labels") or {}
        if name == "cc_wet.records_seen":
            records_seen += val
        elif name == "cc_wet.shard_parse_emitted":
            parse_emitted += val
        elif name == "cc_wet.shard_download_attempts":
            download_attempts += val
            outcome = labels.get("outcome")
            if outcome == "cache_hit":
                download_cache_hits += val
                download_ok += val
            elif outcome == "ok":
                download_ok += val
    parse_weighted = 0.0
    parse_count = 0
    dl_weighted = 0.0
    dl_count = 0
    for h in histograms:
        if not isinstance(h, dict):
            continue
        name = str(h.get("name") or "")
        if name not in (
            "cc_wet.shard_parse_seconds",
            "cc_wet.iter_parse_seconds",
            "cc_wet.shard_download_seconds",
        ):
            continue
        has_wet = True
        n = int(h.get("count") or 0)
        if n <= 0:
            continue
        p95 = float(h.get("p95") or 0.0)
        if name in ("cc_wet.shard_parse_seconds", "cc_wet.iter_parse_seconds"):
            parse_count += n
            parse_weighted += p95 * n
        else:
            dl_count += n
            dl_weighted += p95 * n
    if not has_wet:
        return None
    return {
        "records_seen": int(records_seen),
        "parse_emitted": int(parse_emitted),
        "download_attempts": int(download_attempts),
        "download_ok": int(download_ok),
        "download_cache_hits": int(download_cache_hits),
        "parse_p95": (parse_weighted / parse_count) if parse_count else None,
        "download_p95": (dl_weighted / dl_count) if dl_count else None,
    }


def format_wet_parse_summary_line(summary: dict[str, Any]) -> str:
    """Render a single operator-facing WET parse summary line (no Rich markup)."""
    bits = [
        f"seen={summary.get('records_seen', 0)}",
        f"emitted={summary.get('parse_emitted', 0)}",
    ]
    attempts = int(summary.get("download_attempts") or 0)
    ok = int(summary.get("download_ok") or 0)
    if attempts:
        bits.append(f"download={ok}/{attempts} ok")
        cache = int(summary.get("download_cache_hits") or 0)
        if cache:
            bits.append(f"cache={cache}")
    parse_p95 = summary.get("parse_p95")
    if parse_p95 is not None:
        bits.append(f"parse_p95={_format_metric_duration(float(parse_p95))}")
    dl_p95 = summary.get("download_p95")
    if dl_p95 is not None:
        bits.append(f"dl_p95={_format_metric_duration(float(dl_p95))}")
    return "WET      " + "  ".join(bits)


def summarize_jsonl_sync_metrics_table(snap: dict[str, Any]) -> dict[str, Any] | None:
    """Aggregate JSONL crash-safe sync/orphan metrics for the CLI table strip.

    Returns None when the snapshot has no ``jsonl.sync*`` / orphan series.
    """
    counters = list(snap.get("counters") or [])
    histograms = list(snap.get("histograms") or [])
    gauges = list(snap.get("gauges") or [])
    syncs = 0.0
    sync_ok = 0.0
    orphans_recovered = 0.0
    orphans_removed = 0.0
    has_series = False
    for c in counters:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "")
        if name not in (
            "jsonl.syncs",
            "jsonl.orphans_recovered",
            "jsonl.orphans_removed",
            "jsonl.orphans_recover_errors",
        ):
            continue
        has_series = True
        val = float(c.get("value") or 0)
        labels = c.get("labels") or {}
        if name == "jsonl.syncs":
            syncs += val
            if labels.get("outcome") == "ok":
                sync_ok += val
        elif name == "jsonl.orphans_recovered":
            orphans_recovered += val
        elif name == "jsonl.orphans_removed":
            orphans_removed += val
    sync_weighted = 0.0
    sync_count = 0
    for h in histograms:
        if not isinstance(h, dict):
            continue
        name = str(h.get("name") or "")
        if name != "jsonl.sync_seconds":
            continue
        has_series = True
        n = int(h.get("count") or 0)
        if n <= 0:
            continue
        p95 = float(h.get("p95") or 0.0)
        sync_count += n
        sync_weighted += p95 * n
    open_records = 0.0
    for g in gauges:
        if not isinstance(g, dict):
            continue
        if str(g.get("name") or "") == "jsonl.open_records":
            has_series = True
            open_records = float(g.get("value") or 0)
            break
    if not has_series:
        return None
    return {
        "syncs": int(syncs),
        "sync_ok": int(sync_ok),
        "sync_p95": (sync_weighted / sync_count) if sync_count else None,
        "orphans_recovered": int(orphans_recovered),
        "orphans_removed": int(orphans_removed),
        "open_records": int(open_records),
    }


def format_jsonl_sync_summary_line(summary: dict[str, Any]) -> str:
    """Render a single operator-facing JSONL sync summary line (no Rich markup)."""
    bits: list[str] = []
    syncs = int(summary.get("syncs") or 0)
    ok = int(summary.get("sync_ok") or 0)
    if syncs:
        bits.append(f"sync={ok}/{syncs} ok")
    p95 = summary.get("sync_p95")
    if p95 is not None:
        bits.append(f"sync_p95={_format_metric_duration(float(p95))}")
    open_recs = int(summary.get("open_records") or 0)
    if open_recs:
        bits.append(f"open={open_recs}")
    recovered = int(summary.get("orphans_recovered") or 0)
    removed = int(summary.get("orphans_removed") or 0)
    if recovered or removed:
        bits.append(f"orphans={recovered} recovered/{removed} removed")
    if not bits:
        bits.append("idle")
    return "JSONL    " + "  ".join(bits)


def summarize_fts_metrics_table(snap: dict[str, Any]) -> dict[str, Any] | None:
    """Aggregate FTS build-path metrics for the CLI table summary strip.

    Returns None when the snapshot has no ``fts.*`` series.
    """
    counters = list(snap.get("counters") or [])
    histograms = list(snap.get("histograms") or [])
    gauges = list(snap.get("gauges") or [])
    builds = 0.0
    full = 0.0
    incremental = 0.0
    restore = 0.0
    errors = 0.0
    has_fts = False
    for c in counters:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "")
        if name not in ("fts.builds", "fts.build_errors"):
            continue
        has_fts = True
        val = float(c.get("value") or 0)
        labels = c.get("labels") or {}
        if name == "fts.builds":
            builds += val
            mode = labels.get("mode")
            if mode == "full":
                full += val
            elif mode == "incremental":
                incremental += val
            elif mode == "restore":
                restore += val
        elif name == "fts.build_errors":
            errors += val
    weighted_p95 = 0.0
    hist_count = 0
    for h in histograms:
        if not isinstance(h, dict):
            continue
        if str(h.get("name") or "") != "fts.build_seconds":
            continue
        has_fts = True
        labels = h.get("labels") or {}
        if labels.get("outcome") == "error":
            continue
        n = int(h.get("count") or 0)
        if n <= 0:
            continue
        p95 = float(h.get("p95") or 0.0)
        hist_count += n
        weighted_p95 += p95 * n
    indexed_rows = 0.0
    for g in gauges:
        if not isinstance(g, dict):
            continue
        if str(g.get("name") or "") == "fts.indexed_rows":
            has_fts = True
            indexed_rows = float(g.get("value") or 0)
            break
    if not has_fts:
        return None
    return {
        "builds": int(builds),
        "full": int(full),
        "incremental": int(incremental),
        "restore": int(restore),
        "errors": int(errors),
        "build_p95": (weighted_p95 / hist_count) if hist_count else None,
        "indexed_rows": int(indexed_rows),
    }


def format_fts_summary_line(summary: dict[str, Any]) -> str:
    """Render a single operator-facing FTS summary line (no Rich markup)."""
    bits: list[str] = []
    builds = int(summary.get("builds") or 0)
    if builds:
        bits.append(f"builds={builds}")
        full = int(summary.get("full") or 0)
        incr = int(summary.get("incremental") or 0)
        restore = int(summary.get("restore") or 0)
        mode_bits = []
        if full:
            mode_bits.append(f"full={full}")
        if incr:
            mode_bits.append(f"incr={incr}")
        if restore:
            mode_bits.append(f"restore={restore}")
        if mode_bits:
            bits.append(" ".join(mode_bits))
    p95 = summary.get("build_p95")
    if p95 is not None:
        bits.append(f"p95={_format_metric_duration(float(p95))}")
    rows = int(summary.get("indexed_rows") or 0)
    if rows:
        bits.append(f"rows={rows}")
    errors = int(summary.get("errors") or 0)
    if errors:
        bits.append(f"errors={errors}")
    if not bits:
        bits.append("idle")
    return "FTS      " + "  ".join(bits)


def summarize_warc_repair_metrics_table(snap: dict[str, Any]) -> dict[str, Any] | None:
    """Aggregate WARC range-repair metrics for the CLI table summary strip.

    Returns None when the snapshot has no ``warc_repair.*`` series.
    """
    counters = list(snap.get("counters") or [])
    histograms = list(snap.get("histograms") or [])
    docs_emitted = 0.0
    fetch_attempts = 0.0
    fetch_ok = 0.0
    fetch_http_error = 0.0
    fetch_network_error = 0.0
    parse_attempts = 0.0
    parse_emitted = 0.0
    parse_empty = 0.0
    has_warc = False
    for c in counters:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "")
        if name not in (
            "warc_repair.docs_emitted",
            "warc_repair.fetch_attempts",
            "warc_repair.parse_attempts",
        ):
            continue
        has_warc = True
        val = float(c.get("value") or 0)
        labels = c.get("labels") or {}
        outcome = labels.get("outcome")
        if name == "warc_repair.docs_emitted":
            docs_emitted += val
        elif name == "warc_repair.fetch_attempts":
            fetch_attempts += val
            if outcome == "ok":
                fetch_ok += val
            elif outcome == "http_error":
                fetch_http_error += val
            elif outcome == "network_error":
                fetch_network_error += val
        elif name == "warc_repair.parse_attempts":
            parse_attempts += val
            if outcome == "emitted":
                parse_emitted += val
            elif outcome == "empty":
                parse_empty += val
    fetch_weighted = 0.0
    fetch_count = 0
    parse_weighted = 0.0
    parse_count = 0
    for h in histograms:
        if not isinstance(h, dict):
            continue
        name = str(h.get("name") or "")
        if name not in ("warc_repair.fetch_seconds", "warc_repair.parse_seconds"):
            continue
        has_warc = True
        n = int(h.get("count") or 0)
        if n <= 0:
            continue
        p95 = float(h.get("p95") or 0.0)
        if name == "warc_repair.fetch_seconds":
            fetch_count += n
            fetch_weighted += p95 * n
        else:
            parse_count += n
            parse_weighted += p95 * n
    if not has_warc:
        return None
    return {
        "docs_emitted": int(docs_emitted),
        "fetch_attempts": int(fetch_attempts),
        "fetch_ok": int(fetch_ok),
        "fetch_http_error": int(fetch_http_error),
        "fetch_network_error": int(fetch_network_error),
        "parse_attempts": int(parse_attempts),
        "parse_emitted": int(parse_emitted),
        "parse_empty": int(parse_empty),
        "fetch_p95": (fetch_weighted / fetch_count) if fetch_count else None,
        "parse_p95": (parse_weighted / parse_count) if parse_count else None,
    }


def format_warc_repair_summary_line(summary: dict[str, Any]) -> str:
    """Render a single operator-facing WARC repair summary line (no Rich markup)."""
    bits: list[str] = []
    docs = int(summary.get("docs_emitted") or 0)
    if docs:
        bits.append(f"docs={docs}")
    attempts = int(summary.get("fetch_attempts") or 0)
    ok = int(summary.get("fetch_ok") or 0)
    if attempts:
        bits.append(f"fetch={ok}/{attempts} ok")
        http_err = int(summary.get("fetch_http_error") or 0)
        net_err = int(summary.get("fetch_network_error") or 0)
        if http_err:
            bits.append(f"http_err={http_err}")
        if net_err:
            bits.append(f"net_err={net_err}")
    fetch_p95 = summary.get("fetch_p95")
    if fetch_p95 is not None:
        bits.append(f"fetch_p95={_format_metric_duration(float(fetch_p95))}")
    parse_p95 = summary.get("parse_p95")
    if parse_p95 is not None:
        bits.append(f"parse_p95={_format_metric_duration(float(parse_p95))}")
    empty = int(summary.get("parse_empty") or 0)
    if empty:
        bits.append(f"empty={empty}")
    if not bits:
        bits.append("idle")
    return "WARC     " + "  ".join(bits)


def summarize_task_metrics_table(snap: dict[str, Any]) -> dict[str, Any] | None:
    """Aggregate worker task duration/failure metrics for the CLI table strip.

    Returns None when the snapshot has no ``tasks.*`` series.
    """
    counters = list(snap.get("counters") or [])
    histograms = list(snap.get("histograms") or [])
    completed = 0.0
    failed = 0.0
    retry = 0.0
    dead_letter = 0.0
    no_adapter = 0.0
    has_tasks = False
    for c in counters:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "")
        if name not in ("tasks.completed", "tasks.failed"):
            continue
        has_tasks = True
        val = float(c.get("value") or 0)
        labels = c.get("labels") or {}
        outcome = labels.get("outcome")
        if name == "tasks.completed":
            completed += val
        elif name == "tasks.failed":
            failed += val
            if outcome == "retry":
                retry += val
            elif outcome == "dead_letter":
                dead_letter += val
            elif outcome == "no_adapter":
                no_adapter += val
    weighted_p95 = 0.0
    hist_count = 0
    for h in histograms:
        if not isinstance(h, dict):
            continue
        if str(h.get("name") or "") != "tasks.duration_seconds":
            continue
        has_tasks = True
        n = int(h.get("count") or 0)
        if n <= 0:
            continue
        p95 = float(h.get("p95") or 0.0)
        hist_count += n
        weighted_p95 += p95 * n
    if not has_tasks:
        return None
    return {
        "completed": int(completed),
        "failed": int(failed),
        "retry": int(retry),
        "dead_letter": int(dead_letter),
        "no_adapter": int(no_adapter),
        "duration_p95": (weighted_p95 / hist_count) if hist_count else None,
    }


def format_task_summary_line(summary: dict[str, Any]) -> str:
    """Render a single operator-facing task metrics summary line (no Rich markup)."""
    bits: list[str] = []
    completed = int(summary.get("completed") or 0)
    if completed:
        bits.append(f"done={completed}")
    failed = int(summary.get("failed") or 0)
    if failed:
        bits.append(f"fail={failed}")
        retry = int(summary.get("retry") or 0)
        dead = int(summary.get("dead_letter") or 0)
        no_adapter = int(summary.get("no_adapter") or 0)
        detail: list[str] = []
        if retry:
            detail.append(f"retry={retry}")
        if dead:
            detail.append(f"dead={dead}")
        if no_adapter:
            detail.append(f"no_adapter={no_adapter}")
        if detail:
            bits.append(" ".join(detail))
    p95 = summary.get("duration_p95")
    if p95 is not None:
        bits.append(f"p95={_format_metric_duration(float(p95))}")
    if not bits:
        bits.append("idle")
    return "TASKS    " + "  ".join(bits)


def _print_metrics_table(snap: dict[str, Any], *, limit: int = 40) -> None:
    """Render a human-readable metrics summary (uptime + top series)."""
    uptime = float(snap.get("uptime_seconds") or 0.0)
    hours, rem = divmod(int(uptime), 3600)
    minutes, seconds = divmod(rem, 60)
    pfx = snap.get("prefix")
    pfx_note = f"  prefix={pfx!r}" if pfx else ""
    console.print(
        f"[bold]Metrics[/bold]  uptime={hours:d}h {minutes:02d}m {seconds:02d}s"
        f"{pfx_note}  "
        f"([dim]--format json|prometheus · --prefix name.[/dim])"
    )

    fineweb_summary = summarize_fineweb_metrics_table(snap)
    if fineweb_summary is not None:
        console.print(f"[bold cyan]{escape(format_fineweb_summary_line(fineweb_summary))}[/bold cyan]")
    wet_summary = summarize_wet_parse_metrics_table(snap)
    if wet_summary is not None:
        console.print(f"[bold cyan]{escape(format_wet_parse_summary_line(wet_summary))}[/bold cyan]")
    jsonl_summary = summarize_jsonl_sync_metrics_table(snap)
    if jsonl_summary is not None:
        console.print(f"[bold cyan]{escape(format_jsonl_sync_summary_line(jsonl_summary))}[/bold cyan]")
    fts_summary = summarize_fts_metrics_table(snap)
    if fts_summary is not None:
        console.print(f"[bold cyan]{escape(format_fts_summary_line(fts_summary))}[/bold cyan]")
    warc_summary = summarize_warc_repair_metrics_table(snap)
    if warc_summary is not None:
        console.print(f"[bold cyan]{escape(format_warc_repair_summary_line(warc_summary))}[/bold cyan]")
    task_summary = summarize_task_metrics_table(snap)
    if task_summary is not None:
        console.print(f"[bold cyan]{escape(format_task_summary_line(task_summary))}[/bold cyan]")

    counters = list(snap.get("counters") or [])
    gauges = list(snap.get("gauges") or [])
    histograms = list(snap.get("histograms") or [])

    def _lbl(labels: Any) -> str:
        if not labels:
            return ""
        if isinstance(labels, dict) and labels:
            return " " + ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return ""

    ct = Table(title=f"Counters ({min(limit, len(counters))}/{len(counters)})", show_lines=False)
    ct.add_column("name", style=banner.C_HI)
    ct.add_column("labels", style=banner.C_DIM)
    ct.add_column("value", justify="right")
    # Highest values first — operators care about hot paths.
    for row in sorted(counters, key=lambda r: float(r.get("value") or 0), reverse=True)[:limit]:
        ct.add_row(
            str(row.get("name") or ""),
            _lbl(row.get("labels")).strip(),
            f"{float(row.get('value') or 0):g}",
        )
    if counters:
        console.print(ct)
    else:
        console.print("[dim]No counters yet (process idle or freshly started).[/dim]")

    gt = Table(title=f"Gauges ({min(limit, len(gauges))}/{len(gauges)})", show_lines=False)
    gt.add_column("name", style=banner.C_HI)
    gt.add_column("labels", style=banner.C_DIM)
    gt.add_column("value", justify="right")
    for row in sorted(gauges, key=lambda r: str(r.get("name") or ""))[:limit]:
        gt.add_row(
            str(row.get("name") or ""),
            _lbl(row.get("labels")).strip(),
            f"{float(row.get('value') or 0):g}",
        )
    if gauges:
        console.print(gt)

    ht = Table(
        title=f"Histograms ({min(limit, len(histograms))}/{len(histograms)})",
        show_lines=False,
    )
    ht.add_column("name", style=banner.C_HI)
    ht.add_column("labels", style=banner.C_DIM)
    ht.add_column("count", justify="right")
    ht.add_column("p50", justify="right")
    ht.add_column("p95", justify="right")
    ht.add_column("p99", justify="right")
    ht.add_column("avg", justify="right")
    for row in sorted(histograms, key=lambda r: int(r.get("count") or 0), reverse=True)[:limit]:
        name = str(row.get("name") or "")
        if _hist_is_seconds(name):
            p50_s = _format_metric_duration(float(row.get("p50") or 0))
            p95_s = _format_metric_duration(float(row.get("p95") or 0))
            p99_s = _format_metric_duration(float(row.get("p99") or 0))
            avg_s = _format_metric_duration(float(row.get("avg") or 0))
        else:
            p50_s = f"{float(row.get('p50') or 0):.4g}"
            p95_s = f"{float(row.get('p95') or 0):.4g}"
            p99_s = f"{float(row.get('p99') or 0):.4g}"
            avg_s = f"{float(row.get('avg') or 0):.4g}"
        ht.add_row(
            name,
            _lbl(row.get("labels")).strip(),
            str(int(row.get("count") or 0)),
            p50_s,
            p95_s,
            p99_s,
            avg_s,
        )
    if histograms:
        console.print(ht)
    else:
        console.print("[dim]No histograms yet — HTTP fetch latency appears after the first GET.[/dim]")


# ── backfill ────────────────────────────────────────────────────────────
@backfill_app.command("submit")
def backfill_submit(
    start: str = typer.Option(..., "--start", help="Start date (ISO or yyyy-mm-dd)"),
    end: str = typer.Option("now", "--end", help="End date (ISO, yyyy-mm-dd, or 'now')"),
    sources: list[str] = typer.Option(  # noqa: B008
        [],
        "--source",
        "-s",
        help=(
            "Restrict to specific source kinds. Repeat. Accepted spellings "
            "(case-insensitive, -/_ interchangeable): common_crawl_wet (cc_wet, "
            "wet, CC-WET), fineweb (fw, FineWeb), gdelt, rss, sitemap, "
            "common_crawl_index, common_crawl_warc, fineweb_2, atom, tail_recrawl."
        ),
    ),
    domains: list[str] = typer.Option([], "--domain", help="Limit to these domains."),
    languages: list[str] = typer.Option([], "--lang", help="Limit to languages (BCP-47)."),
    max_tasks: int = typer.Option(0, "--max-tasks", help="Cap total tasks for smoke tests."),
    notes: str = typer.Option("", "--note", help="Free-form note."),
    match: list[str] = typer.Option(  # noqa: B008
        [],
        "--match",
        "-m",
        help="Topic filter: keep only docs with this whole word/phrase (case-insensitive). Repeat for OR; use --match-regex for partial/pattern matches.",
    ),
    match_all: bool = typer.Option(
        False, "--match-all", help="Require ALL --match terms (AND) instead of ANY (OR)."
    ),
    match_regex: bool = typer.Option(
        False, "--match-regex", help="Treat --match terms as Python regular expressions."
    ),
    match_field: str = typer.Option("both", "--match-field", help="Where to match: title | text | both."),
) -> None:
    state, planner = _bootstrap()
    src = [_resolve_source_kind(s) for s in sources] if sources else []
    start_dt = to_utc(start)
    if start_dt is None:
        raise typer.BadParameter("Invalid start date format")
    if match_field not in ("title", "text", "both"):
        raise typer.BadParameter("--match-field must be one of: title, text, both")
    req = BackfillRequest(
        start=start_dt,
        end=_coerce_end_checked(end),
        sources=src,
        domains=domains or None,
        languages=languages or None,
        max_tasks=max_tasks or None,
        notes=notes or None,
        match=list(match),
        match_all=match_all,
        match_regex=match_regex,
        match_field=match_field,
    )
    job_id = planner.submit_backfill(req)
    rprint(f"[green]Submitted backfill[/green] job_id=[bold]{job_id}[/bold]")
    if match:
        joiner = " AND " if match_all else " OR "
        rprint(
            f"[dim]Topic filter ({'regex' if match_regex else 'keyword'}, {match_field}): {escape(joiner.join(match))}[/dim]"
        )
    st = planner.status(job_id)
    if int(st.get("tasks_total") or 0) == 0 or st.get("warning") == "zero_tasks":
        rprint("[bold yellow]WARNING: backfill planned 0 tasks — nothing will be scraped.[/bold yellow]")
        for reason in st.get("zero_task_reasons") or []:
            src = reason.get("source", "?")
            detail = reason.get("detail") or reason.get("reason") or ""
            rprint(f"[yellow]  • {escape(str(src))}: {escape(str(detail))}[/yellow]")
        if st.get("notes") and not st.get("zero_task_reasons"):
            rprint(f"[yellow]  {escape(str(st['notes']))}[/yellow]")
        rprint(
            "[dim]Hint: check --source kinds, date range, and domain filters; "
            "RSS alone does not plan historical partitions.[/dim]"
        )
    print(json.dumps(st, indent=2, default=str))


def _print_job_summary_table(job_id: str, status: dict[str, Any]) -> None:
    from datetime import UTC, datetime

    import dateutil.parser
    from rich.panel import Panel
    from rich.table import Table

    status_val = status.get("status", "unknown").upper()
    style = "bold green" if status_val == "COMPLETED" else "bold yellow"

    rprint()
    rprint(
        Panel(
            f"[bold white]JOB ID: {job_id}[/bold white]\nStatus: [{style}]{status_val}[/{style}]",
            title="[bold cyan]AWARENESS BACKFILL INGESTION REPORT[/bold cyan]",
            expand=False,
        )
    )

    perf_table = Table(title="Performance & Ingestion Metrics", show_header=True, header_style="bold magenta")
    perf_table.add_column("Metric", style="cyan")
    perf_table.add_column("Value", style="bold green")

    started_at = None
    completed_at = None
    if status.get("started_at"):
        started_at = dateutil.parser.isoparse(status["started_at"])
    if status.get("completed_at"):
        completed_at = dateutil.parser.isoparse(status["completed_at"])
    else:
        completed_at = datetime.now(UTC)

    duration_str = "N/A"
    duration_sec = 0.0
    if started_at:
        duration = completed_at - started_at
        duration_sec = duration.total_seconds()
        duration_str = str(duration).split(".")[0]

    perf_table.add_row("Duration", duration_str)

    tasks_total = status.get("tasks_total", 0)
    tasks_comp = status.get("tasks_completed", 0)
    tasks_fail = status.get("tasks_failed", 0)
    tasks_dead = status.get("tasks_dead_lettered", 0)
    perf_table.add_row("Tasks Completed", f"{tasks_comp}/{tasks_total}")
    if tasks_fail > 0:
        perf_table.add_row("Tasks Failed", f"[red]{tasks_fail}[/red]")
    if tasks_dead > 0:
        perf_table.add_row("Tasks Dead Lettered", f"[bold red]{tasks_dead}[/bold red]")

    docs_emitted = status.get("docs_emitted", 0)
    docs_dropped = status.get("docs_dedup_dropped", 0)
    total_docs = docs_emitted + docs_dropped
    dedup_ratio = 0.0
    if total_docs > 0:
        dedup_ratio = (docs_dropped / total_docs) * 100.0

    perf_table.add_row("Unique Documents Ingested", f"{docs_emitted:,}")
    perf_table.add_row("Duplicates Dropped", f"{docs_dropped:,}")
    perf_table.add_row("Deduplication Efficiency", f"{dedup_ratio:.2f}%")

    bytes_proc = status.get("bytes_processed", 0)
    perf_table.add_row("Total Raw Data Processed", f"{_format_size(bytes_proc)} ({bytes_proc:,} bytes)")

    if duration_sec > 0:
        bytes_per_sec = bytes_proc / duration_sec
        docs_per_sec = docs_emitted / duration_sec
        perf_table.add_row("Ingestion Bandwidth", f"{_format_size(int(bytes_per_sec))}/s")
        perf_table.add_row("Ingestion Rate", f"{docs_per_sec:.2f} docs/s")

    console.print(perf_table)
    rprint()


@backfill_app.command("run")
def backfill_run(
    job_id: str = typer.Argument(..., help="Job id from `backfill submit`"),
    concurrency: int = typer.Option(0, "--concurrency", help="Override worker concurrency"),
    silent_progress: bool = typer.Option(
        False, "--silent-progress", help="Mute per-document ingestion logs in the terminal"
    ),
    mute_duplicates: bool = typer.Option(
        None,
        "--mute-duplicates/--no-mute-duplicates",
        help="Hide duplicate/revision documents in the terminal log",
    ),
) -> None:
    """Run pending tasks for ``job_id`` to completion (in-process)."""
    state, planner = _bootstrap()
    engine = WorkerEngine(
        state,
        planner,
        concurrency=concurrency or None,
        silent_progress=silent_progress,
        mute_duplicates=mute_duplicates,
    )
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _stop(*_a: object) -> None:
        engine.request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    reader: _StdinLineReader | None = None

    def _handle_input(line: str) -> None:
        """One line from the stdin reader (runs on the event loop)."""
        cmd = line.strip()
        if not cmd:
            rprint("[bold yellow]\nStopping backfill cleanly requested by keyboard input...[/bold yellow]")
            engine.request_stop()
            if reader is not None:
                reader.stop()
            return
        if cmd.startswith("/"):
            parts = cmd[1:].split()
            if not parts:
                return
            action = parts[0].lower()
            if action == "clear":
                print("\033[H\033[2J\033[3J", end="")
                console.print(banner.render_banner())
                rprint(f"[green]Backfill running[/green] job_id=[bold]{job_id}[/bold]")
                rprint(
                    "[bold cyan]Type slash commands (e.g. /help, /clear, /status, /stop) or press ENTER to stop.[/bold cyan]\n"
                )
            elif action == "help":
                rprint("\n[bold cyan]Available Slash Commands:[/bold cyan]")
                rprint("  [bold]/clear[/bold]  - Clear the terminal screen")
                rprint("  [bold]/status[/bold] - Show the current backfill job status and counters")
                rprint("  [bold]/stop[/bold]   - Stop the backfill cleanly")
                rprint("  [bold]/help[/bold]   - Display this help message")
                rprint("  [dim]Press ENTER (empty line) to stop backfill engine.[/dim]\n")
            elif action == "status":
                try:
                    j = state.get_job(job_id)
                    if j:
                        rprint(f"\n[bold cyan]Backfill Job Status ({job_id}):[/bold cyan]")
                        rprint(f"  Status:          [green]{j.status.value}[/green]")
                        rprint(f"  Tasks Processed: {j.tasks_completed}/{j.tasks_total}")
                        rprint(f"  Docs Emitted:    {j.docs_emitted}")
                        rprint(f"  Near-Dup Dropped: {j.docs_dedup_dropped}")
                        rprint(
                            f"  Bytes Processed: {_format_size(j.bytes_processed)} ({j.bytes_processed:,} bytes)\n"
                        )
                    else:
                        rprint("\n[yellow]Could not fetch job status from DB.[/yellow]\n")
                except Exception as e:
                    rprint(f"\n[red]Error fetching status: {escape(str(e))}[/red]\n")
            elif action == "stop":
                rprint("[bold yellow]\nStopping backfill cleanly requested by /stop command...[/bold yellow]")
                engine.request_stop()
                if reader is not None:
                    reader.stop()
            else:
                rprint(f"\n[red]Unknown command: /{action}. Type /help for a list of commands.[/red]\n")
        else:
            rprint(
                f"\n[yellow]Ignored raw input: '{cmd}'. Press ENTER on an empty line or type /stop to exit.[/yellow]\n"
            )

    async def _drive() -> None:
        nonlocal reader
        rprint(f"[green]Backfill started[/green] job_id=[bold]{job_id}[/bold]")
        rprint(
            "[bold cyan]Type slash commands (e.g. /help, /clear, /status, /stop) or press ENTER to stop.[/bold cyan]\n"
        )

        if sys.stdin.isatty():
            reader = _StdinLineReader(loop, _handle_input)
            reader.start()
        try:
            await engine.run_job(job_id)
        finally:
            if reader is not None:
                reader.stop()
            await engine.aclose()

    try:
        loop.run_until_complete(_drive())
    finally:
        loop.close()

    status = planner.status(job_id)
    if sys.stdout.isatty():
        _print_job_summary_table(job_id, status)
    else:
        print(json.dumps(status, indent=2, default=str))


@backfill_app.command("status")
def backfill_status(job_id: str = typer.Argument(...)) -> None:
    state, planner = _bootstrap()
    print(json.dumps(planner.status(job_id), indent=2, default=str))


# ── tail ─────────────────────────────────────────────────────────────────
@tail_app.command("start")
def tail_start(
    seeds: Path = typer.Option(None, "--seeds", help="Path to tail_seeds.yaml"),
    duration: int = typer.Option(0, "--duration", help="Auto-stop after N seconds (0=run until SIGINT)"),
    data_dir: Path = typer.Option(None, "--data-dir", "-d", help="Custom local data directory"),
    to_cloud: bool = typer.Option(
        None, "--to-cloud/--no-to-cloud", help="Write to the S3/Iceberg warehouse (default from `configure`)"
    ),
    to_local: bool = typer.Option(
        None, "--to-local/--no-to-local", help="Write to local JSONL/SQLite (default from `configure`)"
    ),
    to_gdrive: bool = typer.Option(
        None, "--to-gdrive/--no-to-gdrive", help="Upload captures to Google Drive (default from `configure`)"
    ),
    warehouse: str = typer.Option(
        None, "--warehouse", help="S3 bucket / warehouse path (e.g. s3://bucket/path)"
    ),
    interactive: bool = typer.Option(
        True, "--interactive/--no-interactive", help="Prompt for storage target choice interactively"
    ),
    gdelt: bool = typer.Option(
        None, "--gdelt/--no-gdelt", help="Also follow the GDELT global-news firehose (default from config)."
    ),
    gdelt_max_urls: int = typer.Option(
        0, "--gdelt-max-urls", help="Cap URLs pulled per 15-min GDELT slot (0=use config default)."
    ),
    match: list[str] = typer.Option(  # noqa: B008
        [],
        "--match",
        "-m",
        help="Topic filter: keep only live docs with this whole word/phrase (case-insensitive). Repeat for OR; use --match-regex for partial/pattern matches.",
    ),
    match_all: bool = typer.Option(
        False, "--match-all", help="Require ALL --match terms (AND) instead of ANY (OR)."
    ),
    match_regex: bool = typer.Option(
        False, "--match-regex", help="Treat --match terms as Python regular expressions."
    ),
    match_field: str = typer.Option("both", "--match-field", help="Where to match: title | text | both."),
    mute_duplicates: bool = typer.Option(
        None,
        "--mute-duplicates/--no-mute-duplicates",
        help="Hide duplicate/revision documents in the terminal log",
    ),
    job_id: str = typer.Option(None, "--job-id", help="Existing job ID to reuse"),
) -> None:
    """Start the tail engine in foreground. Ctrl-C or pressing ENTER stops it cleanly."""
    is_tty = sys.stdin.isatty()
    if match_field not in ("title", "text", "both"):
        raise typer.BadParameter("--match-field must be one of: title, text, both")
    if gdelt_max_urls < 0:
        raise typer.BadParameter("--gdelt-max-urls must be >= 0 (0 uses the config default)")

    # Destinations resolve in three tiers: explicit --to-* flags win; otherwise
    # the values persisted by `awareness configure`; otherwise (nothing set yet)
    # the interactive prompt, or safe defaults in non-interactive mode.
    yaml_data = _read_yaml_data()
    configured = any(k in yaml_data for k in ("enable_jsonl_staging", "enable_iceberg", "enable_gdrive"))
    cur = get_settings()
    from_config = configured and to_local is None and to_cloud is None and to_gdrive is None

    if configured:
        if to_local is None:
            to_local = cur.enable_jsonl_staging
        if to_cloud is None:
            to_cloud = cur.enable_iceberg
        if to_gdrive is None:
            to_gdrive = cur.enable_gdrive

    if interactive and is_tty and to_local is None and to_cloud is None and to_gdrive is None:
        rprint("[bold cyan]Tail Storage Configuration[/bold cyan]")
        rprint(
            "[dim]Tip: set this once with [bold]awareness configure[/bold] to skip this prompt next time.[/dim]"
        )
        rprint("Where would you like to save the live captured data?")
        rprint("  [1] Local storage only (JSONL & SQLite/DuckDB index)")
        rprint("  [2] Cloud storage only (S3 / Iceberg)")
        rprint("  [3] Both Local and Cloud")
        rprint("  [4] Nowhere (display captures in terminal, do not save)")

        choice = typer.prompt("Select option [1-4]", default="1")
        if choice == "2":
            to_local, to_cloud = False, True
        elif choice == "3":
            to_local, to_cloud = True, True
        elif choice == "4":
            to_local, to_cloud = False, False
        else:
            to_local, to_cloud = True, False
        if to_gdrive is None:
            to_gdrive = False
    else:
        if to_local is None:
            to_local = True
        if to_cloud is None:
            to_cloud = False
        if to_gdrive is None:
            to_gdrive = False

    if data_dir:
        os.environ["AW_DATA_DIR"] = str(data_dir.resolve())
    if to_cloud:
        os.environ["AW_ENABLE_ICEBERG"] = "True"
        if warehouse:
            os.environ["AW_ICEBERG_WAREHOUSE"] = warehouse
        elif not os.environ.get("AW_ICEBERG_WAREHOUSE"):
            os.environ["AW_ICEBERG_WAREHOUSE"] = "s3://awareness/warehouse"
    else:
        os.environ["AW_ENABLE_ICEBERG"] = "False"

    os.environ["AW_ENABLE_JSONL_STAGING"] = "True" if to_local else "False"
    os.environ["AW_ENABLE_GDRIVE"] = "True" if to_gdrive else "False"

    _sinks = [n for n, on in (("local", to_local), ("s3/iceberg", to_cloud), ("gdrive", to_gdrive)) if on]
    _plan = ", ".join(_sinks) if _sinks else "terminal only (NOT saved)"
    _src = " (from `awareness configure`)" if from_config else ""
    rprint(f"[dim]Writing captures to:[/dim] [bold {banner.C_HI}]{_plan}[/]{_src}")

    reset_settings()
    state, planner = _bootstrap()
    settings = get_settings()
    # Resolve GDELT firehose + topic filter (CLI overrides config defaults).
    use_gdelt = settings.tail_gdelt if gdelt is None else gdelt
    gdelt_cap = gdelt_max_urls or settings.tail_gdelt_max_urls
    match_config = {
        "match": list(match),
        "match_all": match_all,
        "match_regex": match_regex,
        "match_field": match_field,
    }
    tail = TailEngine(state, planner)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    shutdown = asyncio.Event()

    def _stop(*_a: object) -> None:
        loop.call_soon_threadsafe(shutdown.set)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    reader: _StdinLineReader | None = None
    job_id_res: str | None = None

    def _handle_input(line: str) -> None:
        """One line from the stdin reader (runs on the event loop)."""
        cmd = line.strip()
        if not cmd:
            rprint("[bold yellow]\nStopping capture cleanly requested by keyboard input...[/bold yellow]")
            loop.call_soon_threadsafe(shutdown.set)
            if reader is not None:
                reader.stop()
            return
        if cmd.startswith("/"):
            parts = cmd[1:].split()
            if not parts:
                return
            action = parts[0].lower()
            if action == "clear":
                print("\033[H\033[2J\033[3J", end="")
                console.print(banner.render_banner())
                rprint(f"[green]Tail running[/green] job_id=[bold]{job_id_res or '?'}[/bold]")
                rprint(
                    "[bold cyan]Type slash commands (e.g. /help, /clear, /status, /stop) or press ENTER to stop.[/bold cyan]\n"
                )
            elif action == "help":
                rprint("\n[bold cyan]Available Slash Commands:[/bold cyan]")
                rprint("  [bold]/clear[/bold]  - Clear the terminal screen")
                rprint("  [bold]/status[/bold] - Show the current tail job status and counters")
                rprint("  [bold]/stop[/bold]   - Stop the tail stream cleanly")
                rprint("  [bold]/help[/bold]   - Display this help message")
                rprint("  [dim]Press ENTER (empty line) to stop tail engine.[/dim]\n")
            elif action == "status":
                try:
                    j = state.get_job(job_id_res) if job_id_res else None
                    if j:
                        rprint(f"\n[bold cyan]Tail Job Status ({job_id_res}):[/bold cyan]")
                        rprint(f"  Status:          [green]{j.status.value}[/green]")
                        rprint(f"  Tasks Processed: {j.tasks_completed}/{j.tasks_total}")
                        rprint(f"  Docs Emitted:    {j.docs_emitted}")
                        rprint(f"  Near-Dup Dropped: {j.docs_dedup_dropped}")
                        rprint(
                            f"  Bytes Processed: {_format_size(j.bytes_processed)} ({j.bytes_processed:,} bytes)\n"
                        )
                    else:
                        rprint("\n[yellow]Could not fetch job status from DB.[/yellow]\n")
                except Exception as e:
                    rprint(f"\n[red]Error fetching status: {escape(str(e))}[/red]\n")
            elif action == "stop":
                rprint("[bold yellow]\nStopping capture cleanly requested by /stop command...[/bold yellow]")
                loop.call_soon_threadsafe(shutdown.set)
                if reader is not None:
                    reader.stop()
            else:
                rprint(f"\n[red]Unknown command: /{action}. Type /help for a list of commands.[/red]\n")
        else:
            rprint(
                f"\n[yellow]Ignored raw input: '{cmd}'. Press ENTER on an empty line or type /stop to exit.[/yellow]\n"
            )

    async def _drive() -> None:
        nonlocal reader, job_id_res
        job_id_res = await tail.start(
            seeds_path=seeds,
            match_config=match_config,
            gdelt=use_gdelt,
            gdelt_max_urls=gdelt_cap,
            mute_duplicates=mute_duplicates,
            job_id=job_id,
        )
        rprint(f"[green]Tail started[/green] job_id=[bold]{job_id_res}[/bold]")
        if use_gdelt:
            rprint(f"[dim]GDELT firehose: ON (≤{gdelt_cap} URLs / 15-min slot)[/dim]")
        if match:
            joiner = " AND " if match_all else " OR "
            rprint(
                f"[dim]Topic filter ({'regex' if match_regex else 'keyword'}, {match_field}): {escape(joiner.join(match))}[/dim]"
            )
        rprint(
            "[bold cyan]Type slash commands (e.g. /help, /clear, /status, /stop) or press ENTER to stop.[/bold cyan]\n"
        )

        if sys.stdin.isatty():
            reader = _StdinLineReader(loop, _handle_input)
            reader.start()
        try:
            if duration > 0:
                try:
                    await asyncio.wait_for(shutdown.wait(), timeout=duration)
                except TimeoutError:
                    pass
            else:
                await shutdown.wait()
        finally:
            if reader is not None:
                reader.stop()
            await tail.stop()
            rprint("[yellow]Tail stopped cleanly.[/yellow]")

    try:
        loop.run_until_complete(_drive())
    finally:
        loop.close()


@tail_app.command("stop")
def tail_stop() -> None:
    """Signal a running tail to stop via state DB (cooperative)."""
    state, planner = _bootstrap()
    tail = state.get_tail()
    if not tail.get("running"):
        rprint("[yellow]Tail is not running[/yellow]")
        return
    job_id = tail.get("job_id")
    if job_id:
        # We can't reach into the foreground process from here in the
        # zero-Docker setup; we mark the tail state as stopping so that the
        # next poll of the running process sees it and shuts down cleanly.
        planner.stop_tail(job_id, note="cli-requested-stop")
        rprint("[green]Tail stop requested[/green]")
    else:
        rprint("[yellow]No tail job id recorded[/yellow]")


@tail_app.command("status")
def tail_status() -> None:
    state, _ = _bootstrap()
    print(json.dumps(state.get_tail(), indent=2, default=str))


@tail_app.command("check-seeds")
def tail_check_seeds(seeds: Path = typer.Option(None, "--seeds", help="Path to tail_seeds.yaml")) -> None:
    """Validate feeds and sitemaps in tail_seeds.yaml for connectivity, robots.txt, and parseability."""
    import anyio
    import httpx
    import yaml

    from awareness.util.robots import RobotsCache

    if not seeds:
        settings = get_settings()
        seeds = settings.tail_seed_file

    if not seeds or not seeds.exists():
        rprint(f"[red]Seeds file not found at: {seeds}[/red]")
        return

    try:
        with open(seeds, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception as e:
        rprint(f"[red]Failed to parse YAML seeds: {e}[/red]")
        return

    feeds_list = data.get("feeds", []) or []
    atom_list = data.get("atom", []) or []
    sitemaps_list = data.get("sitemaps", []) or []

    all_feeds = []
    for f in feeds_list:
        if isinstance(f, dict) and f.get("url"):
            all_feeds.append((f["url"], "RSS/Feed"))
    for f in atom_list:
        if isinstance(f, dict) and f.get("url"):
            all_feeds.append((f["url"], "Atom"))
    for s in sitemaps_list:
        if isinstance(s, dict) and s.get("url"):
            all_feeds.append((s["url"], "Sitemap"))

    if not all_feeds:
        rprint("[yellow]No seeds configured in tail_seeds.yaml[/yellow]")
        return

    rprint(f"[bold cyan]Validating {len(all_feeds)} configuration seeds...[/bold cyan]\n")

    async def validate_all():
        ua = get_settings().user_agent
        async with httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=True,
            headers={"User-Agent": ua},
        ) as client:
            robots = RobotsCache()
            robots._client = client

            table = Table("Type", "Seed URL", "HTTP Status", "Robots.txt", "Parser Status")
            for url, kind in all_feeds:
                allowed = await robots.is_allowed(url, ua)
                robots_status = "[green]Allowed[/green]" if allowed else "[red]Disallowed[/red]"

                try:
                    r = await client.get(url)
                    status_str = (
                        f"[green]{r.status_code}[/green]"
                        if r.status_code == 200
                        else f"[yellow]{r.status_code}[/yellow]"
                    )

                    if kind in ("RSS/Feed", "Atom"):
                        import feedparser

                        parsed = feedparser.parse(r.text)
                        if parsed.bozo:
                            parser_str = "[yellow]Bozo Feed (Format warnings)[/yellow]"
                        else:
                            parser_str = f"[green]Parsed ({len(parsed.entries)} entries)[/green]"
                    elif "<sitemap" in r.text or "<url" in r.text:
                        parser_str = "[green]Valid Sitemap XML[/green]"
                    else:
                        parser_str = "[red]Not a valid Sitemap XML[/red]"
                except Exception as e:
                    status_str = "[red]Connection Failed[/red]"
                    parser_str = f"[red]{str(e)[:50]}[/red]"

                table.add_row(kind, url, status_str, robots_status, parser_str)
            console.print(table)
            await robots.aclose()

    anyio.run(validate_all)


# ── dead-letter queue ────────────────────────────────────────────────────
@dlq_app.command("list")
def dlq_list(
    limit: int = typer.Option(50, "--limit", "-n", help="Max rows to show (1–1000)"),
    job_id: str = typer.Option("", "--job-id", "-j", help="Filter by job id"),
    offset: int = typer.Option(0, "--offset", help="Skip N newest rows (pagination)"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable JSON array"),
) -> None:
    """List dead-lettered tasks (newest first).

    Empty queues exit 0 with a short note (or ``[]`` under ``--json``) so
    scripts can poll without treating idle as failure.
    """
    state, _ = _bootstrap()
    jid = job_id.strip() or None
    total = state.count_dlq(job_id=jid)
    rows = state.list_dlq(limit=limit, job_id=jid, offset=offset)
    if as_json:
        print(json.dumps({"total": total, "offset": offset, "items": rows}, indent=2, default=str))
        return
    if total == 0:
        scope = f" for job {jid}" if jid else ""
        rprint(f"[dim]Dead-letter queue is empty{scope}.[/dim]")
        return
    if not rows:
        rprint(
            f"[yellow]No DLQ rows in this window[/yellow] (total={total}, offset={offset}, limit={limit})."
        )
        return
    table = Table(
        title=f"Dead-letter queue ({len(rows)} shown / {total} total)",
        show_lines=False,
    )
    table.add_column("id", style="cyan", justify="right")
    table.add_column("created", style="dim")
    table.add_column("job_id")
    table.add_column("task_id")
    table.add_column("error", overflow="fold")
    table.add_column("payload", overflow="fold")
    for row in rows:
        payload = row.get("payload") or {}
        # Compact payload preview: prefer partition/url keys when present.
        preview_bits: list[str] = []
        if isinstance(payload, dict):
            for key in ("url", "partition_key", "source_type", "discovery_channel"):
                if key in payload and payload[key] is not None:
                    preview_bits.append(f"{key}={payload[key]}")
            if not preview_bits:
                raw = json.dumps(payload, default=str, ensure_ascii=False)
                preview_bits.append(raw if len(raw) <= 80 else raw[:77] + "…")
        else:
            preview_bits.append(str(payload)[:80])
        created = row.get("created_at") or "—"
        if isinstance(created, str) and len(created) >= 19:
            created = created[:19].replace("T", " ")
        table.add_row(
            str(row.get("id") or ""),
            str(created),
            str(row.get("job_id") or "—"),
            str(row.get("task_id") or "—"),
            (row.get("error") or "")[:200],
            " ".join(preview_bits)[:120],
        )
    console.print(table)
    if offset + len(rows) < total:
        rprint(
            f"[dim]… {total - offset - len(rows)} older not shown "
            f"(use --offset {offset + len(rows)} or raise --limit).[/dim]"
        )


@dlq_app.command("count")
def dlq_count(
    job_id: str = typer.Option("", "--job-id", "-j", help="Filter by job id"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable JSON"),
) -> None:
    """Show how many tasks are in the dead-letter queue."""
    state, _ = _bootstrap()
    jid = job_id.strip() or None
    n = state.count_dlq(job_id=jid)
    if as_json:
        print(json.dumps({"dlq_count": n, "job_id": jid}))
        return
    scope = f" (job {jid})" if jid else ""
    if n == 0:
        rprint(f"[dim]DLQ empty{scope}.[/dim]")
    else:
        rprint(f"[bold red]{n}[/bold red] dead-lettered task(s){scope}.")


@dlq_app.command("replay")
def dlq_replay(
    dlq_id: int = typer.Argument(..., help="DLQ row id (from `dlq list`)"),
    keep_attempts: bool = typer.Option(
        False,
        "--keep-attempts",
        help="Do not reset attempts (default: reset to 0 so max-retries restarts)",
    ),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable JSON"),
) -> None:
    """Re-arm a dead-lettered task and drop its DLQ entry.

    Resets the task to PENDING so the worker pool can claim it again. By default
    ``attempts`` is cleared; pass ``--keep-attempts`` to preserve the prior
    counter (may immediately re-dead-letter if already at max retries).
    """
    state, _ = _bootstrap()
    result = state.replay_dlq(dlq_id, reset_attempts=not keep_attempts)
    if as_json:
        print(json.dumps(result, indent=2, default=str))
        if not result.get("ok"):
            raise typer.Exit(code=1)
        return
    if not result.get("ok"):
        reason = result.get("reason") or "unknown"
        rprint(f"[bold red]Replay failed[/bold red]: {reason} (dlq_id={dlq_id}).")
        if reason == "dlq_missing":
            rprint("[dim]Use `awareness dlq list` to see current ids.[/dim]")
        elif reason == "task_missing":
            rprint(
                f"[dim]Task {result.get('task_id')!r} is gone; re-plan or reseed "
                "instead of replaying this DLQ row.[/dim]"
            )
        raise typer.Exit(code=1)
    rprint(
        f"[bold green]Replayed[/bold green] dlq #{result.get('dlq_id')} → "
        f"task [cyan]{result.get('task_id')}[/cyan] "
        f"(job {result.get('job_id')}, was {result.get('previous_status')}, "
        f"attempts={result.get('attempts')})."
    )


@dlq_app.command("purge")
def dlq_purge(
    dlq_id: int = typer.Argument(..., help="DLQ row id (from `dlq list`)"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable JSON"),
) -> None:
    """Remove a DLQ entry without re-arming the task.

    Use after manually resolving a failure (or when abandoning the task). The
    task row stays ``DEAD_LETTERED`` (or whatever status it has); only the
    queue entry is deleted so operators can keep the DLQ clean.
    """
    state, _ = _bootstrap()
    result = state.purge_dlq(dlq_id)
    if as_json:
        print(json.dumps(result, indent=2, default=str))
        if not result.get("ok"):
            raise typer.Exit(code=1)
        return
    if not result.get("ok"):
        reason = result.get("reason") or "unknown"
        rprint(f"[bold red]Purge failed[/bold red]: {reason} (dlq_id={dlq_id}).")
        if reason == "dlq_missing":
            rprint("[dim]Use `awareness dlq list` to see current ids.[/dim]")
        raise typer.Exit(code=1)
    rprint(
        f"[bold green]Purged[/bold green] dlq #{result.get('dlq_id')} "
        f"(task [cyan]{result.get('task_id') or '—'}[/cyan], "
        f"job {result.get('job_id') or '—'}) — task not re-armed."
    )


@dlq_app.command("purge-bulk")
def dlq_purge_bulk(
    job_id: str = typer.Option("", "--job-id", "-j", help="Only purge rows for this job"),
    limit: int = typer.Option(
        0,
        "--limit",
        "-n",
        help="Max rows to drop (0 = all matching; newest first)",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip confirmation prompt (required for non-interactive use)",
    ),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable JSON"),
) -> None:
    """Drop many DLQ entries without re-arming tasks.

    Filters by ``--job-id`` when set; otherwise purges the whole queue.
    Use ``--limit`` to drain large queues in batches. Task rows stay
    ``DEAD_LETTERED``; only queue entries are removed (same as ``dlq purge``).
    """
    state, _ = _bootstrap()
    jid = job_id.strip() or None
    cap = None if limit <= 0 else int(limit)
    pending = state.count_dlq(job_id=jid)
    if pending == 0:
        empty = {
            "ok": True,
            "purged": 0,
            "job_id": jid,
            "limit": cap,
            "remaining": 0,
        }
        if as_json:
            print(json.dumps(empty, indent=2, default=str))
            return
        scope = f" for job {jid}" if jid else ""
        rprint(f"[dim]Dead-letter queue is empty{scope}; nothing to purge.[/dim]")
        return
    will_purge = pending if cap is None else min(pending, cap)
    if not yes:
        scope = f" for job {jid}" if jid else ""
        rprint(
            f"[bold yellow]About to purge {will_purge} DLQ row(s){scope}[/bold yellow] "
            f"(of {pending} matching; tasks not re-armed)."
        )
        if not typer.confirm("Continue?", default=False):
            rprint("[dim]Aborted.[/dim]")
            raise typer.Exit(code=1)
    result = state.purge_dlq_bulk(job_id=jid, limit=cap)
    if as_json:
        print(json.dumps(result, indent=2, default=str))
        return
    rprint(
        f"[bold green]Purged[/bold green] {result.get('purged')} DLQ row(s)"
        + (f" for job {jid}" if jid else "")
        + f" — {result.get('remaining')} remaining; tasks not re-armed."
    )


# ── inspect ──────────────────────────────────────────────────────────────
@app.command()
def inspect(
    start: str = typer.Option(..., "--start"),
    end: str = typer.Option("now", "--end"),
    limit: int = typer.Option(20, "--limit"),
    domain: str = typer.Option("", "--domain"),
    source: str = typer.Option("", "--source"),
) -> None:
    """Query stored captures by date/domain/source."""
    state, _ = _bootstrap()
    settings = get_settings()
    idx = DuckDbIndex(
        db_path=settings.duckdb_path(),
        jsonl_dir=settings.staging_jsonl_dir(),
        iceberg_warehouse=settings.iceberg_warehouse,
    )
    start_dt = to_utc(start)
    end_dt = inclusive_end(_coerce_end_checked(end))
    where = ["fetch_ts >= $start", "fetch_ts <= $end"]
    params: dict[str, Any] = {"start": start_dt, "end": end_dt}
    if domain:
        where.append("lower(domain) = $dom")
        params["dom"] = str(domain).strip().lower()
    if source:
        # Case-insensitive: align CLI list with API/search (RSS vs rss).
        where.append("lower(source_type) = $src")
        params["src"] = str(source).strip().lower()
    where_sql = " AND ".join(where)
    sql = f"""
        SELECT
          doc_id, capture_id, source_type, source_name,
          fetch_ts, domain, title, length(text) AS text_len, language
        FROM captures
        WHERE {where_sql}
        ORDER BY fetch_ts DESC
        LIMIT {int(limit)}
    """
    try:
        rows = idx.execute(sql, params)
    except Exception as exc:
        rprint(f"[red]Query failed:[/red] {exc}")
        return
    if not rows:
        rprint("[yellow]No captures match.[/yellow]")
        return
    cols = list(rows[0].keys())
    table = Table(*cols)
    for r in rows:
        table.add_row(*(str(r[c]) for c in cols))
    console.print(table)


@app.command(name="counts")
def counts(
    start: str = typer.Option(..., "--start"),
    end: str = typer.Option("now", "--end"),
) -> None:
    """Aggregate counts by source, domain, and language in [start, end]."""
    state, _ = _bootstrap()
    settings = get_settings()
    idx = DuckDbIndex(
        db_path=settings.duckdb_path(),
        jsonl_dir=settings.staging_jsonl_dir(),
        iceberg_warehouse=settings.iceberg_warehouse,
    )
    start_dt = to_utc(start)
    end_dt = inclusive_end(_coerce_end_checked(end))
    try:
        params = {"start": start_dt, "end": end_dt}
        # Case-normalize source buckets (RSS vs rss) so CLI counts match search chips.
        by_source = idx.execute(
            """
            SELECT lower(CAST(source_type AS VARCHAR)) AS source_type, COUNT(*) AS n
            FROM captures
            WHERE fetch_ts BETWEEN $start AND $end
              AND source_type IS NOT NULL
              AND CAST(source_type AS VARCHAR) != ''
            GROUP BY 1
            ORDER BY n DESC
            """,
            params,
        )
        by_domain = idx.execute(
            """
            SELECT domain, COUNT(*) AS n
            FROM captures
            WHERE fetch_ts BETWEEN $start AND $end AND domain IS NOT NULL
            GROUP BY domain
            ORDER BY n DESC LIMIT 25
            """,
            params,
        )
        # Primary BCP-47 tags: en / en-US / en_GB → one "en" bucket.
        by_language = idx.execute(
            f"""
            SELECT {PRIMARY_LANGUAGE_SQL} AS language, COUNT(*) AS n
            FROM captures
            WHERE fetch_ts BETWEEN $start AND $end
              AND language IS NOT NULL
              AND CAST(language AS VARCHAR) != ''
            GROUP BY 1
            ORDER BY n DESC
            LIMIT 50
            """,
            params,
        )
        total = idx.execute(
            "SELECT COUNT(*) AS n FROM captures WHERE fetch_ts BETWEEN $start AND $end",
            params,
        )
        # M-02: execute() returns rows — extract the scalar int.
        total_n = int(total[0]["n"]) if total else 0
        print(
            json.dumps(
                {
                    "total": total_n,
                    "by_source": by_source,
                    "by_domain": by_domain,
                    "by_language": by_language,
                },
                indent=2,
                default=str,
            )
        )
    except Exception as exc:
        rprint(f"[red]Query failed:[/red] {escape(str(exc))}")


@app.command(name="digest")
def digest(  # noqa: PLR0917 (10 options incl. the SMTP delivery flags)
    days: int = typer.Option(
        7, "--days", min=1, max=365, help="Digest window length in days"
    ),
    markdown: bool = typer.Option(
        False, "--markdown", help="Render the digest as markdown (default output format)"
    ),
    json_out: bool = typer.Option(False, "--json", help="Output raw digest JSON instead of markdown"),
    out: str = typer.Option("", "--out", help="Write the digest to this file instead of stdout"),
    email_to: str = typer.Option(
        "", "--email", help="Email the digest to this address via SMTP"
    ),
    smtp_host: str = typer.Option(
        "", "--smtp-host", help="SMTP server host (default: $SMTP_HOST)"
    ),
    smtp_port: int | None = typer.Option(
        None, "--smtp-port", min=1, max=65535,
        help="SMTP server port (default: $SMTP_PORT or 587)",
    ),
    smtp_user: str = typer.Option(
        "", "--smtp-user", help="SMTP login user (default: $SMTP_USER)"
    ),
    smtp_password: str = typer.Option(
        "", "--smtp-password", help="SMTP login password (default: $SMTP_PASSWORD)"
    ),
    from_addr: str = typer.Option(
        "", "--from", help="From address (default: $EMAIL_FROM)"
    ),
) -> None:
    """Generate a digest of the last N days of captures (markdown or JSON).

    With --email, the rendered markdown is delivered over SMTP instead of
    being printed locally. SMTP details come from the --smtp-* flags or the
    SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / EMAIL_FROM env vars.
    """
    settings = get_settings()
    idx = DuckDbIndex(
        db_path=settings.duckdb_path(),
        jsonl_dir=settings.staging_jsonl_dir(),
        iceberg_warehouse=settings.iceberg_warehouse,
    )
    from awareness.consume.digest import generate_digest, render_digest_markdown  # noqa: PLC0415

    try:
        digest_obj = generate_digest(idx, days=days)
        if json_out and not markdown:
            text = json.dumps(digest_obj.model_dump(mode="json"), indent=2)
        else:
            text = render_digest_markdown(digest_obj)
        if email_to:
            _email_digest(
                to_addr=email_to,
                subject=f"Awareness digest — {digest_obj.generated_at:%Y-%m-%d}",
                body=text,
                smtp_host=smtp_host,
                smtp_port=smtp_port,
                smtp_user=smtp_user,
                smtp_password=smtp_password,
                from_addr=from_addr,
            )
            return
        if out:
            out_path = Path(out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(text, encoding="utf-8")
            rprint(f"[green]Digest written to {out_path}[/green]")
        else:
            console.print(text)
    finally:
        idx.close()


def _email_digest(
    *,
    to_addr: str,
    subject: str,
    body: str,
    smtp_host: str,
    smtp_port: int | None,
    smtp_user: str,
    smtp_password: str,
    from_addr: str,
) -> None:
    """Deliver *body* (plain text) to *to_addr* over SMTP (stdlib only).

    Explicit --smtp-* flags win; anything unset falls back to the
    SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / EMAIL_FROM env
    vars. Connection / auth / send failures print a rich red error and
    exit with code 1.
    """
    host = smtp_host or os.environ.get("SMTP_HOST", "")
    if not host:
        rprint(
            "[red]Email delivery needs an SMTP server:[/red] "
            "pass --smtp-host or set SMTP_HOST."
        )
        raise typer.Exit(code=1)
    raw_port = smtp_port or os.environ.get("SMTP_PORT") or "587"
    try:
        port = int(raw_port)
    except (TypeError, ValueError) as exc:
        rprint(f"[red]Invalid SMTP port:[/red] {escape(str(raw_port))}")
        raise typer.Exit(code=1) from exc
    user = smtp_user or os.environ.get("SMTP_USER", "")
    password = smtp_password or os.environ.get("SMTP_PASSWORD", "")
    sender = from_addr or os.environ.get("EMAIL_FROM", "") or user or to_addr

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_addr
    msg.set_content(body)  # text/plain

    server: smtplib.SMTP | smtplib.SMTP_SSL | None = None
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            # Explicit TLS (STARTTLS) on submission ports — without this,
            # credentials and the digest body travel in the clear on 587.
            server.ehlo()
            server.starttls()
            server.ehlo()
        if user:
            server.login(user, password)
        server.send_message(msg)
        rprint(f"[green]Digest emailed to {to_addr}[/green]")
    except Exception as exc:  # connection / auth / send failures
        rprint(f"[red]Email delivery failed:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                server.close()


# ── trends ──────────────────────────────────────────────────────────────────

_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"
_SPARK_MIN_WIDTH = 20
_SPARK_MAX_WIDTH = 60
_SPIKE_Z_THRESHOLD = 2.5
_SPIKE_MIN_ABSOLUTE = 3


def _sparkline(values: list[int | float], width: int = 40) -> str:
    """Block-character sparkline of *values*, scaled to the series min/max.

    *width* is clamped to ``[_SPARK_MIN_WIDTH, _SPARK_MAX_WIDTH]`` characters:
    shorter series are linearly upsampled, longer ones downsampled, so the
    sparkline stays readable for any window length. Non-finite values
    (NaN / inf) are dropped before rendering.
    """
    finite = [value for value in values if math.isfinite(float(value))]
    n = len(finite)
    if n == 0:
        return ""
    width = min(max(width, _SPARK_MIN_WIDTH), _SPARK_MAX_WIDTH)
    if width == 1:
        return _SPARK_BLOCKS[0]
    if n == width:
        sampled = list(finite)
    else:
        sampled = []
        for i in range(width):
            pos = (n - 1) * i / (width - 1)
            lo = int(pos)
            hi = min(lo + 1, n - 1)
            frac = pos - lo
            sampled.append(finite[lo] * (1.0 - frac) + finite[hi] * frac)
        # Downsampling can erase isolated spikes between lattice points; pin
        # the true extremes into the lattice column nearest their source
        # index. Upsampling never pins — every sampled column is an exact
        # interpolation and the endpoints are already correct, so pinning
        # would just smear the true peak into the last column.
        if n > width:
            argmax = max(range(n), key=lambda k: (finite[k], k))
            argmin = min(range(n), key=lambda k: (finite[k], k))
            i_max = round(argmax * (width - 1) / (n - 1))
            i_min = round(argmin * (width - 1) / (n - 1))
            sampled[i_max] = max(sampled[i_max], finite[argmax])
            sampled[i_min] = min(sampled[i_min], finite[argmin])
    top = max(sampled)
    low = min(sampled)
    # Flat series: interpolation of identical values can drift by an ulp
    # (5.0 vs 5.000000000000001), which would blow up the scale — check the
    # original values exactly.
    if max(finite) == min(finite) or top == low:
        return _SPARK_BLOCKS[0] * width
    scale = 7.0 / (top - low)
    out = []
    for value in sampled:
        idx = round((value - low) * scale)
        out.append(_SPARK_BLOCKS[min(max(idx, 0), 7)])
    return "".join(out)


def _bucket_day_range(ts: datetime, granularity: str) -> set:
    """Calendar day set covered by one *granularity* bucket starting at *ts*."""
    first = ts.date()
    if granularity == "day":
        return {first}
    if granularity == "week":
        return {first + timedelta(days=i) for i in range(7)}
    if ts.month == 12:
        next_month = ts.replace(year=ts.year + 1, month=1, day=1)
    else:
        next_month = ts.replace(month=ts.month + 1, day=1)
    last = next_month - timedelta(days=1)
    num_days = (last.date() - first).days + 1
    return {first + timedelta(days=i) for i in range(num_days)}


def _zscore_series(counts: list[int]) -> list[float]:
    """Per-bucket z-scores (sample std, ``ddof=1``); 0.0 when underivable."""
    n = len(counts)
    if n == 0:
        return []
    mean = sum(counts) / n
    if n < 2:
        return [0.0] * n
    var = sum((c - mean) ** 2 for c in counts) / (n - 1)
    std = var**0.5
    if std == 0.0:
        return [0.0] * n
    return [(c - mean) / std for c in counts]


@app.command(name="trends")
def trends(
    term: str = typer.Argument(..., help="Term to trend across the captured corpus"),
    days: int = typer.Option(14, "--days", min=1, max=365, help="Window length in days"),
    granularity: str = typer.Option(
        "day",
        "--granularity",
        click_type=click.Choice(["day", "week", "month"]),
        help="Time bucket size",
    ),
    chart: bool = typer.Option(False, "--chart", help="Append a sparkline of the series"),
    with_sentiment: bool = typer.Option(
        False, "--sentiment", help="Also score each bucket with the sentiment engine"
    ),
) -> None:
    """Show term frequency over time, z-scores and (optionally) sentiment.

    Buckets are zero-filled over the window, so the series is chart-ready.
    A z-score at or above 2.5 is marked with ``!``. With ``--chart`` a
    block-character sparkline (clamped to 20-60 chars) is printed under the
    table; with ``--sentiment`` an average-score column is added.
    """
    cleaned = (term or "").strip()
    if not cleaned:
        raise typer.BadParameter("term must not be empty")
    settings = get_settings()
    idx = DuckDbIndex(
        db_path=settings.duckdb_path(),
        jsonl_dir=settings.staging_jsonl_dir(),
        iceberg_warehouse=settings.iceberg_warehouse,
    )
    from awareness.analytics.engine import TermFrequencyEngine  # noqa: PLC0415

    try:
        engine = TermFrequencyEngine(idx)
        buckets = engine.term_frequency_over_time(
            cleaned, window_days=days, granularity=granularity
        )
        spikes = engine.detect_spikes(
            cleaned, window_days=days, zscore_threshold=_SPIKE_Z_THRESHOLD
        )
    finally:
        idx.close()
    spike_days = {s.bucket.date() for s in spikes}
    counts = [b.count for b in buckets]
    zscores = _zscore_series(counts)

    sentiment_scores: dict[datetime, float] = {}
    if with_sentiment:
        try:
            from awareness.sentiment.engine import SentimentEngine  # noqa: PLC0415
        except ImportError:
            rprint("[yellow]sentiment engine not available; skipping sentiment[/yellow]")
        else:
            sentiment_idx = DuckDbIndex(
                db_path=settings.duckdb_path(),
                jsonl_dir=settings.staging_jsonl_dir(),
                iceberg_warehouse=settings.iceberg_warehouse,
            )
            try:
                sentiment_buckets = SentimentEngine(sentiment_idx).term_sentiment_over_time(
                    cleaned, window_days=days, granularity=granularity
                )
            finally:
                sentiment_idx.close()
            sentiment_scores = {sb.ts: sb.avg_score for sb in sentiment_buckets}

    if not buckets:
        rprint(f"[yellow]No captures found for {cleaned!r} in the last {days} days.[/yellow]")
        return

    table = Table(title=f"Trend: {cleaned!r} (last {days} days, {granularity} buckets)")
    table.add_column("Date", style=banner.C_HI)
    table.add_column("Count", justify="right")
    table.add_column("Z", justify="right")
    if with_sentiment and sentiment_scores:
        table.add_column("Sentiment", justify="right")
    for bucket, count, zscore in zip(buckets, counts, zscores, strict=True):
        marked = " !" if bucket.ts.date() in spike_days else ""
        date_col = f"{bucket.ts:%Y-%m-%d}"
        if granularity != "day":
            date_col = f"{date_col} ({_bucket_label(granularity)})"
        row = [date_col, str(count), f"{zscore:.2f}{marked}"]
        if with_sentiment and sentiment_scores:
            row.append(f"{sentiment_scores.get(bucket.ts, 0.0):+.2f}")
        table.add_row(*row)
    console.print(table)
    if chart:
        console.print(
            f"[dim]Sparkline ({granularity} buckets, window max = {max(counts)}):[/dim] "
            + _sparkline([float(c) for c in counts])
        )


def _bucket_label(granularity: str) -> str:
    return "week of" if granularity == "week" else "month of"


def _block_bar(count: int, max_count: int, width: int = 20) -> str:
    """Block-character bar scaled to *max_count* (empty when *count* is 0)."""
    if max_count <= 0 or count <= 0:
        return ""
    n = max(1, round(width * count / max_count))
    return "█" * n


def _ratio_verdict(ratio: float) -> str:
    """Rich-styled percentage cell with a duplicate-ratio verdict.

    Green below 5%, yellow below 20%, red at/above 20%.
    """
    pct = float(ratio) * 100.0
    style = "green" if pct < 5.0 else ("yellow" if pct < 20.0 else "red")
    return f"[{style}]{pct:.1f}%[/{style}]"


@app.command(name="quality")
def quality(
    json_out: bool = typer.Option(
        False, "--json", help="Output the raw quality snapshot as JSON instead of a table"
    ),
) -> None:
    """Corpus quality report: sizes, duplicate ratios, languages, domains.

    Builds the same DuckDB index as ``digest`` and prints
    ``CorpusXEngine.quality_snapshot()`` as a Rich table (or raw JSON with
    ``--json``). An empty corpus prints a clean "empty corpus" message.
    """
    settings = get_settings()
    idx = DuckDbIndex(
        db_path=settings.duckdb_path(),
        jsonl_dir=settings.staging_jsonl_dir(),
        iceberg_warehouse=settings.iceberg_warehouse,
    )
    from awareness.corpusx.engine import CorpusXEngine  # noqa: PLC0415

    try:
        snapshot = CorpusXEngine(idx).quality_snapshot()
    finally:
        idx.close()

    if json_out:
        print(json.dumps(snapshot.model_dump(mode="json"), indent=2, default=str))
        return
    if snapshot.total_captures == 0:
        rprint("[yellow]empty corpus[/yellow]")
        return

    general = Table(title="General")
    general.add_column("Metric", style=banner.C_HI)
    general.add_column("Value", justify="right")
    general.add_row("total_captures", f"{snapshot.total_captures:,}")
    general.add_row("capture_rate_per_day", f"{snapshot.capture_rate_per_day:.1f}")
    general.add_row("dedup_group_count", str(snapshot.dedup_group_count))
    general.add_row("avg_length", f"{snapshot.avg_length:.0f}")
    console.print(general)

    ratios = Table(title="Ratios")
    ratios.add_column("Metric", style=banner.C_HI)
    ratios.add_column("Value", justify="right")
    ratios.add_row("duplicate_ratio", _ratio_verdict(snapshot.duplicate_ratio))
    ratios.add_row("near_duplicate_ratio", _ratio_verdict(snapshot.near_duplicate_ratio))
    console.print(ratios)

    top_langs = list(snapshot.languages.items())[:8]
    max_lang = max((count for _, count in top_langs), default=0)
    langs = Table(title="Languages (top 8)")
    langs.add_column("language", style=banner.C_HI)
    langs.add_column("count", justify="right")
    langs.add_column("", style=banner.C_DIM)
    for lang, count in top_langs:
        langs.add_row(lang, str(count), _block_bar(count, max_lang))
    console.print(langs)

    domains = Table(title="Top domains (top 10)")
    domains.add_column("domain", style=banner.C_HI)
    domains.add_column("count", justify="right")
    for dom in snapshot.top_domains[:10]:
        domains.add_row(dom.domain, str(dom.count))
    console.print(domains)


# ── report ───────────────────────────────────────────────────────────────────
_FIRING_DETAIL_MAX = 80


def _quality_section(snap: Any) -> list[str]:
    """Corpus-quality bullets for the combined report."""
    lines = ["## Corpus quality", ""]
    lines.append(f"- total_captures: {snap.total_captures}")
    lines.append(f"- empty_text: {snap.empty_text}")
    lines.append(f"- duplicate_ratio: {snap.duplicate_ratio:.1%}")
    lines.append(f"- near_duplicate_ratio: {snap.near_duplicate_ratio:.1%}")
    lines.append(f"- dedup_group_count: {snap.dedup_group_count}")
    lines.append(f"- avg_length: {snap.avg_length:.0f}")
    lines.append(f"- capture_rate_per_day: {snap.capture_rate_per_day:.1f}")
    if snap.top_domains:
        pairs = ", ".join(f"{d.domain} ({d.count})" for d in snap.top_domains[:10])
        lines.append(f"- top_domains: {pairs}")
    if snap.languages:
        langs = ", ".join(
            f"{lang} ({count})"
            for lang, count in sorted(snap.languages.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        lines.append(f"- languages: {langs}")
    return lines


def _md_cell(text: str) -> str:
    """Escape markdown-table pipe characters in a cell value."""
    return str(text).replace("|", "\\|")


def _firings_section(firings: list[dict[str, Any]], days: int) -> list[str]:
    """Alert-activity table for the combined report (detail truncated)."""
    lines = ["## Alert activity", ""]
    if not firings:
        lines.append(f"_No alert firings in the last {days} days._")
        return lines
    lines.append(f"{len(firings)} alert firing(s) in the last {days} days.")
    lines.append("")
    lines.append("| Fired at (UTC) | Rule | Kind | Term | Count | Threshold | Detail |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for f in firings:
        detail = _md_cell(f["detail"])
        if len(detail) > _FIRING_DETAIL_MAX:
            detail = detail[:_FIRING_DETAIL_MAX - 3] + "..."
        lines.append(
            f"| {_md_cell(f['fired_at'].strftime('%Y-%m-%d %H:%M'))} "
            f"| {_md_cell(f['rule_name'])} | {_md_cell(f['kind'])} | {_md_cell(f['term'])} "
            f"| {f['count']} | {f['threshold']:g} | {detail} |"
        )
    return lines


def _gdelt_section(note: str | None, no_gdelt: bool) -> list[str]:
    """GDELT-context section: the note, or why it is absent."""
    lines = ["## GDELT context", ""]
    if no_gdelt:
        lines.append("_GDELT context skipped (--no-gdelt)._")
    elif note is None:
        lines.append("_No top terms — GDELT context skipped._")
    else:
        lines.append(note)
    return lines


def _render_report_markdown(
    digest_obj: Any,
    quality_snap: Any,
    firings: list[dict[str, Any]],
    gdelt_note: str | None,
    no_gdelt: bool,
) -> str:
    """Combine the digest markdown with quality, alerts and GDELT sections."""
    from awareness.consume.digest import render_digest_markdown  # noqa: PLC0415

    days = digest_obj.days
    lines: list[str] = [f"# Awareness report (last {days}d)", ""]
    lines.append(f"_Generated {digest_obj.generated_at:%Y-%m-%d %H:%M} UTC_")
    lines.append("")
    lines.append("## Digest")
    lines.append("")
    # The digest renderer starts with its own "# ..." title; reuse the body.
    digest_body = render_digest_markdown(digest_obj).split("\n", 1)[1].strip()
    lines.append(digest_body)
    lines.append("")
    lines.extend(_quality_section(quality_snap))
    lines.append("")
    lines.extend(_firings_section(firings, days))
    lines.append("")
    lines.extend(_gdelt_section(gdelt_note, no_gdelt))
    lines.append("")
    return "\n".join(lines)


@app.command(name="report")
def report(
    days: int = typer.Option(
        7, "--days", min=1, max=365, help="Report window length in days"
    ),
    out: str = typer.Option("", "--out", help="Write the report to this file instead of stdout"),
    json_out: bool = typer.Option(False, "--json", help="Output a raw JSON object"),
    email_to: str = typer.Option("", "--email", help="Email the report to this address via SMTP"),
    no_gdelt: bool = typer.Option(False, "--no-gdelt", help="Skip the GDELT context fetch"),
) -> None:
    """Combined report: digest + corpus quality + alert activity + GDELT context.

    Builds ONE DuckDB index (same pattern as ``digest``) and computes the
    digest (with ``include_gdelt=False`` — the report owns GDELT so an offline
    bridge degrades to a note instead of failing the digest), the corpus
    quality snapshot, alert firings in the window, and (unless ``--no-gdelt``)
    a GDELT comparison for the digest's top term. Renders a combined markdown
    report; ``--out`` writes it to a file, ``--email`` delivers it over SMTP
    (same machinery as ``digest --email``), ``--json`` dumps one JSON object.
    An empty corpus yields a report with zeros (exit 0); fatal errors exit 1.
    """
    settings = get_settings()
    idx: DuckDbIndex | None = None
    try:
        idx = DuckDbIndex(
            db_path=settings.duckdb_path(),
            jsonl_dir=settings.staging_jsonl_dir(),
            iceberg_warehouse=settings.iceberg_warehouse,
        )
        from awareness.consume.digest import generate_digest  # noqa: PLC0415
        from awareness.corpusx.engine import CorpusXEngine  # noqa: PLC0415

        digest_obj = generate_digest(idx, days=days, include_gdelt=False)
        quality_snap = CorpusXEngine(idx).quality_snapshot()

        from awareness.alerts.store import AlertStore  # noqa: PLC0415

        assert settings.data_dir is not None
        store = AlertStore(settings.data_dir / "alerts.db")
        try:
            firings = store.list_firings(limit=100, since=utcnow() - timedelta(days=days))
        finally:
            store.close()

        gdelt_note: str | None = None
        if not no_gdelt and digest_obj.top_terms:
            from awareness.gdeltx.engine import MAX_WINDOW_DAYS, GdeltBridge  # noqa: PLC0415

            try:
                bridge = GdeltBridge(idx)
                comparison = bridge.compare_with_local(
                    digest_obj.top_terms[0].term, window_days=min(days, MAX_WINDOW_DAYS)
                )
                gdelt_note = (
                    f"GDELT: {comparison.term} local {comparison.local_count} "
                    f"vs external {comparison.gdelt_count} (r={comparison.correlation_r:.2f})"
                )
            except Exception as exc:
                logger.warning("report_gdelt_unavailable", err=str(exc))
                gdelt_note = "GDELT unavailable"
    except Exception as exc:
        rprint(f"[red]report failed:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    finally:
        if idx is not None:
            idx.close()

    payload = {
        "generated_at": digest_obj.generated_at.isoformat(),
        "days": days,
        "digest": digest_obj.model_dump(mode="json"),
        "quality": quality_snap.model_dump(mode="json"),
        "firings": [{**f, "fired_at": f["fired_at"].isoformat()} for f in firings],
        "gdelt": gdelt_note,
    }
    if json_out:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        if out or email_to:
            import sys as _sys  # noqa: PLC0415

            rprint(
                "[yellow]--json: printed to stdout; "
                "--out/--email ignored for JSON mode[/yellow]",
                file=_sys.stderr,
            )
        return
    text = _render_report_markdown(digest_obj, quality_snap, firings, gdelt_note, no_gdelt)
    if email_to:
        # _email_digest already takes (to_addr, subject, body) — reuse it with
        # the combined markdown as the body; SMTP details fall back to env vars.
        _email_digest(
            to_addr=email_to,
            subject=f"Awareness report — {digest_obj.generated_at:%Y-%m-%d}",
            body=text,
            smtp_host="",
            smtp_port=None,
            smtp_user="",
            smtp_password="",
            from_addr="",
        )
        if out:
            rprint(
                "[yellow]--email: markdown emailed; "
                "--out ignored when --email is set[/yellow]"
            )
        return
    if out:
        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
        tmp_path.write_text(text, encoding="utf-8")
        tmp_path.replace(out_path)
        rprint(f"[green]Report written to {out_path}[/green]")
    else:
        console.print(text)


def summarize_feed_health(snap: dict[str, Any]) -> dict[str, Any]:
    """Aggregate feed/tail fetch health from a metrics snapshot.

    Mirrors the dashboard SPA (``summarizeFeedHealth`` in
    ``src/awareness/api/web/app.js``): ``feeds.fetch_attempts`` is bucketed
    by outcome (``ok`` / ``retry_exhausted`` / everything else = error),
    ``feeds.fetch_non_200`` and ``tail.fetch_non_200`` are summed, and the
    ``feeds.fetch_seconds`` p95 is count-weighted across label series. The
    0-100 health score is ``clamp(100 - 10*error_rate - 5*non200_rate)``
    where the rates are percentages of total attempts (``None`` when no
    attempts have been recorded yet).
    """
    attempts = 0.0
    ok = 0.0
    retry_exhausted = 0.0
    non200 = 0.0
    tail_non200 = 0.0
    buckets: dict[str, float] = {}
    for c in snap.get("counters") or []:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "")
        val = float(c.get("value") or 0)
        if name == "feeds.fetch_attempts":
            attempts += val
            outcome = (c.get("labels") or {}).get("outcome", "error")
            buckets[outcome] = buckets.get(outcome, 0.0) + val
        elif name == "feeds.fetch_non_200":
            non200 += val
        elif name == "tail.fetch_non_200":
            tail_non200 += val
    ok = buckets.get("ok", 0.0)
    retry_exhausted = buckets.get("retry_exhausted", 0.0)
    error = attempts - ok - retry_exhausted
    weighted_p95 = 0.0
    samples = 0
    for h in snap.get("histograms") or []:
        if isinstance(h, dict) and str(h.get("name") or "") == "feeds.fetch_seconds":
            n = int(h.get("count") or 0)
            if n > 0:
                p95 = h.get("p95")
                if isinstance(p95, (int, float)) and math.isfinite(float(p95)):
                    samples += n
                    weighted_p95 += float(p95) * n
    p95_sec = weighted_p95 / samples if samples else None
    error_rate = (100.0 * error / attempts) if attempts else None
    non200_rate = (100.0 * non200 / attempts) if attempts else None
    score: int | None = None
    if attempts > 0:
        raw_score = min(100.0, max(0.0, 100.0 - 10.0 * error_rate - 5.0 * non200_rate))
        # Half-up (floor(x + 0.5)) to match the SPA's Math.round; Python's
        # round() is half-even and diverges at exact halves (62.5 → 62 vs 63).
        score = math.floor(raw_score + 0.5)
    return {
        "attempts": int(attempts),
        "ok": int(ok),
        "error": int(error),
        "retry_exhausted": int(retry_exhausted),
        "non200": int(non200),
        "tail_non200": int(tail_non200),
        "p95_sec": p95_sec,
        "samples": samples,
        "error_rate_pct": error_rate,
        "non200_rate_pct": non200_rate,
        "score": score,
    }


@app.command(name="feeds")
def feeds(
    json_out: bool = typer.Option(
        False, "--json", help="Output the health summary as JSON instead of a table"
    ),
) -> None:
    """Feed-health report from in-process fetch metrics.

    Mirrors the dashboard "Feed health" band: attempts by outcome, non-200
    responses (feeds + tail), fetch p95 latency and a 0-100 health score
    (``100 - 10*error_rate - 5*non200_rate``, clamped).
    """
    summary = summarize_feed_health(get_metrics().snapshot())
    if json_out:
        print(json.dumps(summary, indent=2, default=str))
        return
    if summary["attempts"] == 0:
        rprint("[yellow]no fetch activity recorded[/yellow]")
        return
    table = Table(title="Feed health")
    table.add_column("Metric", style=banner.C_HI)
    table.add_column("Value", justify="right")
    table.add_row("attempts", f"{summary['attempts']:,}")
    table.add_row("ok", f"{summary['ok']:,}")
    table.add_row("error", f"{summary['error']:,} ({summary['error_rate_pct']:.1f}%)")
    table.add_row("retry_exhausted", f"{summary['retry_exhausted']:,}")
    table.add_row("non-200", f"{summary['non200']:,} ({summary['non200_rate_pct']:.1f}%)")
    table.add_row("tail non-200", f"{summary['tail_non200']:,}")
    p95 = summary["p95_sec"]
    table.add_row("fetch p95", f"{p95 * 1000:.0f} ms" if p95 is not None else "—")
    score = summary["score"]
    if score is not None:
        style = "green" if score >= 80 else ("yellow" if score >= 50 else "red")
        table.add_row("health score", f"[{style}]{score}[/{style}]")
    console.print(table)


# ── x: X scraper sessions ───────────────────────────────────────────────────


async def _x_with_store(op):
    """Open the process-local ``{data_dir}/xscraper.sqlite`` store, run *op*, close."""
    settings = get_settings()
    assert settings.data_dir is not None
    from awareness.xscraper.store import SessionStore  # noqa: PLC0415

    store = SessionStore(settings.data_dir / "xscraper.sqlite")
    await store.open()
    await store.init()
    try:
        return await op(store)
    finally:
        await store.close()


@x_app.command(name="sessions")
def x_sessions(
    limit: int = typer.Option(50, "--limit", min=1, max=500, help="Max sessions to list"),
) -> None:
    """List X scraper sessions, newest first."""
    sessions = asyncio.run(_x_with_store(lambda store: store.list_sessions(limit=limit)))
    table = Table(title=f"X scraper sessions ({len(sessions)})")
    table.add_column("ID", style=banner.C_HI)
    table.add_column("Title")
    table.add_column("Status")
    table.add_column("Created", style=banner.C_DIM)
    table.add_column("Tweets", justify="right")
    for s in sessions:
        table.add_row(
            s.session_id,
            s.title or "-",
            s.status,
            f"{s.created_at:%Y-%m-%d %H:%M:%S}",
            str(s.backfill_tweets + s.stream_tweets),
        )
    console.print(table)


@x_app.command(name="show")
def x_show(
    session_id: str = typer.Argument(..., help="Session id to inspect"),
    limit: int = typer.Option(100, "--limit", min=1, max=5000, help="Max tweets to list"),
) -> None:
    """Show one X scraper session plus its tweets (newest first)."""

    async def _show(store):
        session = await store.get_session(session_id)
        if session is None:
            return None
        tweets = await store.list_tweets(session_id, limit=limit)
        return session, tweets

    result = asyncio.run(_x_with_store(_show))
    if result is None:
        rprint(f"[red]session {session_id!r} not found[/red]")
        raise typer.Exit(code=2)
    session, tweets = result
    total = session.backfill_tweets + session.stream_tweets
    rprint(f"[bold cyan]Session:[/bold cyan] {session.title or session.session_id}")
    rprint(f"  id:      {session.session_id}")
    rprint(f"  status:  {session.status}")
    rprint(f"  created: {session.created_at:%Y-%m-%d %H:%M:%S}")
    rprint(f"  query:   {session.query}")
    rprint(f"  tweets:  {total} (backfill={session.backfill_tweets}, stream={session.stream_tweets})")
    table = Table(title=f"Tweets ({len(tweets)})")
    table.add_column("Username", style=banner.C_HI)
    table.add_column("Created", style=banner.C_DIM)
    table.add_column("Text")
    for t in tweets:
        text = t.text if len(t.text) <= 140 else t.text[:137] + "..."
        table.add_row(f"@{t.username}", f"{t.created_at:%Y-%m-%d %H:%M:%S}", text)
    console.print(table)


@x_app.command(name="create")
def x_create(  # noqa: PLR0917 - spec-mandated option surface
    title: str = typer.Option(..., "--title", help="Session title"),
    keywords: str = typer.Option(..., "--keywords", help="Comma-separated search keywords"),
    accounts: str = typer.Option("", "--accounts", help="Comma-separated X accounts (handles)"),
    raw_query: str = typer.Option("", "--raw-query", help="Raw X query fragment"),
    lookback: str = typer.Option("2h", "--lookback", help="Lookback window (e.g. 2h, 1d)"),
    language: str = typer.Option("", "--language", help="BCP-47 language filter (e.g. en)"),
) -> None:
    """Create an X scraper session (queued) and print its id + query."""
    from pydantic import ValidationError  # noqa: PLC0415

    from awareness.xscraper.models import SearchRequest  # noqa: PLC0415
    from awareness.xscraper.query import build_search_query  # noqa: PLC0415

    keyword_list = [k.strip() for k in keywords.split(",") if k.strip()]
    account_list = [a.strip() for a in accounts.split(",") if a.strip()]
    try:
        request = SearchRequest(
            title=title,
            keywords=keyword_list,
            accounts=account_list,
            raw_query=raw_query or None,
            lookback=lookback,
            language=language or None,
        )
        query = build_search_query(
            keywords=request.keywords,
            accounts=request.accounts,
            raw_query=request.raw_query,
            language=request.language,
        )
    except ValidationError as exc:
        detail = "; ".join(
            e["msg"] if not e.get("loc") else ".".join(str(p) for p in e["loc"]) + ": " + e["msg"]
            for e in exc.errors()
        )
        raise typer.BadParameter(f"invalid search request: {detail}") from exc
    except ValueError as exc:
        raise typer.BadParameter(f"invalid search request: {exc}") from exc

    session = asyncio.run(_x_with_store(lambda store: store.create_session(request, query)))
    rprint(f"[green]session created[/green] id={session.session_id} status={session.status}")
    rprint(f"query: {session.query}")


@x_app.command(name="simulate")
def x_simulate(
    session_id: str = typer.Argument(..., help="Session id to simulate tweets for"),
    count: int = typer.Option(20, "--count", min=1, max=200, help="Number of tweets to generate"),
    seed: int | None = typer.Option(None, "--seed", help="RNG seed for deterministic output"),
) -> None:
    """Generate simulated tweets for a session (deterministic per seed)."""
    from awareness.xscraper.simulate import simulate_session  # noqa: PLC0415

    async def _sim(store):
        inserted = await simulate_session(store, session_id, n_tweets=count, seed=seed)
        session = await store.get_session(session_id)
        return inserted, session

    try:
        inserted, session = asyncio.run(_x_with_store(_sim))
    except KeyError:
        rprint(f"[red]session {session_id!r} not found[/red]")
        raise typer.Exit(code=2) from None
    total = session.backfill_tweets + session.stream_tweets
    rprint(f"[green]simulated {inserted} tweets[/green] for session {session_id}")
    rprint(f"total tweets: {total}")


@x_app.command(name="export")
def x_export(
    session_id: str = typer.Argument(..., help="Session id to export"),
    out: str = typer.Option("", "--out", help="Output CSV path (default: {data_dir}/x_export_<id>.csv)"),
    limit: int = typer.Option(500, "--limit", min=1, max=500, help="Max tweets to export"),
) -> None:
    """Export a session's tweets to a CSV file."""
    from awareness.xscraper.analyze import export_tweets_csv  # noqa: PLC0415

    settings = get_settings()
    assert settings.data_dir is not None
    out_path = Path(out) if out else settings.data_dir / f"x_export_{session_id}.csv"

    async def _export(store):
        return await export_tweets_csv(store, session_id, out_path, limit=limit)

    try:
        count = asyncio.run(_x_with_store(_export))
    except KeyError:
        rprint(f"[red]session {session_id!r} not found[/red]")
        raise typer.Exit(code=2) from None
    rprint(f"Wrote {count} rows to {out_path}")


@x_app.command(name="analyze")
def x_analyze(
    session_id: str = typer.Argument(..., help="Session id to analyze"),
) -> None:
    """Analyze captured tweets for a session (authors, terms, sentiment)."""
    from awareness.xscraper.analyze import analyze_session  # noqa: PLC0415

    async def _ana(store):
        return await analyze_session(store, session_id)

    try:
        analysis = asyncio.run(_x_with_store(_ana))
    except KeyError:
        rprint(f"[red]session {session_id!r} not found[/red]")
        raise typer.Exit(code=2) from None

    console.print(
        f"[bold cyan]Analysis[/bold cyan] {session_id} — "
        f"{analysis['tweet_count']} tweets"
    )
    sentiment = analysis["sentiment"]
    sentiment_table = Table(title=f"Sentiment ({analysis['tweet_count']} tweets)")
    sentiment_table.add_column("Class")
    sentiment_table.add_column("Count", justify="right")
    sentiment_table.add_row("positive", str(sentiment["positive"]))
    sentiment_table.add_row("negative", str(sentiment["negative"]))
    sentiment_table.add_row("neutral", str(sentiment["neutral"]))
    sentiment_table.add_row("avg score", f"{sentiment['avg_score']:.4f}")
    console.print(sentiment_table)

    trend = analysis["sentiment_trend"]
    if trend:
        trend_values = [float(day["avg_score"]) for day in trend]
        console.print(
            f"[dim]Sentiment trend (daily avg score, {trend[0]['date']}..{trend[-1]['date']}, "
            f"min {min(trend_values):.2f}, max {max(trend_values):.2f}):[/dim] "
            + _sparkline(trend_values)
        )

    authors_table = Table(title="Top authors")
    authors_table.add_column("Username", style=banner.C_HI)
    authors_table.add_column("Count", justify="right")
    for author in analysis["authors"]:
        authors_table.add_row(f"@{author['username']}", str(author["count"]))
    console.print(authors_table)

    terms_table = Table(title="Top terms")
    terms_table.add_column("Term", style=banner.C_HI)
    terms_table.add_column("Count", justify="right")
    for term in analysis["top_terms"]:
        terms_table.add_row(term["term"], str(term["count"]))
    console.print(terms_table)

    timeline_table = Table(title="Timeline")
    timeline_table.add_column("Date", style=banner.C_DIM)
    timeline_table.add_column("Count", justify="right")
    for day in analysis["timeline"]:
        timeline_table.add_row(day["date"], str(day["count"]))
    console.print(timeline_table)

    engagement = analysis["engagement"]
    engagement_table = Table(title="Engagement")
    engagement_table.add_column("Metric")
    engagement_table.add_column("Value", justify="right")
    engagement_table.add_row("total likes", f"{engagement['total_likes']:,}")
    engagement_table.add_row("total retweets", f"{engagement['total_retweets']:,}")
    engagement_table.add_row("avg likes", f"{engagement['avg_likes']:.2f}")
    console.print(engagement_table)


@app.command()
def clear() -> None:
    """Clear the terminal screen."""
    print("\033[H\033[2J\033[3J", end="")


def _make_tui_layout(state: StateDB, settings: Any, idx: DuckDbIndex, selected_job_idx: int = 0) -> Any:
    from datetime import datetime

    from rich.layout import Layout
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    layout = Layout()
    layout.split_column(Layout(name="header", size=4), Layout(name="body"), Layout(name="footer", size=3))
    layout["body"].split_row(Layout(name="left", ratio=1), Layout(name="right", ratio=2))
    layout["right"].split_column(
        Layout(name="right_top", ratio=1),
        Layout(name="right_middle", ratio=1),
        Layout(name="right_bottom", ratio=1),
    )

    # 1. Header
    time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header_text = Text.assemble(
        (" AWARENESS ENGINE TUI DASHBOARD ", "bold reverse cyan"),
        "  |  Local Time: ",
        (time_str, "yellow"),
        "  |  Controls: ",
        (
            "[Q] Quit  [C] Compact  [T] Toggle Tail  [A] Toggle API  [R] Refresh  [L] Logs  [Y] Analytics  [S] Cancel  [D] Delete  [N] New",
            "bold green",
        ),
    )
    layout["header"].update(Panel(header_text, border_style="cyan"))

    # 2. Left Panel: Telemetry & DB
    db_metrics = _query_db_metrics(state)
    pid = _get_api_pid()
    api_status = "[green]RUNNING[/green]" if pid else "[red]STOPPED[/red]"
    tail_info = state.get_tail()
    tail_status = "[green]ACTIVE[/green]" if tail_info.get("running") else "[red]INACTIVE[/red]"

    total_docs = db_metrics["total_docs_emitted"] + db_metrics["total_docs_dedup_dropped"]
    dedup_ratio = (db_metrics["total_docs_dedup_dropped"] / total_docs * 100) if total_docs > 0 else 0.0

    left_text = Text()
    left_text.append("\n")
    left_text.append("SYSTEM SERVICES\n", style="bold cyan")
    left_text.append(f"  • API Server:      {api_status}\n")
    left_text.append(f"  • Live Tail:       {tail_status}\n\n")

    left_text.append("INGESTION SUMMARY\n", style="bold cyan")
    left_text.append("  • Raw Processed:   ")
    left_text.append(_format_size(db_metrics["total_bytes_processed"]), style="green")
    left_text.append("\n  • Unique Ingested: ")
    left_text.append(f"{db_metrics['total_docs_emitted']:,}", style="bold green")
    left_text.append("\n  • Dup Dropped:     ")
    left_text.append(f"{db_metrics['total_docs_dedup_dropped']:,}", style="yellow")
    left_text.append("\n  • Dedup Ratio:     ")
    left_text.append(f"{dedup_ratio:.2f}%", style="cyan")
    left_text.append("\n\n")

    left_text.append("DATABASE METRICS\n", style="bold cyan")
    left_text.append(f"  • Total Jobs:      {db_metrics['jobs_count']}\n")
    left_text.append(f"  • Tasks in Queue:  {db_metrics['tasks_count']}\n")
    left_text.append(f"  • Dedup Content:   {db_metrics['dedup_content_count']:,}\n")
    left_text.append(f"  • Dedup Near:      {db_metrics['dedup_near_count']:,}\n")
    left_text.append("  • DLQ Failures:    ")
    left_text.append(str(db_metrics["dlq_count"]), style="red" if db_metrics["dlq_count"] > 0 else "white")
    left_text.append("\n")

    layout["left"].update(
        Panel(left_text, title="[bold white]Telemetry & State[/bold white]", border_style="blue")
    )

    # 3. Right Top Panel: Jobs
    jobs = state.list_jobs(limit=5)
    if jobs:
        selected_job_idx = max(0, min(selected_job_idx, len(jobs) - 1))
    else:
        selected_job_idx = 0

    jobs_table = Table(expand=True, box=None)
    jobs_table.add_column("Job ID", style="cyan")
    jobs_table.add_column("Kind", style="white")
    jobs_table.add_column("Status", style="bold green")
    jobs_table.add_column("Tasks", style="white")
    jobs_table.add_column("Docs", style="green")
    jobs_table.add_column("Dedup", style="yellow")

    for idx_job, j in enumerate(jobs):
        status_color = (
            "green" if j.status.value == "completed" else "yellow" if j.status.value == "running" else "red"
        )
        is_selected = idx_job == selected_job_idx
        prefix = "→ " if is_selected else "  "

        job_id_text = f"{prefix}{j.job_id[:12]}"
        kind_text = j.kind.value
        status_text = f"[{status_color}]{j.status.value}[/{status_color}]"
        tasks_text = f"{j.tasks_completed}/{j.tasks_total}"
        docs_text = str(j.docs_emitted)
        dedup_text = str(j.docs_dedup_dropped)

        if is_selected:
            jobs_table.add_row(
                f"[bold reverse cyan]{job_id_text}[/bold reverse cyan]",
                f"[bold cyan]{kind_text}[/bold cyan]",
                f"[bold]{status_text}[/bold]",
                f"[bold cyan]{tasks_text}[/bold cyan]",
                f"[bold green]{docs_text}[/bold green]",
                f"[bold yellow]{dedup_text}[/bold yellow]",
            )
        else:
            jobs_table.add_row(
                job_id_text,
                kind_text,
                status_text,
                tasks_text,
                docs_text,
                dedup_text,
            )
    layout["right_top"].update(
        Panel(jobs_table, title="[bold white]Recent Jobs[/bold white]", border_style="magenta")
    )

    # 3.1 Right Middle Panel: Recent Captures
    captures_table = Table(expand=True, box=None)
    captures_table.add_column("Time", style="cyan")
    captures_table.add_column("Title", style="white")
    captures_table.add_column("Domain", style="dim white")

    try:
        captures_rows = idx.execute(
            """
            SELECT fetch_ts, title, domain, source_type
            FROM captures
            ORDER BY fetch_ts DESC
            LIMIT 10
            """
        )
    except Exception:
        captures_rows = []

    import re

    for r in captures_rows:
        fetch_ts = r.get("fetch_ts")
        time_str = ""
        if isinstance(fetch_ts, datetime):
            time_str = fetch_ts.strftime("%H:%M:%S")
        else:
            s = str(fetch_ts)
            m = re.search(r"(\d{2}):(\d{2}):(\d{2})", s)
            if m:
                time_str = m.group(0)
            else:
                time_str = s[:8]

        title = r.get("title") or "(No Title)"
        if len(title) > 50:
            title = title[:47] + "..."
        domain = r.get("domain") or "unknown"
        if len(domain) > 30:
            domain = domain[:27] + "..."

        captures_table.add_row(time_str, title, domain)

    layout["right_middle"].update(
        Panel(captures_table, title="[bold white]Recent Captures[/bold white]", border_style="cyan")
    )

    # 4. Right Bottom Panel: Storage sizes
    total_local_bytes = 0
    total_local_files = 0
    dirs_to_check = {
        "Staging JSONL": settings.staging_jsonl_dir(),
        "Iceberg Warehouse": settings.iceberg_warehouse,
        "SQLite State DB": settings.data_dir / "state" if settings.data_dir else None,
        "DuckDB Metadata": settings.duckdb_path(),
    }
    storage_table = Table(expand=True, box=None)
    storage_table.add_column("Component", style="cyan")
    storage_table.add_column("Files", justify="right", style="green")
    storage_table.add_column("Size", justify="right", style="bold green")
    storage_table.add_column("Path / Configuration", style="dim white")

    for label, path in dirs_to_check.items():
        if path and not _is_cloud_path(path):
            size, count = _get_path_size(path)
            total_local_bytes += size
            total_local_files += count
            storage_table.add_row(label, f"{count:,}", _format_size(size), str(path))
        else:
            storage_table.add_row(label, "-", "[blue]Cloud URI[/blue]", str(path))

    layout["right_bottom"].update(
        Panel(storage_table, title="[bold white]Disk Storage Breakdown[/bold white]", border_style="green")
    )

    # 5. Footer
    footer_text = Text(
        f"Data Root: {settings.data_dir}  |  Total Local Files: {total_local_files:,}  |  Disk Space: {_format_size(total_local_bytes)}",
        justify="center",
        style="dim cyan",
    )
    layout["footer"].update(Panel(footer_text, border_style="cyan"))

    return layout


def _get_key_nonblocking() -> str | None:
    import select
    import sys

    if not sys.stdin.isatty():
        return None
    try:
        import termios
        import tty
    except ImportError:
        return None

    fd = sys.stdin.fileno()
    try:
        old_settings = termios.tcgetattr(fd)
    except Exception:
        return None

    try:
        tty.setraw(fd)
        rlist, _, _ = select.select([sys.stdin], [], [], 0.05)
        if rlist:
            key = sys.stdin.read(1)
            if key == "\x1b":
                # Check for escape sequence
                rlist2, _, _ = select.select([sys.stdin], [], [], 0.01)
                if rlist2:
                    seq2 = sys.stdin.read(1)
                    rlist3, _, _ = select.select([sys.stdin], [], [], 0.01)
                    if rlist3:
                        seq3 = sys.stdin.read(1)
                        if seq2 == "[" and seq3 == "A":
                            return "up"
                        elif seq2 == "[" and seq3 == "B":
                            return "down"
                        elif seq2 == "[" and seq3 == "C":
                            return "right"
                        elif seq2 == "[" and seq3 == "D":
                            return "left"
                return "esc"
            return key
    except Exception:
        pass
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        except Exception:
            pass
    return None


def _make_tui_log_layout(settings: Any, log_type: str, scroll_offset: int) -> tuple[Any, int, int]:
    import os
    from datetime import datetime

    from rich.layout import Layout
    from rich.panel import Panel
    from rich.text import Text

    layout = Layout()
    layout.split_column(Layout(name="header", size=4), Layout(name="body"), Layout(name="footer", size=3))

    # 1. Header
    time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_name = "API LOGS (api.log)" if log_type == "api" else "APPLICATION LOGS (awareness.log)"
    header_text = Text.assemble(
        (" AWARENESS ENGINE LOG VIEWER ", "bold reverse yellow"),
        "  |  Local Time: ",
        (time_str, "yellow"),
        "  |  Controls: ",
        ("[Q] Quit  [L] Cycle Views  [TAB/S] Toggle Log  [Up/Down/J/K/U/D] Scroll  [G] Reset", "bold green"),
    )
    layout["header"].update(Panel(header_text, border_style="yellow"))

    # Get log file path
    if log_type == "api":
        log_path = settings.log_dir / "api.log"
    else:
        log_path = settings.log_dir / "awareness.log"

    lines_to_display = []
    total_lines = 0

    try:
        term_height = os.get_terminal_size().lines
    except Exception:
        term_height = 24
    visible_height = max(5, term_height - 9)

    if log_path.exists():
        try:
            with open(log_path, encoding="utf-8", errors="ignore") as f:
                all_lines = f.read().splitlines()
                total_lines = len(all_lines)

                # Apply scroll offset
                start_idx = max(0, total_lines - visible_height - scroll_offset)
                end_idx = max(0, total_lines - scroll_offset)
                lines_to_display = all_lines[start_idx:end_idx]
        except Exception as e:
            lines_to_display = [f"Error reading log file: {e}"]
    else:
        lines_to_display = [f"Log file does not exist at {log_path}"]

    # Render lines with color highlights
    body_text = Text()
    for line in lines_to_display:
        line_lower = line.lower()
        if "error" in line_lower or "critical" in line_lower:
            body_text.append(line + "\n", style="bold red")
        elif "warning" in line_lower or "warn" in line_lower:
            body_text.append(line + "\n", style="yellow")
        elif "info" in line_lower:
            body_text.append(line + "\n", style="green")
        elif "debug" in line_lower:
            body_text.append(line + "\n", style="dim white")
        else:
            body_text.append(line + "\n")

    panel_title = f"[bold white]{log_name} — Showing {len(lines_to_display)}/{total_lines} lines[/bold white]"
    layout["body"].update(Panel(body_text, title=panel_title, border_style="yellow"))

    # 3. Footer
    footer_text = Text(
        f"File: {log_path}  |  Scroll Offset: {scroll_offset} lines  |  Total Lines: {total_lines}",
        justify="center",
        style="dim yellow",
    )
    layout["footer"].update(Panel(footer_text, border_style="yellow"))

    return layout, total_lines, visible_height


# ── TUI analytics panel ────────────────────────────────────────────────────

# Key that jumps straight to the analytics panel (free in the current keymap:
# ``a`` is the API toggle, ``t`` the tail toggle on the dashboard).
_TUI_ANALYTICS_KEY = "y"
# Window (days, daily buckets) used for the per-term sparkline/spike/sentiment view.
_TUI_ANALYTICS_WINDOW_DAYS = 14
_TUI_ANALYTICS_TOP_TERMS = 10
_TUI_ANALYTICS_TOP_DOMAINS = 8
_TUI_ANALYTICS_SPARK_WIDTH = 40
# Lazy, once-per-TUI-session analytics index; built on first analytics render
# and closed by :func:`_close_tui_analytics_index` when the TUI exits.
# Held in a dict so the cache can be mutated without ``global`` statements.
_TUI_ANALYTICS_CACHE: dict[str, DuckDbIndex | None] = {"index": None}
# Per-(term, window) memo for the term view: the three analyses cost ~4.5 s
# on a 100k-doc corpus, so re-running them every 2 s refresh tick freezes
# the TUI. The cache is invalidated when the term changes.
_TUI_TERM_VIEW_CACHE: dict[str, Any] = {}


def _tui_analytics_index(settings: Any) -> DuckDbIndex:
    """Return the process-cached analytics index, building it lazily once."""
    if _TUI_ANALYTICS_CACHE["index"] is None:
        _TUI_ANALYTICS_CACHE["index"] = DuckDbIndex(
            db_path=settings.duckdb_path(),
            jsonl_dir=settings.staging_jsonl_dir(),
            iceberg_warehouse=settings.iceberg_warehouse,
        )
    return _TUI_ANALYTICS_CACHE["index"]


def _close_tui_analytics_index() -> None:
    """Close the lazy analytics index if the TUI ever opened one."""
    cached = _TUI_ANALYTICS_CACHE["index"]
    if cached is not None:
        try:
            cached.close()
        except Exception as exc:
            logger.info("tui_analytics_index_close_failed", err=str(exc))
        _TUI_ANALYTICS_CACHE["index"] = None


def _make_tui_analytics_layout(
    settings: Any, idx: DuckDbIndex | None = None, term: str | None = None
) -> Any:
    """Render the analytics panel: top terms + domains, or a per-term view.

    When *idx* is omitted the lazily-built analytics index (see
    :func:`_tui_analytics_index`) is used; callers may pass an explicit index
    (tests, mock) instead. Engine failures render an inline error instead of
    crashing the TUI refresh loop.
    """
    from rich.layout import Layout  # noqa: PLC0415
    from rich.panel import Panel  # noqa: PLC0415
    from rich.text import Text  # noqa: PLC0415

    if idx is None:
        idx = _tui_analytics_index(settings)

    layout = Layout()
    layout.split_column(Layout(name="header", size=4), Layout(name="body"), Layout(name="footer", size=3))

    # 1. Header
    time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header_text = Text.assemble(
        (" AWARENESS ENGINE ANALYTICS ", "bold reverse green"),
        "  |  Local Time: ",
        (time_str, "yellow"),
        "  |  Controls: ",
        (
            "[Q] Quit  [L] Cycle Views  [Y] Analytics  [T] Analyze Term  [ESC] Clear Term",
            "bold green",
        ),
    )
    layout["header"].update(Panel(header_text, border_style="green"))

    # 2. Body
    cleaned = (term or "").strip()
    if cleaned:
        body = _analytics_term_view(idx, cleaned)
    else:
        body = _analytics_overview(idx)
    layout["body"].update(Panel(body, border_style="green"))

    # 3. Footer
    index_path = getattr(idx, "_db_path", None)
    footer_text = Text(
        f"Index: {index_path}  |  Term: {cleaned or '(top terms)'}",
        justify="center",
        style="dim green",
    )
    layout["footer"].update(Panel(footer_text, border_style="green"))

    return layout


def _analytics_overview(idx: DuckDbIndex) -> Any:
    """Top-10 term counts + top-8 domain breakdown (index errors inline)."""
    from rich.console import Group  # noqa: PLC0415
    from rich.panel import Panel  # noqa: PLC0415
    from rich.table import Table  # noqa: PLC0415
    from rich.text import Text  # noqa: PLC0415

    try:
        from awareness.analytics.engine import TermFrequencyEngine  # noqa: PLC0415

        engine = TermFrequencyEngine(idx)
        top = engine.top_terms(limit=_TUI_ANALYTICS_TOP_TERMS, min_count=2)
        domains = engine.domain_breakdown(limit=_TUI_ANALYTICS_TOP_DOMAINS)
    except Exception as exc:
        return Text(f"[red]Analytics unavailable: {escape(str(exc))}[/red]")

    top_table = Table(expand=True, box=None)
    top_table.add_column("Term", style="cyan")
    top_table.add_column("Count", justify="right", style="bold green")
    if top:
        for tc in top:
            top_table.add_row(tc.term, str(tc.count))
    else:
        top_table.add_row("(no terms above min count)", "0")

    domains_table = Table(expand=True, box=None)
    domains_table.add_column("Domain", style="cyan")
    domains_table.add_column("Captures", justify="right", style="bold green")
    if domains:
        for dc in domains:
            domains_table.add_row(dc.domain, str(dc.count))
    else:
        domains_table.add_row("(no domains)", "0")

    return Group(
        Panel(top_table, title="[bold white]Top 10 Terms[/bold white]", border_style="blue"),
        Panel(domains_table, title="[bold white]Top 8 Domains[/bold white]", border_style="blue"),
    )


def _analytics_term_view(idx: DuckDbIndex, term: str) -> Any:
    """Per-term view: daily sparkline + count/z-score/sentiment table."""
    from rich.console import Group  # noqa: PLC0415
    from rich.table import Table  # noqa: PLC0415
    from rich.text import Text  # noqa: PLC0415

    cache_key = f"{term.strip().lower()}:{_TUI_ANALYTICS_WINDOW_DAYS}"
    if cache_key in _TUI_TERM_VIEW_CACHE:
        return _TUI_TERM_VIEW_CACHE[cache_key]

    try:
        from awareness.analytics.engine import TermFrequencyEngine  # noqa: PLC0415

        engine = TermFrequencyEngine(idx)
        buckets = engine.term_frequency_over_time(
            term, window_days=_TUI_ANALYTICS_WINDOW_DAYS, granularity="day"
        )
        spikes = engine.detect_spikes(
            term,
            window_days=_TUI_ANALYTICS_WINDOW_DAYS,
            zscore_threshold=_SPIKE_Z_THRESHOLD,
        )
    except Exception as exc:
        return Text(f"[red]Analytics unavailable: {escape(str(exc))}[/red]")

    if not buckets:
        return Text(
            f"[yellow]No captures found for {term!r} in the last "
            f"{_TUI_ANALYTICS_WINDOW_DAYS} days.[/yellow]"
        )

    counts = [b.count for b in buckets]
    zscores = _zscore_series(counts)
    spike_days = {s.bucket.date() for s in spikes}

    sentiment_scores: dict[datetime, float] = {}
    try:
        from awareness.sentiment.engine import SentimentEngine  # noqa: PLC0415

        sentiment_buckets = SentimentEngine(idx).term_sentiment_over_time(
            term, window_days=_TUI_ANALYTICS_WINDOW_DAYS, granularity="day"
        )
        sentiment_scores = {sb.ts: sb.avg_score for sb in sentiment_buckets}
    except ImportError:
        pass  # sentiment engine not installed — drop the column gracefully
    except Exception as exc:
        logger.info("tui_sentiment_skipped", term=term, err=str(exc))

    table = Table(expand=True, box=None)
    table.add_column("Date", style="cyan")
    table.add_column("Count", justify="right")
    table.add_column("Z", justify="right")
    if sentiment_scores:
        table.add_column("Sentiment", justify="right")
    for bucket, count, zscore in zip(buckets, counts, zscores, strict=True):
        marked = " !" if bucket.ts.date() in spike_days else ""
        row = [f"{bucket.ts:%Y-%m-%d}", str(count), f"{zscore:.2f}{marked}"]
        if sentiment_scores:
            row.append(f"{sentiment_scores.get(bucket.ts, 0.0):+.2f}")
        table.add_row(*row)

    result = Group(
        Text(f"Term: {term!r} — {_TUI_ANALYTICS_WINDOW_DAYS}-day daily buckets", style="bold cyan"),
        Text(f"Spikes: {len(spikes)} detected  |  Sparkline:", style="dim"),
        Text(_sparkline([float(c) for c in counts], width=_TUI_ANALYTICS_SPARK_WIDTH)),
        table,
    )
    _TUI_TERM_VIEW_CACHE[cache_key] = result
    return result


@app.command(name="tui")
def tui(refresh_rate: float = typer.Option(2.0, "--refresh", "-r", help="Refresh rate in seconds")) -> None:
    """Launch the interactive Terminal User Interface (TUI) dashboard."""
    state, planner = _bootstrap()
    settings = get_settings()
    idx = DuckDbIndex(
        db_path=settings.duckdb_path(),
        jsonl_dir=settings.staging_jsonl_dir(),
        iceberg_warehouse=settings.iceberg_warehouse,
    )
    selected_job_idx = 0

    import os
    import signal
    import subprocess
    import sys
    import time

    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text

    # Clear screen before starting
    print("\033[H\033[2J\033[3J", end="")

    status_msg = ""
    last_update = 0.0

    current_view = "dashboard"
    log_scroll_offset = 0
    max_scroll = 0
    visible_log_height = 10
    analytics_term: str | None = None

    def compact_action() -> str:
        pending = state.list_pending_manifests()
        if not pending:
            return "[green]No staging files pending compaction.[/green]"
        from awareness.storage.iceberg import IcebergWriter

        try:
            writer = IcebergWriter(
                catalog_db=settings.iceberg_catalog_db, warehouse=settings.iceberg_warehouse
            )
            writer.ensure_table()
            compacted = 0
            for item in pending:
                p = Path(item["path"])
                if not p.exists() and settings.data_dir:
                    p = settings.data_dir / p
                if not p.exists():
                    state.mark_manifest_compacted(item["id"])
                    continue
                rows = []
                import gzip

                open_func = gzip.open if str(p).endswith(".gz") else open
                with open_func(p, "rt", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            rows.append(json.loads(line))
                if rows:
                    writer.append(rows)
                state.mark_manifest_compacted(item["id"])
                compacted += 1
            return f"[green]✔ Compacted {compacted} manifests successfully![/green]"
        except Exception as e:
            return f"[red]Compaction failed: {e}[/red]"

    def toggle_tail_action() -> str:
        tail_info = state.get_tail()
        if tail_info.get("running"):
            job_id = tail_info.get("job_id")
            if job_id:
                # Cooperative stop request
                state.set_tail(False, note="tui-requested-stop")
                return "[yellow]Sent stop request to tail daemon.[/yellow]"
            return "[red]No active tail job ID to stop.[/red]"
        else:
            try:
                subprocess.Popen(
                    [sys.executable, "-c", "from awareness.tail.daemon import run; run()"],
                    start_new_session=True,
                )
                return "[green]Spawning live tail daemon in background...[/green]"
            except Exception as e:
                return f"[red]Failed to start tail: {e}[/red]"

    def toggle_api_action() -> str:
        pid = _get_api_pid()
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
                return "[yellow]Sent SIGTERM to API server.[/yellow]"
            except Exception as e:
                return f"[red]Failed to stop API: {e}[/red]"
        else:
            try:
                env = os.environ.copy()
                api_port = _default_api_port()
                env["AW_API_HOST"] = "127.0.0.1"
                env["AW_API_PORT"] = str(api_port)
                log_dir = settings.log_dir
                log_dir.mkdir(parents=True, exist_ok=True)
                api_log_path = log_dir / "api.log"
                state_dir = settings.data_dir / "state"
                state_dir.mkdir(parents=True, exist_ok=True)
                with open(api_log_path, "a", encoding="utf-8") as lf:
                    proc = subprocess.Popen(
                        [sys.executable, "-c", "from awareness.api.server import run; run()"],
                        env=env,
                        stdout=lf,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                (state_dir / "api.pid").write_text(str(proc.pid), encoding="utf-8")
                return f"[green]Spawning API server on http://127.0.0.1:{api_port}...[/green]"
            except Exception as e:
                return f"[red]Failed to start API: {e}[/red]"

    try:
        layout = _make_tui_layout(state, settings, idx, selected_job_idx)
        with Live(layout, refresh_per_second=10, screen=True) as live:
            while True:
                # Read keyboard non-blockingly
                key = _get_key_nonblocking()
                if key:
                    key_raw = key
                    key_lower = key.lower()
                    if key_lower == "q":
                        break
                    elif key_lower == "c" and current_view == "dashboard":
                        status_msg = "[yellow]Running staging data compaction...[/yellow]"
                        # Force immediate footer update
                        footer_content = Text.assemble(
                            (status_msg, "bold yellow"),
                            "  |  ",
                            (f"Data Root: {settings.data_dir}", "dim cyan"),
                        )
                        layout["footer"].update(Panel(footer_content, border_style="cyan"))
                        live.update(layout)
                        status_msg = compact_action()
                        last_update = 0.0  # Force immediate refresh
                    elif key_lower == "t" and current_view == "dashboard":
                        status_msg = toggle_tail_action()
                        last_update = 0.0
                    elif key_lower == "a" and current_view == "dashboard":
                        status_msg = toggle_api_action()
                        last_update = 0.0
                    elif key_lower == "r":
                        status_msg = "[cyan]Forcing refresh...[/cyan]"
                        last_update = 0.0
                    elif key_lower == "l":
                        if current_view == "dashboard":
                            current_view = "api_logs"
                        elif current_view == "api_logs":
                            current_view = "app_logs"
                        elif current_view == "app_logs":
                            current_view = "analytics"
                        else:
                            current_view = "dashboard"
                        log_scroll_offset = 0
                        status_msg = ""
                        last_update = 0.0
                    elif key_lower == _TUI_ANALYTICS_KEY:
                        current_view = "analytics"
                        log_scroll_offset = 0
                        status_msg = ""
                        last_update = 0.0
                    elif key_lower in ("\tab", "\t", "s") and current_view in ("api_logs", "app_logs"):
                        current_view = "app_logs" if current_view == "api_logs" else "api_logs"
                        log_scroll_offset = 0
                        last_update = 0.0
                    elif key_lower in ("up", "k") and current_view in ("api_logs", "app_logs"):
                        log_scroll_offset = min(max_scroll, log_scroll_offset + 1)
                        last_update = 0.0
                    elif key_lower in ("down", "j") and current_view in ("api_logs", "app_logs"):
                        log_scroll_offset = max(0, log_scroll_offset - 1)
                        last_update = 0.0
                    elif key_lower == "u" and current_view in ("api_logs", "app_logs"):
                        # page up
                        log_scroll_offset = min(
                            max_scroll, log_scroll_offset + max(1, visible_log_height - 2)
                        )
                        last_update = 0.0
                    elif key_lower == "d" and current_view in ("api_logs", "app_logs"):
                        # page down
                        log_scroll_offset = max(0, log_scroll_offset - max(1, visible_log_height - 2))
                        last_update = 0.0
                    elif key_raw == "G" and current_view in ("api_logs", "app_logs"):
                        log_scroll_offset = max_scroll
                        last_update = 0.0
                    elif key_lower == "g" and current_view in ("api_logs", "app_logs"):
                        log_scroll_offset = 0
                        last_update = 0.0
                    elif key_lower in ("up", "k") and current_view == "dashboard":
                        selected_job_idx = max(0, selected_job_idx - 1)
                        last_update = 0.0
                    elif key_lower in ("down", "j") and current_view == "dashboard":
                        jobs = state.list_jobs(limit=5)
                        selected_job_idx = min(len(jobs) - 1 if jobs else 0, selected_job_idx + 1)
                        last_update = 0.0
                    elif key_lower == "s" and current_view == "dashboard":
                        jobs = state.list_jobs(limit=5)
                        if jobs and 0 <= selected_job_idx < len(jobs):
                            sel_job = jobs[selected_job_idx]
                            from awareness.schemas.jobs import JobKind, JobStatus

                            if sel_job.status not in (
                                JobStatus.COMPLETED,
                                JobStatus.CANCELLED,
                                JobStatus.FAILED,
                            ):
                                live.stop()
                                print("\033[H\033[2J\033[3J", end="")
                                rprint("[bold red]Cancel Job[/bold red]")
                                confirm = typer.confirm(
                                    f"Are you sure you want to cancel job {sel_job.job_id}?"
                                )
                                if confirm:
                                    state.set_job_status(sel_job.job_id, JobStatus.CANCELLED)
                                    if sel_job.kind == JobKind.TAIL:
                                        state.set_tail(False, job_id=sel_job.job_id, note="tui-cancelled")
                                    status_msg = f"[yellow]Cancelled job {sel_job.job_id}[/yellow]"
                                else:
                                    status_msg = "[yellow]Cancellation aborted[/yellow]"
                                print("\033[H\033[2J\033[3J", end="")
                                live.start()
                            else:
                                status_msg = f"[red]Job {sel_job.job_id} is already completed/cancelled[/red]"
                        else:
                            status_msg = "[red]No job selected to cancel[/red]"
                        last_update = 0.0
                    elif key_lower == "d" and current_view == "dashboard":
                        jobs = state.list_jobs(limit=5)
                        if jobs and 0 <= selected_job_idx < len(jobs):
                            sel_job = jobs[selected_job_idx]
                            from awareness.schemas.jobs import JobStatus

                            if sel_job.status != JobStatus.RUNNING:
                                live.stop()
                                print("\033[H\033[2J\033[3J", end="")
                                rprint("[bold red]Delete Job[/bold red]")
                                confirm = typer.confirm(
                                    f"Are you sure you want to delete job {sel_job.job_id}?"
                                )
                                if confirm:
                                    state.delete_job(sel_job.job_id)
                                    status_msg = f"[green]Deleted job {sel_job.job_id}[/green]"
                                    selected_job_idx = max(0, selected_job_idx - 1)
                                else:
                                    status_msg = "[yellow]Deletion aborted[/yellow]"
                                print("\033[H\033[2J\033[3J", end="")
                                live.start()
                            else:
                                status_msg = f"[red]Cannot delete running job {sel_job.job_id}[/red]"
                        else:
                            status_msg = "[red]No job selected to delete[/red]"
                        last_update = 0.0
                    elif key_lower == "n" and current_view == "dashboard":
                        live.stop()
                        print("\n\033[H\033[2J\033[3J", end="")
                        rprint("[bold cyan]=== Create New Job ===[/bold cyan]\n")
                        job_type = ""
                        while job_type not in ("backfill", "tail"):
                            job_type = (
                                typer.prompt("Job Type (backfill or tail)", default="backfill")
                                .strip()
                                .lower()
                            )
                        if job_type == "backfill":
                            start_str = typer.prompt(
                                "Start date (ISO or relative, e.g. '1 day ago', '2026-06-05')"
                            ).strip()
                            end_str = typer.prompt(
                                "End date (ISO, relative, or 'now')", default="now"
                            ).strip()
                            sources_str = typer.prompt(
                                "Sources (comma-separated, e.g. 'CC-WET,FineWeb,GDELT')",
                                default="CC-WET,FineWeb,GDELT",
                            ).strip()
                            domains_str = typer.prompt(
                                "Domain filters (comma-separated, optional)", default=""
                            ).strip()
                            match_str = typer.prompt(
                                "Match keywords (comma-separated, optional)", default=""
                            ).strip()

                            from awareness.schemas.doc import SourceKind

                            sources_list = [s.strip() for s in sources_str.split(",") if s.strip()]
                            src_kinds = []
                            for s in sources_list:
                                s_clean = s.strip().lower().replace("-", "_")
                                if s_clean in ("cc_wet", "common_crawl_wet", "wet"):
                                    src_kinds.append(SourceKind.COMMON_CRAWL_WET)
                                elif s_clean in ("fineweb", "fw"):
                                    src_kinds.append(SourceKind.FINEWEB)
                                elif s_clean in ("gdelt",):
                                    src_kinds.append(SourceKind.GDELT)
                                elif s_clean in ("rss",):
                                    src_kinds.append(SourceKind.RSS)
                                elif s_clean in ("sitemap",):
                                    src_kinds.append(SourceKind.SITEMAP)
                                else:
                                    try:
                                        src_kinds.append(SourceKind(s_clean))
                                    except ValueError:
                                        pass
                            domains_list = [d.strip() for d in domains_str.split(",") if d.strip()] or None
                            matches = [m.strip() for m in match_str.split(",") if m.strip()]
                            from awareness.schemas.jobs import BackfillRequest

                            start_dt = to_utc(start_str)
                            try:
                                end_dt = coerce_relative_end(end_str)
                            except ValueError:
                                # M-03 parity: unparseable end in the TUI falls
                                # back to "now" instead of crashing the REPL.
                                end_dt = coerce_relative_end("now")
                            req = BackfillRequest(
                                start=start_dt,
                                end=end_dt,
                                sources=src_kinds,
                                domains=domains_list,
                                match=matches,
                                match_all=False,
                                match_regex=False,
                                match_field="both",
                            )
                            job_id = planner.submit_backfill(req)
                            subprocess.Popen(
                                [
                                    sys.executable,
                                    "-m",
                                    "awareness.cli.main",
                                    "backfill",
                                    "run",
                                    job_id,
                                    "--silent-progress",
                                ],
                                start_new_session=True,
                            )
                            status_msg = f"[green]Launched backfill job: {job_id}[/green]"
                        else:  # tail
                            duration_str = typer.prompt(
                                "Duration in seconds (0 for infinite)", default="0"
                            ).strip()
                            sources_str = typer.prompt(
                                "Sources (comma-separated, e.g. 'RSS,GDELT')", default="RSS,GDELT"
                            ).strip()
                            match_str = typer.prompt(
                                "Match keywords (comma-separated, optional)", default=""
                            ).strip()
                            try:
                                duration = int(duration_str)
                            except ValueError:
                                duration = 0
                            sources_list = [s.strip().lower() for s in sources_str.split(",") if s.strip()]
                            use_gdelt = "gdelt" in sources_list
                            matches = [m.strip() for m in match_str.split(",") if m.strip()]
                            from awareness.tail.engine import _load_seeds

                            seeds = _load_seeds(None)
                            if matches:
                                seeds = {
                                    **seeds,
                                    "match": matches,
                                    "match_all": False,
                                    "match_regex": False,
                                    "match_field": "both",
                                }
                            job_id = planner.submit_tail(seeds)
                            cmd = [
                                sys.executable,
                                "-m",
                                "awareness.cli.main",
                                "tail",
                                "start",
                                "--no-interactive",
                                "--duration",
                                str(duration),
                                "--job-id",
                                job_id,
                            ]
                            if use_gdelt:
                                cmd.append("--gdelt")
                            else:
                                cmd.append("--no-gdelt")
                            for m in matches:
                                cmd.extend(["--match", m])
                            subprocess.Popen(cmd, start_new_session=True)
                            status_msg = f"[green]Launched tail job: {job_id}[/green]"
                        print("\033[H\033[2J\033[3J", end="")
                        live.start()
                        last_update = 0.0
                    elif key_lower == "t" and current_view == "analytics":
                        live.stop()
                        print("\n\033[H\033[2J\033[3J", end="")
                        rprint("[bold cyan]=== Analyze Term ===[/bold cyan]\n")
                        term_input = typer.prompt("Term to analyze (empty clears)").strip()
                        analytics_term = term_input or None
                        _TUI_TERM_VIEW_CACHE.clear()
                        print("\033[H\033[2J\033[3J", end="")
                        live.start()
                        status_msg = ""
                        last_update = 0.0
                    elif key_lower == "esc" and current_view == "analytics":
                        analytics_term = None
                        _TUI_TERM_VIEW_CACHE.clear()
                        status_msg = "[dim]Term cleared — showing top terms.[/dim]"
                        last_update = 0.0

                # Check refresh interval
                now = time.time()
                if now - last_update >= refresh_rate:
                    if current_view == "dashboard":
                        layout = _make_tui_layout(state, settings, idx, selected_job_idx)
                        if status_msg:
                            footer_content = Text.assemble(
                                (status_msg, "bold yellow"),
                                "  |  ",
                                (f"Data Root: {settings.data_dir}", "dim cyan"),
                            )
                            layout["footer"].update(Panel(footer_content, border_style="cyan"))
                    elif current_view == "analytics":
                        layout = _make_tui_analytics_layout(settings, term=analytics_term)
                        if status_msg:
                            footer_content = Text.assemble(
                                (status_msg, "bold yellow"),
                                "  |  ",
                                (f"Data Root: {settings.data_dir}", "dim cyan"),
                            )
                            layout["footer"].update(Panel(footer_content, border_style="cyan"))
                    else:
                        log_file_type = "api" if current_view == "api_logs" else "app"
                        layout, total_log_lines, visible_log_height = _make_tui_log_layout(
                            settings, log_file_type, log_scroll_offset
                        )
                        max_scroll = max(0, total_log_lines - visible_log_height)
                    live.update(layout)
                    last_update = now

                time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        # Close the lazy analytics index if the panel was ever opened.
        _close_tui_analytics_index()
        # Extra clear screen on exit
        print("\033[H\033[2J\033[3J", end="")
        rprint("[yellow]TUI Dashboard exited.[/yellow]")


def highlight_query(text: str, query: str) -> str:
    """Escapes rich tags in text and highlights query tokens case-insensitively using prefix boundaries."""
    escaped_text = escape(text or "")
    if not query:
        return escaped_text

    # Extract query tokens (word characters, length >= 2)
    terms = [t for t in re.findall(r"[\w']+", query.lower()) if len(t) >= 2]
    if not terms:
        return escaped_text

    terms.sort(key=len, reverse=True)

    # Compile a regex to match terms case-insensitively starting at word boundaries
    pattern = re.compile(r"\b(" + "|".join(re.escape(t) for t in terms) + r")\w*", re.IGNORECASE)

    def replace(m: re.Match) -> str:
        match_str = m.group(0)
        start, end = m.span()

        # Check if this match is inside an HTML entity (e.g., &amp;, &lt;, &gt;, &quot;, &#39;)
        # Search backwards for '&'
        amp_pos = -1
        for i in range(start - 1, -1, -1):
            c = escaped_text[i]
            if c == "&":
                amp_pos = i
                break
            if not (c.isalnum() or c == "#"):
                break

        if amp_pos != -1:
            # Search forwards for ';'
            semi_pos = -1
            for i in range(end, len(escaped_text)):
                c = escaped_text[i]
                if c == ";":
                    semi_pos = i
                    break
                if not (c.isalnum() or c == "#"):
                    break
            if semi_pos != -1:
                return match_str

        return f"[bold yellow]{match_str}[/bold yellow]"

    return pattern.sub(replace, escaped_text)


def highlight_tokens(text: str, query: str) -> str:
    return highlight_query(text, query)


@app.command(name="browse")
def browse(
    start: str = typer.Option(
        "", "--start", help="Start date range (empty = all time; e.g. '30 days ago', '2026-01-01')"
    ),
    end: str = typer.Option("now", "--end", help="End date range"),
    domain: str = typer.Option("", "--domain", help="Filter by domain"),
    source: str = typer.Option("", "--source", help="Filter by source"),
    language: str = typer.Option(
        "",
        "--lang",
        help="Filter by BCP-47 language tag (e.g. en, tr); case-insensitive",
    ),
    query: str = typer.Option("", "--query", "-q", help="Search query/terms to highlight"),
    unique: str = typer.Option(
        "none",
        "--unique",
        help="Collapse duplicates: none | content | group (newest fetch_ts per key)",
    ),
) -> None:
    """Interactively browse and read captured text documents from the terminal."""
    state, _ = _bootstrap()
    settings = get_settings()
    idx = DuckDbIndex(
        db_path=settings.duckdb_path(),
        jsonl_dir=settings.staging_jsonl_dir(),
        iceberg_warehouse=settings.iceberg_warehouse,
    )

    unique_mode = (unique or "none").strip().lower() or "none"
    try:
        fold_key = export_fold_key_sql(unique_mode)
    except ValueError as e:
        rprint(f"[red]{e}[/red]")
        raise typer.Exit(code=2) from e

    lang_filter = (language or "").strip().lower() or None

    # Empty start means no lower bound so historical backfills remain visible.
    start_dt = to_utc(start) if (start or "").strip() else None
    end_dt = inclusive_end(_coerce_end_checked(end))

    # Clear screen
    print("\033[H\033[2J\033[3J", end="")

    offset = 0
    limit = 10

    while True:
        where = ["fetch_ts <= $end"]
        params = {"end": end_dt}
        if start_dt is not None:
            where.append("fetch_ts >= $start")
            params["start"] = start_dt
        if domain:
            where.append("lower(domain) = $dom")
            params["dom"] = str(domain).strip().lower()
        if source:
            # Case-insensitive: RSS vs rss / Tail_Recrawl (API/search parity).
            where.append("lower(source_type) = $src")
            params["src"] = str(source).strip().lower()
        # BCP-47: primary tags (en) match regional subtags (en-US).
        append_language_filter(where, params, lang_filter)
        if query:
            terms = [t for t in re.findall(r"[A-Za-z0-9']+", query.lower()) if len(t) >= 2]
            if terms:
                for idx_term, term in enumerate(terms):
                    param_name = f"q_term_{idx_term}"
                    where.append(f"(title ILIKE ${param_name} OR text ILIKE ${param_name})")
                    params[param_name] = f"%{term}%"
            else:
                where.append("(title ILIKE $q_term OR text ILIKE $q_term)")
                params["q_term"] = f"%{query}%"

        where_sql = " AND ".join(where)
        browse_select = "doc_id, domain, title, fetch_ts, source_type, text, language"
        if fold_key is None:
            sql = f"""
                SELECT {browse_select}
                FROM captures
                WHERE {where_sql}
                ORDER BY fetch_ts DESC
                LIMIT {limit} OFFSET {offset}
            """
        else:
            sql = f"""
                SELECT * EXCLUDE (_fold_key) FROM (
                  SELECT DISTINCT ON ({fold_key})
                    {browse_select},
                    {fold_key} AS _fold_key
                  FROM captures
                  WHERE {where_sql}
                  ORDER BY {fold_key}, fetch_ts DESC
                ) _folded
                ORDER BY fetch_ts DESC
                LIMIT {limit} OFFSET {offset}
            """

        try:
            rows = idx.execute(sql, params)
        except Exception as e:
            rprint(f"[red]Query failed:[/red] {e}")
            break

        if not rows:
            if offset == 0:
                range_hint = ""
                if start_dt is not None or end_dt is not None or lang_filter or domain or source:
                    extras = []
                    if lang_filter:
                        extras.append(f"lang={lang_filter}")
                    if domain:
                        extras.append(f"domain={str(domain).strip().lower()}")
                    if source:
                        extras.append(f"source={str(source).strip().lower()}")
                    extra_sql = (", " + ", ".join(extras)) if extras else ""
                    range_hint = (
                        f" (filters: start={start_dt or '−∞'}, end={end_dt}"
                        f"{extra_sql}; "
                        "try widening --start/--end/--lang/--source/--domain)"
                    )
                rprint(f"[yellow]No captures found in this range.{range_hint}[/yellow]")
                break
            else:
                rprint("[yellow]No more pages. Going back...[/yellow]")
                offset = max(0, offset - limit)
                continue

        # Display table (surface active unique fold + filters so operators see mode)
        unique_label = f" unique={unique_mode}" if unique_mode != "none" else ""
        lang_label = f" lang={lang_filter}" if lang_filter else ""
        domain_label = f" domain={str(domain).strip().lower()}" if domain else ""
        source_label = f" source={str(source).strip().lower()}" if source else ""
        table = Table(
            title=(
                f"Awareness Documents - Page {offset // limit + 1} "
                f"(Offset: {offset}{unique_label}{lang_label}{domain_label}{source_label})"
            )
        )
        table.add_column("#", justify="center", style="yellow")
        table.add_column("Domain", style="cyan")
        table.add_column("Title", style="white")
        table.add_column("Date Captured", style="dim green")
        table.add_column("Source", style="magenta")
        table.add_column("Lang", style="dim cyan")

        for i, r in enumerate(rows, 1):
            title = r["title"] or "No Title"
            if len(title) > 50:
                title = title[:47] + "..."
            highlighted_title = highlight_tokens(title, query)
            table.add_row(
                str(i),
                r["domain"] or "N/A",
                highlighted_title,
                str(r["fetch_ts"])[:16],
                r["source_type"] or "N/A",
                r["language"] or "—",
            )

        console.print(table)

        rprint("\n[bold cyan]Navigation Commands:[/bold cyan]")
        rprint("  • [bold]n[/bold]     : Next page")
        rprint("  • [bold]p[/bold]     : Previous page")
        rprint("  • [bold]1-10[/bold]  : View document contents")
        rprint("  • [bold]q[/bold]     : Quit")

        cmd = typer.prompt("\nEnter command", default="n").strip().lower()
        if cmd == "q":
            break
        elif cmd == "n":
            offset += limit
        elif cmd == "p":
            offset = max(0, offset - limit)
        elif cmd.isdigit():
            idx_choice = int(cmd) - 1
            if 0 <= idx_choice < len(rows):
                doc = rows[idx_choice]
                # Clear and view doc
                print("\033[H\033[2J\033[3J", end="")
                rprint("[bold reverse cyan] DOCUMENT READ VIEW [/bold reverse cyan]\n")
                highlighted_title = highlight_tokens(doc["title"] or "No Title", query)
                rprint(f"[bold cyan]Title:[/bold cyan]       {highlighted_title}")
                rprint(f"[bold cyan]Domain:[/bold cyan]      {doc['domain']}")
                rprint(f"[bold cyan]Captured at:[/bold cyan] {doc['fetch_ts']}")
                rprint(f"[bold cyan]Source:[/bold cyan]      {doc['source_type']}")
                if doc["language"]:
                    rprint(f"[bold cyan]Language:[/bold cyan]    {doc['language']}")
                rprint(f"[bold cyan]Doc ID:[/bold cyan]      {doc['doc_id']}\n")
                rprint("-" * 80)

                # Display body text with word wrapping
                highlighted_body = highlight_tokens(doc["text"] or "[Empty Document]", query)
                rprint(highlighted_body)
                rprint("-" * 80)
                typer.prompt("\nPress ENTER to return to list", default="")
                print("\033[H\033[2J\033[3J", end="")
            else:
                rprint("[red]Invalid document index.[/red]")


def _print_search_domain_facets(facets: dict[str, Any] | None) -> None:
    """Print domain and source facet summaries when the search payload includes them."""
    if not facets:
        return

    def _facet_parts(items: object, *, name_keys: tuple[str, ...]) -> list[str]:
        if not isinstance(items, list) or not items:
            return []
        parts: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = ""
            for key in name_keys:
                name = str(item.get(key) or "").strip()
                if name:
                    break
            if not name:
                continue
            n = item.get("n")
            if n is not None:
                parts.append(f"{name} ({int(n)})")
            else:
                parts.append(name)
        return parts

    domains = _facet_parts(facets.get("domains") or [], name_keys=("domain",))
    if domains:
        rprint(f"[dim]Domains:[/dim] {', '.join(domains)}")

    sources = _facet_parts(
        facets.get("sources") or [],
        name_keys=("source_type", "source"),
    )
    if sources:
        rprint(f"[dim]Sources:[/dim] {', '.join(sources)}")

    languages = _facet_parts(facets.get("languages") or [], name_keys=("language", "lang"))
    if languages:
        rprint(f"[dim]Languages:[/dim] {', '.join(languages)}")


def _print_search_diagnostics(diagnostics: dict[str, Any] | None) -> None:
    """Render empty-result diagnostics as a short Rich panel.

    Always prints something for zero-hit searches: if the index omitted
    diagnostics or returned no hints, fall back to a generic suggestion so
    the CLI never silently ends with only "Found 0 documents".
    """
    from rich.panel import Panel

    diagnostics = diagnostics or {}
    hints = list(diagnostics.get("hints") or [])
    if not hints:
        corpus = diagnostics.get("corpus_size")
        if corpus is not None and int(corpus) <= 0:
            hints = ["No documents in index yet — run a backfill or start tail."]
        else:
            hints = ["No matches. Try fewer terms, substring mode, or a wider date window."]

    lines = "\n".join(f"• {escape(str(h))}" for h in hints)
    meta: list[str] = []
    if "corpus_size" in diagnostics:
        meta.append(f"corpus={diagnostics['corpus_size']}")
    if diagnostics.get("mode_used"):
        meta.append(f"mode={diagnostics['mode_used']}")
    filters = diagnostics.get("filters") or {}
    if isinstance(filters, dict):
        if filters.get("domain"):
            meta.append(f"domain={filters['domain']}")
        if filters.get("source"):
            meta.append(f"source={filters['source']}")
        if filters.get("language"):
            meta.append(f"language={filters['language']}")
    title = "No results — suggestions"
    if meta:
        title = f"{title} ({', '.join(meta)})"
    rprint(Panel(lines, title=f"[yellow]{title}[/yellow]", border_style="yellow", expand=False))


def _resolve_search_window(start: str, end: str) -> tuple[datetime | None, datetime | None]:
    """Resolve the (start, end) search window.

    Empty / "all" / "all time" start means NO lower bound (search the entire
    corpus) instead of a silent recent-window default that hid most captures.
    """
    s = (start or "").strip().lower()
    start_dt = None if s in ("", "all", "all time", "alltime", "any") else to_utc(start)
    end_dt = inclusive_end(_coerce_end_checked(end))
    return start_dt, end_dt


@app.command(name="search")
def search(
    query: str = typer.Argument(
        ...,
        help='Search query (BM25 when present; stem-prefix fallback). Wrap the whole query in double quotes for exact phrase match, e.g. "machine learning".',
    ),
    start: str = typer.Option("", "--start", help="Start date range (empty = beginning of corpus)"),
    end: str = typer.Option("now", "--end", help="End date range"),
    domain: str = typer.Option("", "--domain", help="Filter by domain"),
    source: str = typer.Option("", "--source", help="Filter by source"),
    language: str = typer.Option("", "--lang", help="Filter by BCP-47 language tag (e.g. en, tr)"),
    mode: str = typer.Option(
        "", "--mode", "-m", help="Match mode: auto | fts | prefix | substring (default from config)"
    ),
    fields: str = typer.Option(
        "",
        "--fields",
        "-f",
        help="Comma-list of columns to match: title,text,domain,url (default from config)",
    ),
    limit: int = typer.Option(0, "--limit", "-l", help="Results per page (0 = config default)"),
    max_results: int = typer.Option(
        0, "--max-results", help="Hard ceiling on rows returned (0 = config default; overload guard)"
    ),
    interactive: bool = typer.Option(
        True, "--interactive/--no-interactive", help="Enable interactive browsing of search results"
    ),
) -> None:
    """Search ingested documents.

    Matching is configurable. ``auto`` (the default) runs ranked full-text
    search and, only when it finds nothing, retries with stem-root prefix
    matching — so ``finance`` still surfaces ``financial``. Wrap the whole
    query in double quotes for an exact phrase match (e.g. ``"machine
    learning"``); results report ``mode=phrase``. ``--fields`` narrows what
    gets matched; ``--max-results`` caps how much comes back. Defaults come
    from ``config`` (search_default_mode / _fields / _limit /
    search_max_results) and can be overridden per-call here.
    """
    state, _ = _bootstrap()
    settings = get_settings()
    idx = DuckDbIndex(
        db_path=settings.duckdb_path(),
        jsonl_dir=settings.staging_jsonl_dir(),
        iceberg_warehouse=settings.iceberg_warehouse,
    )

    # Resolve per-call overrides against the persisted config defaults.
    mode = (mode or settings.search_default_mode).strip().lower()
    raw_fields = fields or settings.search_default_fields
    field_list = [f.strip().lower() for f in raw_fields.split(",") if f.strip()]
    limit = limit if limit > 0 else settings.search_default_limit
    max_results = max_results if max_results > 0 else settings.search_max_results

    # Empty/"all time" start means no lower bound so historical backfills remain searchable.
    start_dt, end_dt = _resolve_search_window(start, end)

    if not interactive or not sys.stdin.isatty():
        res = idx.search(
            query=query,
            limit=limit,
            offset=0,
            source=source if source else None,
            domain=domain if domain else None,
            language=language if language else None,
            start=start_dt,
            end=end_dt,
            mode=mode,
            fields=field_list,
            max_results=max_results,
        )
        total = res["total"]
        rows = res["rows"]
        ranked = res["ranked"]
        used_mode = res.get("mode", mode)
        capped = " [dim](capped)[/dim]" if total > len(rows) and len(rows) >= max_results else ""

        window = f"{start_dt.date() if start_dt else 'all time'} → {end_dt.date() if end_dt else 'now'}"
        rprint(f"[dim]Window: {window}[/dim]")
        rprint(
            f"[bold cyan]Search Results for:[/bold cyan] '{query}' "
            f"(Found {total} documents, showing top {len(rows)}{capped}, "
            f"Mode: {used_mode}, Fields: {','.join(res.get('fields', field_list))}, Ranked: {ranked})"
        )
        _print_search_domain_facets(res.get("facets"))
        rprint("-" * 80)
        for r in rows:
            title = r["title"] or "No Title"
            score_str = f" [score: {r['score']:.4f}]" if r["score"] is not None else ""
            highlighted_title = highlight_tokens(title, query)
            rprint(f"[bold white]• {highlighted_title}[/bold white]{score_str}")
            rprint(
                f"  [dim]Domain: {r['domain'] or 'N/A'} | Captured: {r['fetch_ts']} | Source: {r['source_type'] or 'N/A'}[/dim]"
            )
            if r.get("snippet"):
                highlighted_snippet = highlight_tokens(r["snippet"], query)
                rprint(f'  [italic]"{highlighted_snippet}"[/italic]')
            rprint()
        if total == 0:
            _print_search_diagnostics(res.get("diagnostics"))
        return

    offset = 0
    while True:
        print("\033[H\033[2J\033[3J", end="")

        res = idx.search(
            query=query,
            limit=limit,
            offset=offset,
            source=source if source else None,
            domain=domain if domain else None,
            language=language if language else None,
            start=start_dt,
            end=end_dt,
            mode=mode,
            fields=field_list,
            max_results=max_results,
        )

        total = res["total"]
        rows = res["rows"]
        ranked = res["ranked"]
        used_mode = res.get("mode", mode)

        if not rows:
            if offset == 0:
                range_hint = ""
                if start_dt is not None:
                    range_hint = f" (start={start_dt}; try --start '' or an earlier date)"
                rprint(f"[yellow]No documents matched query '{query}'.{range_hint}[/yellow]")
                _print_search_diagnostics(res.get("diagnostics"))
                break
            else:
                rprint("[yellow]No more pages. Going back...[/yellow]")
                offset = max(0, offset - limit)
                continue

        table = Table(
            title=f"Search Results for '{query}' - Page {offset // limit + 1} (Found {total} total, Mode: {used_mode}, Ranked: {ranked})"
        )
        table.add_column("#", justify="center", style="yellow")
        table.add_column("Score", style="magenta")
        table.add_column("Domain", style="cyan")
        table.add_column("Title / Snippet", style="white")
        table.add_column("Date Captured", style="dim green")

        for i, r in enumerate(rows, 1):
            score_val = f"{r['score']:.3f}" if r["score"] is not None else "N/A"
            title = r["title"] or "No Title"
            if len(title) > 60:
                title = title[:57] + "..."
            highlighted_title = highlight_tokens(title, query)
            snippet = r.get("snippet", "")
            if snippet:
                highlighted_snippet = highlight_tokens(snippet, query)
                title_and_snippet = f"[bold]{highlighted_title}[/bold]\n  [dim]{highlighted_snippet}[/dim]"
            else:
                title_and_snippet = f"[bold]{highlighted_title}[/bold]"

            table.add_row(str(i), score_val, r["domain"] or "N/A", title_and_snippet, str(r["fetch_ts"])[:16])

        console.print(table)
        _print_search_domain_facets(res.get("facets"))

        rprint("\n[bold cyan]Navigation Commands:[/bold cyan]")
        rprint("  • [bold]n[/bold]     : Next page")
        rprint("  • [bold]p[/bold]     : Previous page")
        rprint("  • [bold]1-10[/bold]  : View document contents")
        rprint("  • [bold]q[/bold]     : Quit search")

        cmd = typer.prompt("\nEnter command", default="n").strip().lower()
        if cmd == "q":
            break
        elif cmd == "n":
            offset += limit
        elif cmd == "p":
            offset = max(0, offset - limit)
        elif cmd.isdigit():
            idx_choice = int(cmd) - 1
            if 0 <= idx_choice < len(rows):
                doc_id = rows[idx_choice]["doc_id"]
                full_doc_rows = idx.execute(
                    "SELECT doc_id, title, domain, fetch_ts, source_type, text FROM captures WHERE doc_id = $id LIMIT 1",
                    {"id": doc_id},
                )
                if full_doc_rows:
                    doc = full_doc_rows[0]
                    print("\033[H\033[2J\033[3J", end="")
                    rprint("[bold reverse cyan] DOCUMENT READ VIEW [/bold reverse cyan]\n")
                    highlighted_title = highlight_tokens(doc["title"] or "No Title", query)
                    rprint(f"[bold cyan]Title:[/bold cyan]       {highlighted_title}")
                    rprint(f"[bold cyan]Domain:[/bold cyan]      {doc['domain']}")
                    rprint(f"[bold cyan]Captured at:[/bold cyan] {doc['fetch_ts']}")
                    rprint(f"[bold cyan]Source:[/bold cyan]      {doc['source_type']}")
                    rprint(f"[bold cyan]Doc ID:[/bold cyan]      {doc['doc_id']}\n")
                    rprint("-" * 80)

                    text_body = doc["text"] or "[Empty Document]"
                    highlighted_text = highlight_tokens(text_body, query)
                    rprint(highlighted_text)
                    rprint("-" * 80)
                    typer.prompt("\nPress ENTER to return to search results", default="")
                    print("\033[H\033[2J\033[3J", end="")
                else:
                    rprint("[red]Failed to load full document text.[/red]")
                    typer.prompt("\nPress ENTER to return")
            else:
                rprint("[red]Invalid document index.[/red]")


@app.command()
def compact(
    force: bool = typer.Option(
        False, "--force", help="Force compaction even if Iceberg is disabled in config"
    ),
    status: bool = typer.Option(
        False,
        "--status",
        help="List pending staging manifests without compacting",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Machine-readable pending status (implies --status)",
    ),
) -> None:
    """Compact local JSONL staging files into the durable Iceberg warehouse.

    Pass ``--status`` (or ``--json``) to inspect the compaction backlog without
    writing to Iceberg.
    """
    state, _ = _bootstrap()
    settings = get_settings()

    if status or as_json:
        summary = state.pending_manifest_summary()
        if as_json:
            print(json.dumps(summary, indent=2))
            return
        n = int(summary["pending_count"])
        if n == 0:
            rprint("[green]No staging files pending compaction.[/green]")
            return
        age_bits = ""
        age_s = summary.get("oldest_age_seconds")
        if age_s is not None:
            age_bits = f", oldest {_format_duration(float(age_s))}"
        rprint(
            f"[bold cyan]{n} manifest file(s) pending compaction[/bold cyan]  "
            f"({int(summary['total_records']):,} records, "
            f"{_format_size(int(summary['total_bytes']))}{age_bits})"
        )
        table = Table(show_header=True, header_style=f"bold {banner.C_HI}")
        table.add_column("id", justify="right")
        table.add_column("path")
        table.add_column("records", justify="right")
        table.add_column("size", justify="right")
        table.add_column("committed_at")
        table.add_column("age", justify="right")
        from datetime import UTC

        now_utc = datetime.now(UTC)
        for m in summary["manifests"]:
            age_cell = "—"
            raw_ca = m.get("committed_at")
            if raw_ca:
                try:
                    ca = datetime.fromisoformat(str(raw_ca).replace("Z", "+00:00"))
                    if ca.tzinfo is None:
                        ca = ca.replace(tzinfo=UTC)
                    age_cell = _format_duration(max(0.0, (now_utc - ca.astimezone(UTC)).total_seconds()))
                except ValueError:
                    age_cell = "—"
            table.add_row(
                str(m.get("id") or ""),
                str(m.get("path") or ""),
                f"{int(m.get('records') or 0):,}",
                _format_size(int(m.get("bytes") or 0)),
                str(m.get("committed_at") or "—"),
                age_cell,
            )
        console.print(table)
        return

    if not settings.enable_iceberg and not force:
        rprint("[yellow]Iceberg storage is disabled in configuration. Use --force to override.[/yellow]")
        return

    pending = state.list_pending_manifests()
    if not pending:
        rprint("[green]No staging files pending compaction.[/green]")
        return

    rprint(f"[bold cyan]Found {len(pending)} manifest files pending compaction.[/bold cyan]\n")

    import time

    from awareness.storage.iceberg import IcebergWriter

    assert settings.iceberg_catalog_db is not None
    assert settings.iceberg_warehouse is not None

    writer = IcebergWriter(catalog_db=settings.iceberg_catalog_db, warehouse=settings.iceberg_warehouse)
    writer.ensure_table()

    compacted_count = 0
    total_records = 0
    total_bytes = 0
    metrics = get_metrics()

    for item in pending:
        manifest_id = item["id"]
        path_str = item["path"]
        p = Path(path_str)

        if not p.exists():
            # If path is relative, try checking under settings.data_dir
            if not p.is_absolute() and settings.data_dir:
                p = settings.data_dir / p

        if not p.exists():
            rprint(f"[yellow]Manifest file not found: {path_str}. Marking as compacted (skipped).[/yellow]")
            state.mark_manifest_compacted(manifest_id)
            metrics.inc("iceberg.compact_manifests", labels={"outcome": "missing"})
            continue

        rprint(
            f"Compacting [cyan]{p.name}[/cyan] ({_format_size(item['bytes'])}, {item['records']} records)..."
        )

        # Read JSONL
        rows = []
        t0 = time.perf_counter()
        try:
            import gzip

            open_func = gzip.open if str(p).endswith(".gz") else open
            with open_func(p, "rt", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        rows.append(json.loads(line))
        except Exception as e:
            elapsed = max(0.0, time.perf_counter() - t0)
            metrics.observe(
                "iceberg.compact_seconds",
                elapsed,
                labels={"outcome": "read_error"},
            )
            metrics.inc("iceberg.compact_manifests", labels={"outcome": "read_error"})
            metrics.inc("iceberg.compact_errors", labels={"stage": "read"})
            rprint(f"[red]Failed to read JSONL file {p}: {e}[/red]")
            continue

        if rows:
            try:
                writer.append(rows)
                state.mark_manifest_compacted(manifest_id)
                elapsed = max(0.0, time.perf_counter() - t0)
                metrics.observe(
                    "iceberg.compact_seconds",
                    elapsed,
                    labels={"outcome": "ok"},
                )
                metrics.inc("iceberg.compact_manifests", labels={"outcome": "ok"})
                metrics.inc("iceberg.compacted_rows", value=float(len(rows)))
                compacted_count += 1
                total_records += len(rows)
                total_bytes += item["bytes"]
            except Exception as e:
                elapsed = max(0.0, time.perf_counter() - t0)
                metrics.observe(
                    "iceberg.compact_seconds",
                    elapsed,
                    labels={"outcome": "append_error"},
                )
                metrics.inc("iceberg.compact_manifests", labels={"outcome": "append_error"})
                metrics.inc("iceberg.compact_errors", labels={"stage": "append"})
                rprint(f"[red]Failed to append manifest {manifest_id} to Iceberg: {e}[/red]")
        else:
            # Empty file: still mark compacted so backlog drains.
            state.mark_manifest_compacted(manifest_id)
            elapsed = max(0.0, time.perf_counter() - t0)
            metrics.observe(
                "iceberg.compact_seconds",
                elapsed,
                labels={"outcome": "empty"},
            )
            metrics.inc("iceberg.compact_manifests", labels={"outcome": "empty"})
            compacted_count += 1

    rprint("\n[green]✔ Compaction completed successfully![/green]")
    rprint(f"  • Files Compacted: {compacted_count}/{len(pending)}")
    rprint(f"  • Total Records:   {total_records:,} docs")
    rprint(f"  • Total Size:      {_format_size(total_bytes)}")


@app.command()
def export(
    output: Path = typer.Option(
        ...,
        "--output",
        "--out",
        "-o",
        help="File path (jsonl) or folder (txt) for exported documents",
    ),
    domain: str = typer.Option("", "--domain", help="Filter documents by domain"),
    source: str = typer.Option("", "--source", help="Filter documents by source type"),
    format_type: str = typer.Option("jsonl", "--format", help="Export format: 'jsonl' or 'txt'"),
    limit: int = typer.Option(
        1000,
        "--limit",
        help="Max rows to export (0 = all matching)",
    ),
    unique: str = typer.Option(
        "none",
        "--unique",
        help="Collapse duplicates: none | content | group",
    ),
) -> None:
    """Export captured documents into a single JSONL file or raw text files folder."""
    state, _ = _bootstrap()
    settings = get_settings()
    idx = DuckDbIndex(
        db_path=settings.duckdb_path(),
        jsonl_dir=settings.staging_jsonl_dir(),
        iceberg_warehouse=settings.iceberg_warehouse,
    )

    rprint("[yellow]Fetching documents to export...[/yellow]")
    try:
        rows = query_export_captures(
            idx,
            limit=limit,
            unique=unique,
            domain=domain,
            source=source,
        )
    except ValueError as e:
        rprint(f"[red]{e}[/red]")
        raise typer.Exit(code=2) from e
    except Exception as e:
        rprint(f"[red]Failed to query captures: {e}[/red]")
        return

    if not rows:
        rprint("[yellow]No captures matched your filters.[/yellow]")
        return

    rprint(f"[bold cyan]Found {len(rows)} documents to export.[/bold cyan]")

    if format_type.lower() == "jsonl":
        try:
            n = write_export_jsonl(output, rows)
            rprint(
                f"[green]✔ Successfully exported {n} documents to JSONL file: [bold]{output}[/bold][/green]"
            )
        except Exception as e:
            rprint(f"[red]Export failed: {e}[/red]")
    elif format_type.lower() == "txt":
        try:
            output.mkdir(parents=True, exist_ok=True)
            written = 0
            for r in rows:
                doc_id = r["doc_id"]
                capture_id = r.get("capture_id") or ""
                safe_title = re.sub(r"[^0-9a-zA-Z\-_]", "", r["title"] or "")[:40]
                # M-05: include capture_id so duplicate doc_ids (re-captures of
                # the same URL) never overwrite each other's files.
                if capture_id:
                    filename = f"{safe_title}_{capture_id[:12]}.txt" if safe_title else f"{capture_id}.txt"
                else:
                    filename = f"{safe_title}_{doc_id[:8]}.txt" if safe_title else f"{doc_id}.txt"
                (output / filename).write_text(r["text"] or "", encoding="utf-8")
                written += 1
            rprint(
                f"[green]✔ Successfully exported {written} document text files to folder: "
                f"[bold]{output}[/bold][/green]"
            )
        except Exception as e:
            rprint(f"[red]Export failed: {e}[/red]")
    else:
        rprint(f"[red]Unknown format type '{format_type}'. Use 'jsonl' or 'txt'.[/red]")


@app.command(name="hf-push")
def hf_push(
    repo_id: str = typer.Argument(
        ..., help="Hugging Face Dataset Repository ID (e.g. 'username/my-dataset')"
    ),
    token: str = typer.Option(
        None, "--token", "-t", help="HF Write Token (or set HF_TOKEN environment variable)"
    ),
    private: bool = typer.Option(True, "--private/--public", help="Make the repository private or public"),
    domain: str = typer.Option("", "--domain", help="Filter documents by domain"),
    source: str = typer.Option("", "--source", help="Filter documents by source type"),
    limit: int = typer.Option(
        1000,
        "--limit",
        help="Max rows to push (0 = all matching) — mirrors `export`",
    ),
) -> None:
    """Push captured documents directly to the Hugging Face Dataset Hub."""
    state, _ = _bootstrap()
    settings = get_settings()

    try:
        from datasets import Dataset  # noqa: PLC0415
    except ImportError:
        rprint("[red]Hugging Face dependencies missing.[/red]")
        rprint(
            'Please install them using: [bold]pip install "awareness[hf]"[/bold] or [bold]uv pip install datasets huggingface-hub[/bold]'
        )
        return

    idx = DuckDbIndex(
        db_path=settings.duckdb_path(),
        jsonl_dir=settings.staging_jsonl_dir(),
        iceberg_warehouse=settings.iceberg_warehouse,
    )

    where = []
    params = {}
    if domain:
        where.append("lower(domain) = $dom")
        params["dom"] = str(domain).strip().lower()
    if source:
        where.append("lower(source_type) = $src")
        params["src"] = str(source).strip().lower()

    where_sql = " WHERE " + " AND ".join(where) if where else ""
    # L-01: bound the push (0 = all matching), mirroring `export --limit`.
    limit_sql = f" LIMIT {int(limit)}" if int(limit) > 0 else ""
    sql = f"""
        SELECT doc_id, capture_id, source_type, source_name, canonical_url, fetch_ts, domain, title, text, language
        FROM captures
        {where_sql}
        ORDER BY fetch_ts DESC
        {limit_sql}
    """

    rprint("[yellow]Querying documents from catalog...[/yellow]")
    try:
        rows = idx.execute(sql, params)
    except Exception as e:
        rprint(f"[red]Failed to query captures: {e}[/red]")
        return

    if not rows:
        rprint("[yellow]No captures found matching filters to export.[/yellow]")
        return

    rprint(f"[bold green]Found {len(rows)} documents to push.[/bold green]")
    rprint(f"Creating Hugging Face dataset and uploading to [bold cyan]{repo_id}[/bold cyan]...")

    try:
        dataset_dicts = []
        for r in rows:
            row_copy = dict(r)
            if row_copy.get("fetch_ts"):
                row_copy["fetch_ts"] = str(row_copy["fetch_ts"])
            dataset_dicts.append(row_copy)

        hf_dataset = Dataset.from_list(dataset_dicts)
        hf_dataset.push_to_hub(
            repo_id=repo_id,
            token=token,
            private=private,
        )
        rprint(
            f"[bold green]✔ Successfully pushed dataset to: https://huggingface.co/datasets/{repo_id}[/bold green]"
        )
    except Exception as e:
        rprint(f"[red]Failed to upload dataset to Hugging Face: {e}[/red]")


@dedup_app.command("check")
def dedup_check(
    url: str = typer.Option("", "--url", help="Canonical URL to check"),
    text: str = typer.Option("", "--text", help="Raw text snippet to check"),
    file_path: Path = typer.Option(None, "--file", help="Local file path to read text from"),
    threshold: int = typer.Option(
        DEFAULT_NEAR_THRESHOLD,
        "--threshold",
        min=0,
        max=128,
        help=f"Near-dup Hamming threshold (default: {DEFAULT_NEAR_THRESHOLD}, engine DEFAULT_NEAR_THRESHOLD)",
    ),
) -> None:
    """Check if a URL or text has already been ingested (exact or near-duplicate check)."""
    state, _ = _bootstrap()
    from awareness.storage.state import DedupRow
    from awareness.util.hashing import content_hash, hamming128, simhash128

    if not url and not text and not file_path:
        rprint("[red]Error: You must provide either --url, --text, or --file to inspect.[/red]")
        return

    text_content = ""
    if file_path:
        if not file_path.exists():
            rprint(f"[red]Error: File not found at {file_path}[/red]")
            return
        text_content = file_path.read_text(encoding="utf-8")
    elif text:
        text_content = text

    if text_content:
        c_hash = content_hash(text_content)
        rprint(f"Computed Exact Content Hash: [bold cyan]{c_hash}[/bold cyan]")

        with state.session() as s:
            match = s.get(DedupRow, c_hash)
            if match:
                rprint("[red]✖ EXACT DUPLICATE DETECTED![/red]")
                rprint(f"  • First Seen Doc ID: [bold]{match.first_doc_id}[/bold]")
                rprint(f"  • Ingested At:        {match.first_seen_at}")
                rprint(f"  • Ingest Match Count: {match.capture_count} captures")
            else:
                rprint("[green]✔ No exact duplicate content match found.[/green]")

        sh = simhash128(text_content)
        rprint(f"Computed Simhash Value:     [bold cyan]{sh:032x}[/bold cyan]")
        rprint(f"Near-dup Hamming threshold: [bold cyan]{threshold}[/bold cyan]")

        candidates = state.find_near_dup_candidates(sh)
        near_match = None
        min_dist = 128

        for doc_id, other_sig in candidates:
            dist = hamming128(sh, other_sig)
            if dist <= threshold and dist < min_dist:
                min_dist = dist
                near_match = doc_id

        if near_match:
            rprint(f"[yellow]⚠ NEAR-DUPLICATE DETECTED (Hamming Distance: {min_dist}/128)![/yellow]")
            rprint(f"  • Matching Doc ID:   [bold]{near_match}[/bold]")
        else:
            rprint("[green]✔ No near-duplicate content match found.[/green]")

    if url:
        rprint(f"Checking URL canonical mapping: [cyan]{url}[/cyan]...")
        settings = get_settings()
        idx = DuckDbIndex(
            db_path=settings.duckdb_path(),
            jsonl_dir=settings.staging_jsonl_dir(),
            iceberg_warehouse=settings.iceberg_warehouse,
        )
        try:
            res = idx.execute(
                "SELECT doc_id, fetch_ts, title FROM captures WHERE canonical_url = $url OR source_name = $url",
                {"url": url},
            )
            if res:
                rprint("[red]✖ URL HAS ALREADY BEEN CAPTURED![/red]")
                for r in res:
                    rprint(f"  • Doc ID:       [bold]{r['doc_id']}[/bold]")
                    rprint(f"  • Captured at:  {r['fetch_ts']}")
                    rprint(f"  • Title:        {r['title']}")
            else:
                rprint("[green]✔ No captured documents found for this URL.[/green]")
        except Exception as e:
            rprint(f"[yellow]Failed to check URL captures via index: {e}[/yellow]")


# ── shared config / destination helpers ─────────────────────────────────
_SOURCE_CHIP = {
    cfg_schema.SOURCE_ENV: f"[{banner.C_HI}]env[/]",
    cfg_schema.SOURCE_YAML: f"[{banner.C_FG}]yaml[/]",
    cfg_schema.SOURCE_DEFAULT: f"[{banner.C_FAINT}]default[/]",
}


def _gdrive_authorized() -> bool:
    """True if Google Drive credentials are on disk. Never raises."""
    try:
        from awareness.storage import gdrive  # noqa: PLC0415

        return bool(gdrive.is_authorized())
    except Exception:
        return False


def _destination_plan(settings: Any) -> cfg_schema.DestinationPlan:
    """Build the 'where TAIL writes' plan from the resolved settings."""
    return cfg_schema.describe_destinations(
        local=bool(settings.enable_jsonl_staging),
        s3=bool(settings.enable_iceberg),
        gdrive=bool(settings.enable_gdrive),
        data_dir=str(settings.data_dir) if settings.data_dir else None,
        warehouse=str(settings.iceberg_warehouse) if settings.iceberg_warehouse else None,
        gdrive_folder=settings.gdrive_folder_name,
        gdrive_authorized=_gdrive_authorized(),
    )


def _render_destination_plan(plan: cfg_schema.DestinationPlan) -> None:
    """Print the routing plan as a compact, colour-coded panel."""
    from rich.panel import Panel

    lines: list[str] = []
    for d in plan.destinations:
        mark = f"[bold {banner.C_FG}]●[/]" if d.enabled else f"[{banner.C_FAINT}]○[/]"
        state = f"[bold {banner.C_HI}]ON [/]" if d.enabled else f"[{banner.C_FAINT}]off[/]"
        lines.append(f"  {mark} {state}  [bold]{d.label}[/bold]")
        lines.append(f"        [{banner.C_DIM}]{escape(d.detail)}[/]")
        if d.enabled and d.warning:
            lines.append(f"        [yellow]⚠ {escape(d.warning)}[/yellow]")
    if plan.terminal_only:
        lines.append(
            "\n  [yellow]TERMINAL-ONLY[/yellow] — captures are displayed but [bold]not saved anywhere[/bold]."
        )
    body = "\n".join(lines)
    console.print(
        Panel(
            body,
            title=f"[bold {banner.C_HI}]Where TAIL writes captures[/]",
            title_align="left",
            border_style=banner.C_LINE,
        )
    )


# ── config subcommand group ─────────────────────────────────────────────
@config_app.command("show")
def config_show(
    section: str = typer.Option("", "--section", "-s", help="Only show one section (substring match)."),
    show_all: bool = typer.Option(
        False, "--all", "-a", help="Include every raw Settings field, not just documented knobs."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit the effective configuration as JSON."),
) -> None:
    """Show the current configuration, grouped by section, with each value's source."""
    settings = get_settings()
    yaml_data = _read_yaml_data()

    if json_out:
        from awareness.config.settings import Settings  # noqa: PLC0415

        keys = list(Settings.model_fields) if show_all else cfg_schema.all_keys()
        payload = {
            k: {
                "value": _jsonable(getattr(settings, k, None)),
                "source": cfg_schema.value_source(k, yaml_data, os.environ),
            }
            for k in keys
        }
        print(json.dumps(payload, indent=2, default=str))
        return

    console.print(banner.render_banner())
    rprint(
        f"\n[bold {banner.C_HI}]Awareness configuration[/]  [dim](file: {_get_yaml_config_path()})[/dim]\n"
    )

    for sect, fields in cfg_schema.fields_by_section().items():
        if section and section.lower() not in sect.lower():
            continue
        table = Table(box=None, padding=(0, 2), expand=False)
        table.add_column("Setting", style=f"bold {banner.C_HI}", no_wrap=True)
        table.add_column("Value", style=banner.C_FG)
        table.add_column("Source")
        table.add_column("Description", style=banner.C_DIM)
        for fld in fields:
            value = getattr(settings, fld.key, None)
            src = cfg_schema.value_source(fld.key, yaml_data, os.environ)
            table.add_row(fld.key, escape(str(value)), _SOURCE_CHIP.get(src, src), fld.description)
        rprint(f"[bold {banner.C_FG}]▸ {sect}[/]")
        console.print(table)
        rprint()

    if show_all:
        from awareness.config.settings import Settings  # noqa: PLC0415

        extra = [k for k in Settings.model_fields if cfg_schema.get_field(k) is None]
        if extra and not section:
            table = Table(box=None, padding=(0, 2))
            table.add_column("Setting", style=f"bold {banner.C_HI}", no_wrap=True)
            table.add_column("Value", style=banner.C_FG)
            table.add_column("Source")
            for k in extra:
                src = cfg_schema.value_source(k, yaml_data, os.environ)
                table.add_row(k, escape(str(getattr(settings, k, None))), _SOURCE_CHIP.get(src, src))
            rprint(f"[bold {banner.C_FG}]▸ Derived / advanced[/]")
            console.print(table)
            rprint()

    rprint(
        "[dim]Edit with [bold]awareness config set <key> <value>[/bold] · "
        "set TAIL destinations with [bold]awareness configure[/bold] · "
        "check health with [bold]awareness config doctor[/bold][/dim]"
    )


@config_app.command("get")
def config_get(
    key: str = typer.Argument(..., help="Config key (e.g. enable-gdrive or enable_gdrive)."),
    json_out: bool = typer.Option(False, "--json", help="Emit as JSON."),
) -> None:
    """Show a single config value, its source, type, default and description."""
    settings = get_settings()
    norm = cfg_schema.normalize_key(key)
    from awareness.config.settings import Settings  # noqa: PLC0415

    if norm not in Settings.model_fields:
        rprint(f"[red]Unknown config key '{escape(key)}'.[/red]")
        sugg = cfg_schema.suggest_keys(key)
        if sugg:
            rprint(f"[dim]Did you mean: {', '.join(sugg)}?[/dim]")
        raise typer.Exit(1)

    value = getattr(settings, norm, None)
    src = cfg_schema.value_source(norm, _read_yaml_data(), os.environ)
    fld = cfg_schema.get_field(norm)
    if json_out:
        print(
            json.dumps(
                {
                    "key": norm,
                    "value": _jsonable(value),
                    "source": src,
                    "type": fld.type_label if fld else None,
                    "default": _jsonable(fld.default) if fld else None,
                    "env_var": ("AW_" + norm.upper()),
                    "description": fld.description if fld else None,
                },
                indent=2,
                default=str,
            )
        )
        return

    rprint(
        f"[bold {banner.C_HI}]{norm}[/] = [bold {banner.C_FG}]{escape(str(value))}[/]  ({_SOURCE_CHIP.get(src, src)})"
    )
    if fld:
        rprint(f"  [dim]{fld.description}[/dim]")
        rprint(f"  [dim]type: {fld.type_label} · default: {fld.default} · env: {fld.env_var}[/dim]")
        if fld.choices:
            rprint(f"  [dim]choices: {', '.join(fld.choices)}[/dim]")
        if fld.minimum is not None or fld.maximum is not None:
            rprint(f"  [dim]range: {fld.minimum} … {fld.maximum}[/dim]")


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Config key (e.g. data-dir or data_dir)."),
    value: str = typer.Argument(..., help="Value to assign (validated & type-coerced)."),
) -> None:
    """Set a configuration value persistently (validated against the schema)."""
    norm = cfg_schema.normalize_key(key)
    from awareness.config.settings import Settings  # noqa: PLC0415

    if norm not in Settings.model_fields:
        rprint(f"[red]Error: '{escape(key)}' is not a valid configuration setting.[/red]")
        sugg = cfg_schema.suggest_keys(key)
        if sugg:
            rprint(f"[dim]Did you mean: {', '.join(sugg)}?[/dim]")
        raise typer.Exit(1)

    old_value = getattr(get_settings(), norm, None)
    fld = cfg_schema.get_field(norm)
    if fld is not None:
        typed, err = fld.coerce(value)
        if err is not None:
            rprint(f"[red]Invalid value for {norm}: {escape(err)}[/red]")
            raise typer.Exit(1)
    else:
        typed = _coerce_val(value)

    try:
        _set_yaml_values({norm: typed})
        reset_settings()
    except Exception as e:
        rprint(f"[red]Error writing config change: {escape(str(e))}[/red]")
        raise typer.Exit(1) from e
    rprint(
        f"[green]✔ {norm}[/green]: [dim]{escape(str(old_value))}[/dim] → "
        f"[bold {banner.C_FG}]{escape(str(typed))}[/]  [dim](saved to {_get_yaml_config_path().name})[/dim]"
    )


@config_app.command("unset")
def config_unset(
    key: str = typer.Argument(..., help="Config key to remove from the override file (reverts to default)."),
) -> None:
    """Remove a key from awareness.yaml so it falls back to its built-in default."""
    norm = cfg_schema.normalize_key(key)
    removed = _unset_yaml_value(norm)
    reset_settings()
    if removed:
        new_val = getattr(get_settings(), norm, None)
        rprint(
            f"[green]✔ Removed [bold]{norm}[/bold] — now using default:[/green] [bold {banner.C_FG}]{escape(str(new_val))}[/]"
        )
    else:
        rprint(
            f"[yellow]'{escape(norm)}' was not set in {_get_yaml_config_path().name}; nothing to remove.[/yellow]"
        )


@config_app.command("reset")
def config_reset(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Reset ALL configuration to defaults (clears awareness.yaml)."""
    path = _get_yaml_config_path()
    if not path.exists() or not _read_yaml_data():
        rprint("[yellow]Configuration is already at defaults (no overrides set).[/yellow]")
        return
    if not yes and not typer.confirm(
        f"Clear all overrides in {path.name} and revert to defaults?", default=False
    ):
        rprint("[dim]Aborted — no changes made.[/dim]")
        return
    _write_yaml_data({})
    reset_settings()
    rprint("[green]✔ Configuration reset to defaults.[/green]")


@config_app.command("path")
def config_path() -> None:
    """Print the configuration file path and whether it exists."""
    path = _get_yaml_config_path()
    exists = path.exists()
    rprint(f"[bold {banner.C_HI}]Config file:[/] {path}")
    rprint(f"  exists: {'[green]yes[/green]' if exists else '[yellow]no (using defaults)[/yellow]'}")
    if os.environ.get("AW_CONFIG_FILE"):
        rprint("  [dim](path comes from AW_CONFIG_FILE)[/dim]")
    env_over = sorted(k for k in os.environ if k.startswith("AW_") and k != "AW_CONFIG_FILE")
    if env_over:
        rprint(f"  [dim]active env overrides: {', '.join(env_over)}[/dim]")


@config_app.command("edit")
def config_edit() -> None:
    """Open the configuration file in $VISUAL / $EDITOR (falls back to nano/vi)."""
    path = _get_yaml_config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# Awareness configuration overrides. See `awareness config show`.\n", encoding="utf-8"
        )
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        from shutil import which  # noqa: PLC0415

        editor = next((e for e in ("nano", "vim", "vi") if which(e)), None)
    if not editor:
        rprint(f"[yellow]No editor found. Edit the file manually:[/yellow] {path}")
        raise typer.Exit(1)
    try:
        subprocess.call([editor, str(path)])
    except Exception as e:
        rprint(f"[red]Could not launch editor '{editor}': {escape(str(e))}[/red]")
        raise typer.Exit(1) from e
    reset_settings()
    rprint("[green]✔ Reloaded configuration. Run [bold]config validate[/bold] to verify.[/green]")


@config_app.command("validate")
def config_validate() -> None:
    """Validate the override file: flag unknown keys and out-of-range values."""
    from awareness.config.settings import Settings  # noqa: PLC0415

    data = _read_yaml_data()
    if not data:
        rprint("[green]✔ No overrides set — configuration is valid (all defaults).[/green]")
        return

    problems: list[str] = []
    warnings: list[str] = []
    for key, raw in data.items():
        norm = cfg_schema.normalize_key(str(key))
        if norm not in Settings.model_fields:
            warnings.append(f"unknown key '{key}' (ignored at runtime)")
            continue
        fld = cfg_schema.get_field(norm)
        if fld is not None:
            _, err = fld.coerce(raw)
            if err is not None:
                problems.append(f"{key}: {err}")

    # Ensure the merged config actually instantiates pydantic.
    try:
        Settings(**data)
    except Exception as e:
        problems.append(f"settings failed to load: {e}")

    for w in warnings:
        rprint(f"[yellow]⚠ {escape(w)}[/yellow]")
    for p in problems:
        rprint(f"[red]✖ {escape(p)}[/red]")
    if problems:
        rprint(f"\n[red]Configuration has {len(problems)} problem(s).[/red]")
        raise typer.Exit(1)
    rprint(
        f"[green]✔ Configuration is valid[/green] ({len(data)} override(s)"
        + (f", {len(warnings)} warning(s)" if warnings else "")
        + ")."
    )


@config_app.command("doctor")
def config_doctor() -> None:
    """Diagnose the write destinations: paths writable? cloud reachable? Drive authorized?"""
    settings = get_settings()
    plan = _destination_plan(settings)
    _render_destination_plan(plan)
    rprint(f"\n[bold {banner.C_HI}]Destination health[/]")

    ok = True

    # Local
    if settings.enable_jsonl_staging:
        target = settings.staging_jsonl_dir()
        writable = _is_writable_dir(target)
        rprint(
            f"  {'[green]✔[/green]' if writable else '[red]✖[/red]'} Local JSONL → {target}"
            + ("" if writable else "  [red](not writable)[/red]")
        )
        ok = ok and writable
    else:
        rprint("  [dim]○ Local JSONL disabled[/dim]")

    # S3 / Iceberg
    if settings.enable_iceberg:
        wh = str(settings.iceberg_warehouse)
        if _is_cloud_path(wh):
            rprint(
                f"  [yellow]●[/yellow] Cloud warehouse → {wh}  [dim](cannot verify credentials offline)[/dim]"
            )
            if not os.environ.get("AWS_ACCESS_KEY_ID"):
                rprint("      [yellow]⚠ AWS_ACCESS_KEY_ID is not set — S3 writes may fail.[/yellow]")
        else:
            writable = _is_writable_dir(Path(wh))
            rprint(
                f"  {'[green]✔[/green]' if writable else '[red]✖[/red]'} Local warehouse → {wh}"
                + ("" if writable else "  [red](not writable)[/red]")
            )
            ok = ok and writable
    else:
        rprint("  [dim]○ Iceberg warehouse disabled[/dim]")

    # Google Drive
    if settings.enable_gdrive:
        if _gdrive_authorized():
            rprint(f"  [green]✔[/green] Google Drive authorized → folder “{settings.gdrive_folder_name}”")
        else:
            rprint(
                "  [red]✖[/red] Google Drive enabled but NOT authorized — run [bold]awareness cloud auth-gdrive[/bold]"
            )
            ok = False
    else:
        rprint("  [dim]○ Google Drive disabled[/dim]")

    if plan.terminal_only:
        rprint("\n  [yellow]All destinations are off — captures will be shown but NOT saved.[/yellow]")
    rprint(
        f"\n[{'green' if ok else 'yellow'}]{'✔ All enabled destinations look healthy.' if ok else '⚠ Some destinations need attention (see above).'}[/]"
    )
    if not ok:
        raise typer.Exit(1)


@config_app.command("interactive")
def config_interactive() -> None:
    """Interactively browse and modify any documented configuration knob."""
    rprint(f"[bold {banner.C_HI}]Interactive Configuration Editor[/]\n")
    fields = list(cfg_schema.CONFIG_SCHEMA)
    while True:
        settings = get_settings()
        yaml_data = _read_yaml_data()
        for i, fld in enumerate(fields, 1):
            value = getattr(settings, fld.key, None)
            src = cfg_schema.value_source(fld.key, yaml_data, os.environ)
            rprint(
                f"  [{i:>2}] [bold]{fld.key}[/bold] = {escape(str(value))}  [dim]({src}) — {fld.description}[/dim]"
            )
        rprint("\n   [0] Save and exit")
        choice = typer.prompt("\nSelect a setting to modify [0-N]", default="0")
        if choice.strip() == "0":
            rprint("[green]Exited config editor.[/green]")
            break
        try:
            idx = int(choice) - 1
        except ValueError:
            rprint("[red]Please enter a valid number.[/red]")
            continue
        if not (0 <= idx < len(fields)):
            rprint("[red]Invalid selection.[/red]")
            continue
        fld = fields[idx]
        current = getattr(get_settings(), fld.key, None)
        new_raw = typer.prompt(f"New value for '{fld.key}' ({fld.type_label})", default=str(current))
        typed, err = fld.coerce(new_raw)
        if err is not None:
            rprint(f"[red]Invalid: {escape(err)}[/red]")
            continue
        _set_yaml_values({fld.key: typed})
        reset_settings()
        rprint(f"[green]✔ Set '{fld.key}' to '{escape(str(typed))}'.[/green]")


# ── configure: set WHERE the TAIL/BODY engine writes (the centrepiece) ───
_DESTINATION_KEYS = (
    "enable_jsonl_staging",
    "enable_iceberg",
    "enable_gdrive",
    "data_dir",
    "iceberg_warehouse",
    "gdrive_folder_name",
    "jsonl_compress",
    "tail_poll_seconds",
    "tail_gdelt",
    "tail_gdelt_max_urls",
    "tail_show_captures",
    "terminal_mute_duplicates",
    "storage_flush_records",
    "storage_flush_seconds",
)


@app.command()
def configure(
    local: bool = typer.Option(None, "--local/--no-local", help="Write captures to local JSONL + index."),
    s3: bool = typer.Option(None, "--s3/--no-s3", help="Write captures to the S3 / Iceberg warehouse."),
    gdrive: bool = typer.Option(None, "--gdrive/--no-gdrive", help="Upload captures to Google Drive."),
    terminal_only: bool = typer.Option(
        False,
        "--terminal-only",
        help="Disable ALL sinks (display captures only); overrides --local/--s3/--gdrive if combined.",
    ),
    data_dir: Path = typer.Option(None, "--data-dir", help="Local data root directory."),
    warehouse: str = typer.Option(
        None, "--warehouse", help="Iceberg warehouse (local path or s3://bucket/path)."
    ),
    gdrive_folder: str = typer.Option(None, "--gdrive-folder", help="Google Drive target folder name."),
    compress: bool = typer.Option(None, "--compress/--no-compress", help="Gzip local JSONL staging files."),
    flush_records: int = typer.Option(
        None, "--flush-records", help="Flush the write buffer after N records."
    ),
    flush_seconds: float = typer.Option(
        None, "--flush-seconds", help="Flush the write buffer at least every N seconds."
    ),
    poll_seconds: float = typer.Option(None, "--poll-seconds", help="Tail seed re-arm interval in seconds."),
    gdelt: bool = typer.Option(None, "--gdelt/--no-gdelt", help="Follow the GDELT firehose while tailing."),
    gdelt_max_urls: int = typer.Option(
        None, "--gdelt-max-urls", help="Cap URLs pulled per 15-min GDELT slot."
    ),
    mute_duplicates: bool = typer.Option(
        None, "--mute-duplicates/--no-mute-duplicates", help="Mute duplicate captures in terminal logging."
    ),
    show: bool = typer.Option(False, "--show", help="Print the current write-destination plan and exit."),
    reset: bool = typer.Option(False, "--reset", help="Reset destination/tail settings to defaults."),
    non_interactive: bool = typer.Option(
        False, "--non-interactive", help="Never prompt; apply flags (or do nothing)."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompts."),
) -> None:
    """Configure WHERE the engine writes captures — before you start a tail.

    Run it with no flags for an interactive wizard, or pass flags to script it:

      awareness configure --local --no-s3 --no-gdrive
      awareness configure --gdrive --gdrive-folder "My Captures"
      awareness configure --terminal-only
      awareness configure --show                  (print the plan, change nothing)
      awareness configure --non-interactive       (print the plan, change nothing)

    The choices persist to awareness.yaml, and `awareness tail start` honours
    them automatically (no more per-launch prompts).
    """
    settings = get_settings()

    if show:
        _render_destination_plan(_destination_plan(settings))
        rprint(
            "[dim]Change with [bold]awareness configure[/bold] (wizard) or flags like [bold]--local/--no-s3[/bold].[/dim]"
        )
        return

    if reset:
        if (
            not yes
            and sys.stdin.isatty()
            and not typer.confirm("Reset destination & tail settings to defaults?", default=False)
        ):
            rprint("[dim]Aborted — no changes made.[/dim]")
            return
        for k in _DESTINATION_KEYS:
            _unset_yaml_value(k)
        reset_settings()
        rprint("[green]✔ Destination & tail settings reset to defaults.[/green]\n")
        _render_destination_plan(_destination_plan(get_settings()))
        return

    decisive = terminal_only or any(
        v is not None
        for v in (
            local,
            s3,
            gdrive,
            data_dir,
            warehouse,
            gdrive_folder,
            compress,
            flush_records,
            flush_seconds,
            poll_seconds,
            gdelt,
            gdelt_max_urls,
            mute_duplicates,
        )
    )

    if not decisive and not non_interactive:
        values = _configure_wizard(settings)
        if values is None:
            rprint("[dim]Aborted — no changes made.[/dim]")
            return
    elif not decisive:  # --non-interactive with no flags → show + hint, change nothing
        _render_destination_plan(_destination_plan(settings))
        rprint(
            "[dim]No flags given. Pass e.g. [bold]--local --no-s3[/bold], or run without --non-interactive for the wizard.[/dim]"
        )
        return
    else:
        values, errors = _configure_from_flags(
            settings,
            local=local,
            s3=s3,
            gdrive=gdrive,
            terminal_only=terminal_only,
            data_dir=data_dir,
            warehouse=warehouse,
            gdrive_folder=gdrive_folder,
            compress=compress,
            flush_records=flush_records,
            flush_seconds=flush_seconds,
            poll_seconds=poll_seconds,
            gdelt=gdelt,
            gdelt_max_urls=gdelt_max_urls,
            mute_duplicates=mute_duplicates,
        )
        if errors:
            for e in errors:
                rprint(f"[red]✖ {escape(e)}[/red]")
            raise typer.Exit(1)

    _set_yaml_values(values)
    reset_settings()
    new_settings = get_settings()
    rprint(f"[green]✔ Saved {len(values)} setting(s) to {_get_yaml_config_path().name}.[/green]\n")
    _render_destination_plan(_destination_plan(new_settings))
    rprint(
        f"\n[dim]Next:[/dim] [bold {banner.C_HI}]awareness tail start[/]  [dim](it will write to the destinations above)[/dim]"
    )


def _configure_from_flags(
    settings: Any,
    *,
    local: bool | None,
    s3: bool | None,
    gdrive: bool | None,
    terminal_only: bool,
    data_dir: Path | None,
    warehouse: str | None,
    gdrive_folder: str | None,
    compress: bool | None,
    flush_records: int | None,
    flush_seconds: float | None,
    poll_seconds: float | None,
    gdelt: bool | None,
    gdelt_max_urls: int | None,
    mute_duplicates: bool | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Translate non-interactive flags into a validated values mapping."""
    values: dict[str, Any] = {}
    errors: list[str] = []

    def put(key: str, raw: Any) -> None:
        fld = cfg_schema.get_field(key)
        if fld is None:
            values[key] = raw
            return
        typed, err = fld.coerce(raw)
        if err is not None:
            errors.append(f"{key}: {err}")
        else:
            values[key] = typed

    if terminal_only:
        if any(v is not None for v in (local, s3, gdrive)):
            rprint("[yellow]⚠ --terminal-only overrides --local/--s3/--gdrive — all sinks disabled.[/yellow]")
        values["enable_jsonl_staging"] = False
        values["enable_iceberg"] = False
        values["enable_gdrive"] = False
    else:
        if local is not None:
            values["enable_jsonl_staging"] = local
        if s3 is not None:
            values["enable_iceberg"] = s3
        if gdrive is not None:
            values["enable_gdrive"] = gdrive

    if data_dir is not None:
        values["data_dir"] = str(data_dir.resolve())
    if warehouse is not None:
        put("iceberg_warehouse", warehouse)
    if gdrive_folder is not None:
        put("gdrive_folder_name", gdrive_folder)
    if compress is not None:
        values["jsonl_compress"] = compress
    if flush_records is not None:
        put("storage_flush_records", flush_records)
    if flush_seconds is not None:
        put("storage_flush_seconds", flush_seconds)
    if poll_seconds is not None:
        put("tail_poll_seconds", poll_seconds)
    if gdelt is not None:
        values["tail_gdelt"] = gdelt
    if mute_duplicates is not None:
        values["terminal_mute_duplicates"] = mute_duplicates
    if gdelt_max_urls is not None:
        put("tail_gdelt_max_urls", gdelt_max_urls)

    # If S3 is being turned on without a cloud warehouse, nudge (not an error).
    if values.get("enable_iceberg") and not _is_cloud_path(
        values.get("iceberg_warehouse", settings.iceberg_warehouse)
    ):
        rprint(
            "[yellow]⚠ S3 enabled but warehouse is a local path. Pass --warehouse s3://bucket/path for true cloud storage.[/yellow]"
        )
    return values, errors


def _configure_wizard(settings: Any) -> dict[str, Any] | None:
    """Interactive walk-through. Returns the values mapping, or None if aborted."""
    rprint(f"[bold {banner.C_HI}]Awareness — capture destination setup[/]")
    rprint(
        "[dim]Choose where live & historical captures are written. Enter accepts the [bold]default[/bold] in brackets.[/dim]\n"
    )
    values: dict[str, Any] = {}

    try:
        # 1 ── Local JSONL + index
        rprint(
            f"[bold {banner.C_FG}]1) Local storage[/] [dim](JSONL files + the SQLite/DuckDB search index)[/dim]"
        )
        local = typer.confirm("   Write captures locally?", default=bool(settings.enable_jsonl_staging))
        values["enable_jsonl_staging"] = local
        if local:
            dd = typer.prompt("   Local data directory", default=str(settings.data_dir))
            values["data_dir"] = str(Path(dd).expanduser().resolve())
            values["jsonl_compress"] = typer.confirm(
                "   Gzip JSONL files (.jsonl.gz)?", default=bool(settings.jsonl_compress)
            )

        # 2 ── Cloud S3 / Iceberg
        rprint(f"\n[bold {banner.C_FG}]2) Cloud warehouse (S3 / Iceberg)[/]")
        s3 = typer.confirm(
            "   Write captures to the Iceberg warehouse?", default=bool(settings.enable_iceberg)
        )
        values["enable_iceberg"] = s3
        if s3:
            wh = typer.prompt(
                "   Warehouse (local path or s3://bucket/path)", default=str(settings.iceberg_warehouse)
            )
            values["iceberg_warehouse"] = wh.strip()
            if not _is_cloud_path(wh):
                rprint("   [yellow]⚠ That is a local path. Use s3://… for real cloud storage.[/yellow]")

        # 3 ── Google Drive
        rprint(f"\n[bold {banner.C_FG}]3) Google Drive[/]")
        if not _gdrive_authorized():
            rprint(
                "   [dim]Not authorized yet — run [bold]awareness cloud auth-gdrive[/bold] to connect an account.[/dim]"
            )
        gd = typer.confirm("   Upload captures to Google Drive?", default=bool(settings.enable_gdrive))
        values["enable_gdrive"] = gd
        if gd:
            folder = typer.prompt("   Drive folder name", default=settings.gdrive_folder_name)
            values["gdrive_folder_name"] = folder.strip() or settings.gdrive_folder_name
            if not _gdrive_authorized():
                rprint(
                    "   [yellow]⚠ Remember to authorize before tailing, or uploads will be skipped.[/yellow]"
                )

        # 4 ── Tail capture knobs
        rprint(f"\n[bold {banner.C_FG}]4) Live tail behaviour[/]")
        values["tail_poll_seconds"] = _wizard_number(
            "   Re-check feeds/sitemaps every N seconds", settings.tail_poll_seconds, "tail_poll_seconds"
        )
        gdelt = typer.confirm("   Follow the GDELT global-news firehose?", default=bool(settings.tail_gdelt))
        values["tail_gdelt"] = gdelt
        if gdelt:
            values["tail_gdelt_max_urls"] = int(
                _wizard_number(
                    "   Max URLs per 15-min GDELT slot", settings.tail_gdelt_max_urls, "tail_gdelt_max_urls"
                )
            )
        values["tail_show_captures"] = typer.confirm(
            "   Print each capture in the terminal as it lands?", default=bool(settings.tail_show_captures)
        )
    except (EOFError, KeyboardInterrupt):
        return None

    # 5 ── Summary + confirm
    rprint(f"\n[bold {banner.C_HI}]Review[/]")
    preview = cfg_schema.describe_destinations(
        local=values.get("enable_jsonl_staging", settings.enable_jsonl_staging),
        s3=values.get("enable_iceberg", settings.enable_iceberg),
        gdrive=values.get("enable_gdrive", settings.enable_gdrive),
        data_dir=values.get("data_dir", str(settings.data_dir) if settings.data_dir else None),
        warehouse=values.get(
            "iceberg_warehouse", str(settings.iceberg_warehouse) if settings.iceberg_warehouse else None
        ),
        gdrive_folder=values.get("gdrive_folder_name", settings.gdrive_folder_name),
        gdrive_authorized=_gdrive_authorized(),
    )
    _render_destination_plan(preview)
    try:
        if not typer.confirm("\nSave this configuration?", default=True):
            return None
    except (EOFError, KeyboardInterrupt):
        return None
    return values


def _wizard_number(prompt: str, current: Any, key: str) -> Any:
    """Prompt for a numeric value, validating against the schema; keep current on bad input."""
    raw = typer.prompt(prompt, default=str(current))
    fld = cfg_schema.get_field(key)
    if fld is None:
        return raw
    typed, err = fld.coerce(raw)
    if err is not None:
        rprint(f"   [yellow]⚠ {escape(err)} — keeping {current}.[/yellow]")
        return current
    return typed


def _jsonable(value: Any) -> Any:
    """Best-effort conversion of a settings value to a JSON-serialisable form."""
    if isinstance(value, Path):
        return str(value)
    return value


def _is_writable_dir(path: Path) -> bool:
    """True if ``path`` exists (or can be created) and accepts a write."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".awareness-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except Exception:
        return False


# ── cloud subcommand group ──────────────────────────────────────────────
@cloud_app.command("auth-gdrive")
def cloud_auth_gdrive() -> None:
    """Authenticate Google Drive storage target via OAuth2."""
    rprint("[bold blue]Google Drive API OAuth2 Setup[/bold blue]")
    rprint("Please go to the Google Cloud Console (https://console.cloud.google.com/),")
    rprint("create an OAuth 2.0 Client ID for a 'Desktop Application', and copy the credentials.")
    rprint("Ensure you have enabled the 'Google Drive API' in your API library.\n")

    client_id = typer.prompt("Enter Google Client ID", default="")
    if not client_id:
        rprint("[red]Client ID is required.[/red]")
        return
    client_secret = typer.prompt("Enter Google Client Secret", default="", hide_input=True)
    if not client_secret:
        rprint("[red]Client Secret is required.[/red]")
        return

    redirect_uri = "http://localhost:8086/"
    scopes = "https://www.googleapis.com/auth/drive.file https://www.googleapis.com/auth/drive"

    import urllib.parse

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scopes,
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)

    rprint("\n[bold green]Opening browser for authorization...[/bold green]")
    rprint(f"URL: {auth_url}\n")

    try:
        webbrowser.open(auth_url)
    except Exception:
        rprint("Failed to open browser automatically. Please open the URL manually.")

    code = None
    server = None
    try:
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                nonlocal code
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                parsed = urllib.parse.urlparse(self.path)
                qs = urllib.parse.parse_qs(parsed.query)
                if "code" in qs:
                    code = qs["code"][0]
                    self.wfile.write(
                        b"<h1>Authorization successful!</h1><p>You can close this tab and return to the terminal.</p>"
                    )
                else:
                    self.wfile.write(b"<h1>Authorization failed</h1><p>No authorization code received.</p>")

            def log_message(self, format, *args):
                pass

        server = HTTPServer(("127.0.0.1", 8086), CallbackHandler)
        server.timeout = 60.0  # 1 minute timeout
        rprint("Waiting for browser redirect on http://localhost:8086/...")
        server.handle_request()
    except Exception as e:
        rprint(f"[yellow]Could not start local redirect server ({e}). Falling back to manual entry.[/yellow]")

    if not code:
        code = typer.prompt("Please paste the 'code' parameter value from the redirect URL (or auth code)")

    if not code:
        rprint("[red]No authorization code provided. Auth failed.[/red]")
        return

    rprint("[yellow]Exchanging authorization code for tokens...[/yellow]")
    import httpx

    token_url = "https://oauth2.googleapis.com/token"
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    try:
        resp = httpx.post(token_url, data=payload, timeout=10.0)
        if resp.status_code == 200:
            tokens = resp.json()
            auth_data = {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": tokens.get("refresh_token"),
                "access_token": tokens.get("access_token"),
            }
            from awareness.storage import gdrive

            gdrive.save_auth(auth_data)
            rprint("[bold green]✔ Successfully authorized Google Drive![/bold green]")
            # Authorizing implies you want Drive as a destination — enable it so
            # `tail start` actually uploads (the upload path is gated on this flag).
            _set_yaml_values({"enable_gdrive": True})
            reset_settings()
            folder_name = get_settings().gdrive_folder_name
            rprint(
                "[green]✔ Enabled Google Drive uploads[/green] [dim](enable_gdrive=true; disable with `awareness config set enable_gdrive false`)[/dim]"
            )
            access_token = tokens.get("access_token")
            if access_token:
                folder_id = gdrive._get_or_create_folder(access_token)
                if folder_id:
                    rprint(f"[green]Folder '{escape(folder_name)}' resolved with ID: {folder_id}[/green]")
        else:
            rprint(f"[red]Token exchange failed: {resp.text}[/red]")
    except Exception as e:
        rprint(f"[red]Error exchanging token: {e}[/red]")


@cloud_app.command("status")
def cloud_status() -> None:
    """Display authorized and active cloud storage configurations."""
    settings = get_settings()
    rprint("[bold cyan]Cloud Storage Integration Status:[/bold cyan]\n")

    s3_active = settings.enable_iceberg
    s3_warehouse = settings.iceberg_warehouse
    s3_key = os.environ.get("AWS_ACCESS_KEY_ID")
    s3_endpoint = os.environ.get("AWS_ENDPOINT_URL")

    rprint("[bold]1. S3 / MinIO Cloud Storage[/bold]")
    rprint(f"  Iceberg cloud write enabled: {s3_active}")
    rprint(f"  Warehouse location:         {s3_warehouse}")
    rprint(f"  AWS Key configured:          {'Yes' if s3_key else 'No (using defaults/env)'}")
    rprint(f"  AWS Endpoint configured:     {s3_endpoint or 'Default AWS'}")

    from awareness.storage import gdrive

    gdrive_authorized = gdrive.is_authorized()
    rprint("\n[bold]2. Google Drive Storage[/bold]")
    rprint(
        f"  Authorized:                  {'[bold green]YES[/bold green]' if gdrive_authorized else '[yellow]NO[/yellow]'}"
    )
    rprint(
        f"  Upload destination enabled:  {'[bold green]YES[/bold green]' if settings.enable_gdrive else '[yellow]NO[/yellow]'}  [dim](enable_gdrive)[/dim]"
    )
    if settings.enable_gdrive and not gdrive_authorized:
        rprint("    [yellow]⚠ Enabled but not authorized — run `awareness cloud auth-gdrive`.[/yellow]")
    if gdrive_authorized:
        auth_data = gdrive.load_auth()
        if auth_data:
            rprint(f"    Client ID:               {str(auth_data.get('client_id'))[:10]}...")
            rprint(f"    Folder Name:             {escape(settings.gdrive_folder_name)}")


# ── interactive shell (full REPL control center) ─────────────────────────
_REPL_META = {"help", "?", "exit", "quit", "q", "clear", "cls", "commands", "menu"}


def _shell_click_command() -> Any:
    """Compile the Typer app to a Click command so the REPL gets every command."""
    from typer.main import get_command

    return get_command(app)


def _shell_command_names(click_cmd: Any) -> list[str]:
    try:
        return sorted(click_cmd.commands.keys())
    except Exception:
        return []


def _shell_subcommands(click_cmd: Any, group: str) -> list[str]:
    try:
        grp = click_cmd.commands.get(group)
        return sorted(grp.commands.keys())
    except Exception:
        return []


def _setup_shell_readline(click_cmd: Any, history_file: Path | None) -> bool:
    """Wire up arrow-key history + Tab completion. Returns True iff readline loaded
    (callers gate readline-only prompt escapes on this — see prompt_str)."""
    try:
        import readline
    except Exception:
        return False

    top = _shell_command_names(click_cmd) + sorted(_REPL_META)
    del top

    def completer(text: str, state: int) -> str | None:
        try:
            import shlex

            from awareness.schemas.doc import SourceKind

            buffer = readline.get_line_buffer()
            begidx = readline.get_begidx()

            # Parse words before the cursor
            prefix = buffer[:begidx]
            try:
                words = shlex.split(prefix)
            except Exception:
                words = prefix.split()

            # Strip leading slash and handle help
            if words:
                if words[0].startswith("/"):
                    words[0] = words[0][1:]
                if words[0] in ("help", "?"):
                    words = words[1:]
                    if words and words[0].startswith("/"):
                        words[0] = words[0][1:]

            normalized_words = [w.lower() for w in words]
            pool = []

            # Check for specific option values (e.g. source, match-field)
            if words and words[-1] in ("--source", "-s"):
                pool = [s.value for s in SourceKind]
            elif words and words[-1] == "--match-field":
                pool = ["title", "text", "both"]
            # Check for config key autocomplete
            elif (
                len(normalized_words) >= 2
                and normalized_words[-2] == "config"
                and normalized_words[-1] in ("get", "set", "unset")
            ):
                pool = [fld.key for fld in cfg_schema.CONFIG_SCHEMA]
            # Check for config key value autocomplete
            elif (
                len(normalized_words) >= 3
                and normalized_words[-3] == "config"
                and normalized_words[-2] == "set"
            ):
                key = normalized_words[-1]
                norm_key = key.replace("-", "_")
                fld = cfg_schema.get_field(norm_key)
                if fld:
                    if fld.kind == cfg_schema.KIND_BOOL:
                        pool = ["true", "false"]
                    elif fld.kind == cfg_schema.KIND_CHOICE and fld.choices:
                        pool = list(fld.choices)
            else:
                current_group = click_cmd
                for word in words:
                    if word.startswith("-"):
                        continue
                    if (
                        current_group
                        and hasattr(current_group, "commands")
                        and word in current_group.commands
                    ):
                        current_group = current_group.commands[word]
                    else:
                        current_group = None
                        break

                if current_group is not None:
                    if text.startswith("-"):
                        opts = []
                        if hasattr(current_group, "params"):
                            for param in current_group.params:
                                opts.extend(getattr(param, "opts", []))
                                opts.extend(getattr(param, "secondary_opts", []))
                        pool = opts
                    elif hasattr(current_group, "commands"):
                        pool = list(current_group.commands.keys())
                        if current_group == click_cmd:
                            pool = list(set(pool) | _REPL_META)
                    else:
                        pool = []

            has_slash = text.startswith("/")
            search_text = text[1:] if has_slash else text

            options = []
            for c in sorted(pool):
                if c.startswith(search_text):
                    options.append("/" + c if has_slash else c)

            return options[state] if state < len(options) else None
        except Exception:
            return None

    readline.set_completer(completer)
    readline.set_completer_delims(" \t\n")
    if "libedit" in (getattr(readline, "__doc__", "") or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")
    if history_file:
        try:
            if history_file.exists():
                readline.read_history_file(str(history_file))
            readline.set_history_length(2000)
        except Exception:
            pass
    return True


def _shell_help(click_cmd: Any) -> None:
    rprint("\n[bold cyan]Available Shell Commands:[/bold cyan]")
    console.print(banner.render_command_map())
    rprint(
        "\n[dim]In-shell extras:[/dim]  "
        "[bold]help <cmd>[/bold] or [bold]<cmd> --help[/bold] for flags  ·  "
        "[bold]↑/↓[/bold] history  ·  [bold]Tab[/bold] complete  ·  "
        "any command also works with a leading [bold]/[/bold]  ·  "
        "[bold]exit[/bold] / [bold]quit[/bold] to leave\n"
    )


def _shell_fork_exception_code(exc: BaseException) -> int | None:
    """Map vendored ``typer._click`` fork exceptions to exit codes.

    The installed typer ships its OWN click fork (``typer._click``) whose
    exception classes are distinct from the real ``click`` package — so the
    real-click ``except`` clauses below never match them and they used to fall
    into the generic handler, printing ``Error: 1`` noise (L-02). Returns
    ``None`` when *exc* is not a fork exception.
    """
    try:
        from typer import _click as typer_click  # noqa: PLC0415
    except Exception:
        return None
    try:
        if isinstance(exc, typer_click.exceptions.Exit):
            return int(getattr(exc, "exit_code", 0) or 0)
        if isinstance(exc, typer_click.exceptions.UsageError):
            rprint(f"[red]{escape(exc.format_message())}[/red]")
            rprint("[dim]Type [bold]help[/bold] for the command map, or [bold]<command> --help[/bold].[/dim]")
            return 2
        if isinstance(exc, typer_click.exceptions.Abort):
            rprint("[yellow]Aborted.[/yellow]")
            return 1
        if isinstance(exc, typer_click.exceptions.ClickException):
            rprint(f"[red]{escape(exc.format_message())}[/red]")
            return int(getattr(exc, "exit_code", 1) or 1)
    except Exception:
        return 1
    return None


def _shell_dispatch(click_cmd: Any, argv: list[str]) -> int:
    """Run one parsed command line through the full CLI — must survive any error.

    Returns the process-style exit code (``exc.exit_code`` on exit) so callers
    can mirror it; the REPL itself keeps running.
    """
    import click

    try:
        rv = click_cmd.main(args=argv, prog_name="awareness", standalone_mode=False)
        # This typer/click fork RETURNS the exit code for `ctx.exit(...)` /
        # `typer.Exit(...)` in non-standalone mode (see typer.core main()) —
        # mirror it silently instead of printing "Error: 1" (L-02).
        return int(rv) if isinstance(rv, int) else 0
    except click.exceptions.Exit as exc:
        # Defensive: other click versions propagate the exception instead.
        return int(exc.exit_code or 0)
    except SystemExit as exc:
        # Some paths still raise SystemExit (e.g. sys.exit from deeper code).
        code = exc.code
        return int(code) if isinstance(code, int) else 0
    except click.exceptions.UsageError as exc:
        rprint(f"[red]{escape(exc.format_message())}[/red]")
        rprint("[dim]Type [bold]help[/bold] for the command map, or [bold]<command> --help[/bold].[/dim]")
        return 2
    except click.exceptions.Abort:
        rprint("[yellow]Aborted.[/yellow]")
        return 1
    except click.exceptions.ClickException as exc:
        rprint(f"[red]{escape(exc.format_message())}[/red]")
        return int(exc.exit_code or 1)
    except KeyboardInterrupt:
        rprint("\n[yellow]Interrupted.[/yellow]")
        return 130
    except Exception as exc:
        code = _shell_fork_exception_code(exc)
        if code is not None:
            return code
        rprint(f"[red]Error:[/red] {escape(str(exc))}")
        return 1


@app.command(name="shell")
def shell() -> None:
    """Start the full interactive control center (REPL) — run ANY command."""
    import shlex

    click_cmd = _shell_click_command()
    is_tty = sys.stdin.isatty()

    console.print(
        banner.render_intro(
            _quickstart_context(),
            subtitle="Welcome to the Awareness Interactive Shell!",
        )
    )
    rprint(
        "[dim]Type any command (e.g. [bold]status[/bold], [bold]search climate[/bold]), "
        "[bold]help[/bold] for the map, or [bold]exit[/bold] to quit.[/dim]\n"
    )

    history_file: Path | None = None
    try:
        history_file = Path("~/.awareness_history").expanduser()
        if history_file.exists() and history_file.is_dir():
            raise ValueError("History path is a directory")
    except Exception:
        try:
            settings = get_settings()
            if settings.data_dir:
                history_file = settings.data_dir / "state" / "shell_history"
                history_file.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            history_file = None
    readline_ok = _setup_shell_readline(click_cmd, history_file) if is_tty else False

    state: StateDB | None = None

    def prompt_str() -> str:
        nonlocal state
        if not is_tty:
            return "awareness> "
        api = _get_api_pid() is not None or _is_port_active("127.0.0.1", _default_api_port())
        tail_on = False
        try:
            if state is None:
                state = _light_state()
            tail_on = bool(state.get_tail().get("running"))
        except Exception:
            pass
        if not readline_ok:
            # Without GNU readline the \001/\002 invisibility markers would print
            # as raw ^A/^B, so fall back to a plain, marker-free prompt.
            return f"awareness {'●' if api else '○'}api {'●' if tail_on else '○'}tail > "
        api_dot = "\001\033[32m\002●\001\033[0m\002" if api else "\001\033[90m\002○\001\033[0m\002"
        tail_dot = "\001\033[32m\002●\001\033[0m\002" if tail_on else "\001\033[90m\002○\001\033[0m\002"
        return (
            f"\001\033[1;36m\002awareness\001\033[0m\002 "
            f"{api_dot}api {tail_dot}tail \001\033[1;36m\002▸\001\033[0m\002 "
        )

    try:
        while True:
            try:
                raw = input(prompt_str())
            except EOFError:
                rprint("\n[yellow]Goodbye![/yellow]")
                break
            except KeyboardInterrupt:
                rprint("\n[dim](use [bold]exit[/bold] to quit)[/dim]")
                continue

            line = raw.strip()
            if not line:
                continue
            if line.startswith("/"):
                line = line[1:].strip()
                if not line:
                    continue
            low = line.lower()

            if low in ("exit", "quit", "q"):
                rprint("[yellow]Goodbye![/yellow]")
                break
            if low in ("clear", "cls"):
                print("\033[H\033[2J\033[3J", end="")
                continue
            if low in ("commands", "menu"):
                console.print(banner.render_command_map())
                continue
            if low == "shell":
                rprint("[dim]Already in the Awareness shell. Type [bold]exit[/bold] to leave.[/dim]")
                continue

            try:
                argv = shlex.split(line)
            except ValueError as exc:
                rprint(f"[red]Parse error:[/red] {escape(str(exc))}")
                continue
            if not argv:
                continue

            if argv[0].lower() in ("help", "?"):
                rest = argv[1:]
                if rest:
                    _shell_dispatch(click_cmd, [*rest, "--help"])
                else:
                    _shell_help(click_cmd)
                continue

            _shell_dispatch(click_cmd, argv)

            if is_tty and history_file:
                try:
                    import readline

                    readline.write_history_file(str(history_file))
                except Exception:
                    pass
    finally:
        if is_tty and history_file:
            try:
                import readline

                readline.write_history_file(str(history_file))
            except Exception:
                pass


@app.command(name="commands")
def commands_map() -> None:
    """Show the full, categorised map of every Awareness command."""
    console.print(banner.render_command_map())


@app.command()
def restart(
    host: str = typer.Option("127.0.0.1", "--host", help="Host address to bind to"),
    port: int = typer.Option(
        _default_api_port, "--port", help="Port to bind to (default: AW_API_PORT or 8085)"
    ),
    tail: bool = typer.Option(
        True, "--tail/--no-tail", help="Start the live tail daemon in-process after restart"
    ),
) -> None:
    """Restart the background Awareness API server (stop, then start)."""
    import time

    rprint("[cyan]Restarting Awareness API…[/cyan]")
    try:
        # Invoke stop with the same port so launchd labels stay consistent.
        stop(host=host, port=port)
    except Exception as exc:  # restart should proceed even if stop() hiccups
        rprint(f"[yellow]stop() reported: {escape(str(exc))}[/yellow]")
    time.sleep(1.0)
    start_args = ["start", "--host", host, "--port", str(port)]
    start_args.append("--tail" if tail else "--no-tail")
    _shell_dispatch(_shell_click_command(), start_args)


if __name__ == "__main__":
    app()
