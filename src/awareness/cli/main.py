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
    init                — initialize storage layout
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import socket
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any

import typer
import yaml
from rich import print as rprint
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from awareness.cli import banner
from awareness.config import get_settings, reset_settings
from awareness.config import schema as cfg_schema
from awareness.obs.logging import configure_logging, get_logger
from awareness.obs.metrics import get_metrics
from awareness.planner.planner import Planner
from awareness.schemas.doc import SourceKind
from awareness.schemas.jobs import BackfillRequest
from awareness.storage.duckdb_index import DuckDbIndex
from awareness.cli.export_util import export_fold_key_sql, query_export_captures, write_export_jsonl
from awareness.dedup.engine import DEFAULT_NEAR_THRESHOLD
from awareness.storage.state import StateDB
from awareness.tail.engine import TailEngine
from datetime import datetime

from awareness.util.timeutil import coerce_relative_end, inclusive_end, to_utc
from awareness.workers.engine import WorkerEngine

app = typer.Typer(no_args_is_help=False, help="Awareness — public text internet awareness engine")
backfill_app = typer.Typer(no_args_is_help=True, help="BODY: historical backfill")
tail_app = typer.Typer(no_args_is_help=True, help="TAIL: live capture")
service_app = typer.Typer(no_args_is_help=True, help="Manage launchd daemon service on macOS")
config_app = typer.Typer(no_args_is_help=True, help="Configure Awareness settings")
cloud_app = typer.Typer(no_args_is_help=True, help="Configure cloud storage integrations (Google Drive, S3)")
dedup_app = typer.Typer(no_args_is_help=True, help="Deduplication inspection & checks")

app.add_typer(backfill_app, name="backfill")
app.add_typer(tail_app, name="tail")
app.add_typer(service_app, name="service")
app.add_typer(config_app, name="config")
app.add_typer(cloud_app, name="cloud")
app.add_typer(dedup_app, name="dedup")

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
        with open(path, "r", encoding="utf-8") as fh:
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
        except (socket.timeout, ConnectionRefusedError):
            return False


def _get_api_pid() -> int | None:
    """Return the live API server PID, cleaning a stale pid file if needed."""
    settings = get_settings()
    pid_file = settings.data_dir / "state" / "api.pid"
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
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
        else:
            if not silent:
                rprint(f"[yellow]⚠ Failed to start tail daemon via API (status {r.status_code}): {r.text}[/yellow]")
    except Exception as e:
        if not silent:
            rprint(f"[yellow]⚠ Could not connect to API to start tail: {e}[/yellow]")


@app.command()
def start(
    host: str = typer.Option("127.0.0.1", "--host", help="Host address to bind to"),
    port: int = typer.Option(_default_api_port, "--port", help="Port to bind to (default: AW_API_PORT or 8085)"),
    tail: bool = typer.Option(True, "--tail/--no-tail", help="Start the live tail daemon in-process"),
    fg: bool = typer.Option(False, "--fg", help="Run in foreground (blocking)"),
    data_dir: Path = typer.Option(None, "--data-dir", "-d", help="Custom local data directory"),
    to_cloud: bool = typer.Option(False, "--to-cloud", help="Enable cloud S3 storage (Iceberg)"),
    to_local: bool = typer.Option(True, "--to-local/--no-to-local", help="Enable local JSONL/SQLite storage"),
    warehouse: str = typer.Option(None, "--warehouse", help="S3 bucket / warehouse path (e.g. s3://bucket/path)"),
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
            rprint(f"[yellow]Awareness API is already running in background (PID {pid}) on http://{host}:{port}[/yellow]")
        else:
            rprint(f"[yellow]Port {port} is already in use. Awareness API might be running under a different manager (e.g. launchd).[/yellow]")
        if tail:
            _trigger_tail_start(host, port)
        return

    if fg:
        rprint(f"[green]Starting Awareness API on http://{host}:{port} in foreground...[/green]")
        os.environ["AW_API_HOST"] = host
        os.environ["AW_API_PORT"] = str(port)
        if tail:
            import threading
            def trigger() -> None:
                import time
                time.sleep(2.0)
                _trigger_tail_start(host, port, silent=True)
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
            start_new_session=True
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
    port: int = typer.Option(_default_api_port, "--port", help="API port / launchd label port (default: AW_API_PORT or 8085)"),
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
    port: int = typer.Option(_default_api_port, "--port", help="API port for the LaunchAgent label (default: AW_API_PORT or 8085)"),
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
    port: int = typer.Option(_default_api_port, "--port", help="API port for the LaunchAgent label (default: AW_API_PORT or 8085)"),
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
        "EnvironmentVariables": {
            "PYTHONPATH": str(root / "src")
        },
        "ProgramArguments": [
            str(venv_python),
            "-m",
            "awareness.cli.main",
            "compact"
        ],
        "StartInterval": interval_seconds,
        "StandardOutPath": "/tmp/awareness-compact.launch.out",
        "StandardErrorPath": "/tmp/awareness-compact.launch.err"
    }

    try:
        plist_dir.mkdir(parents=True, exist_ok=True)
        with open(plist_path, "wb") as f:
            plistlib.dump(plist_data, f)
        rprint(f"[green]✔ Plist file created at: {plist_path}[/green]")
        # Unload if it is already loaded
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        subprocess.run(["launchctl", "load", str(plist_path)], check=True)
        rprint(f"[green]✔ Auto-compaction scheduled successfully via launchctl every {interval} minutes.[/green]")
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


def _query_db_metrics(state: StateDB) -> dict[str, Any]:
    from sqlalchemy import func, select
    from awareness.storage.state import JobRow, TaskRow, DedupRow, DedupNearRow, ManifestRow, DLQRow
    
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
        "dlq_count": 0
    }
    
    try:
        with state.session() as s:
            job_agg = s.execute(
                select(
                    func.count(JobRow.job_id),
                    func.sum(JobRow.bytes_processed),
                    func.sum(JobRow.docs_emitted),
                    func.sum(JobRow.docs_dedup_dropped)
                )
            ).first()
            if job_agg:
                stats_dict["jobs_count"] = job_agg[0] or 0
                stats_dict["total_bytes_processed"] = job_agg[1] or 0
                stats_dict["total_docs_emitted"] = job_agg[2] or 0
                stats_dict["total_docs_dedup_dropped"] = job_agg[3] or 0
            
            task_rows = s.execute(
                select(TaskRow.status, func.count(TaskRow.task_id))
                .group_by(TaskRow.status)
            ).all()
            stats_dict["tasks_count"] = sum(count for status, count in task_rows)
            stats_dict["tasks_by_status"] = {status: count for status, count in task_rows}
            
            stats_dict["dedup_content_count"] = s.scalar(select(func.count(DedupRow.content_hash))) or 0
            stats_dict["dedup_near_count"] = s.scalar(select(func.count(DedupNearRow.id))) or 0
            
            manifest_rows = s.execute(
                select(ManifestRow.compacted_at != None, func.count(ManifestRow.id))
                .group_by(ManifestRow.compacted_at != None)
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
    detailed: bool = typer.Option(False, "--detailed", "-d", help="Show detailed storage sizes and DB record counts")
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
    else:
        if _is_port_active("127.0.0.1", api_port):
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
    rprint(f"  • Total Raw Data Processed:     [green]{_format_size(db_metrics['total_bytes_processed'])}[/green] ({db_metrics['total_bytes_processed']:,} bytes)")
    rprint(f"  • Total Space Occupied on Disk:  [bold green]{_format_size(total_local_bytes)}[/bold green] ({total_local_bytes:,} bytes)")
    rprint(f"  • Total Unique Docs Ingested:    [bold]{db_metrics['total_docs_emitted']:,}[/bold]")
    if db_metrics['total_docs_emitted'] + db_metrics['total_docs_dedup_dropped'] > 0:
        total_docs = db_metrics['total_docs_emitted'] + db_metrics['total_docs_dedup_dropped']
        dedup_ratio = (db_metrics['total_docs_dedup_dropped'] / total_docs) * 100
        rprint(f"  • Ingestion Deduplication Ratio: [cyan]{dedup_ratio:.2f}%[/cyan] ({db_metrics['total_docs_dedup_dropped']:,} docs dropped)")

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
    detailed: bool = typer.Option(True, "--detailed/--summary", help="Show deep disk storage breakdowns")
) -> None:
    """Print detailed storage, database, and ingestion performance statistics."""
    state, _ = _bootstrap()
    settings = get_settings()
    
    # 1. Query State DB
    db_metrics = _query_db_metrics(state)
    
    # 2. Disk sizes (unless cloud)
    data_dir = settings.data_dir
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
        }
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
    rprint(f"  • Total Raw Data Processed (Uncompressed): [green]{_format_size(db_metrics['total_bytes_processed'])}[/green] ({db_metrics['total_bytes_processed']:,} bytes)")
    rprint(f"  • Total Unique Documents Emitted:        [bold green]{db_metrics['total_docs_emitted']:,}[/bold green]")
    rprint(f"  • Deduplication Dropped Documents:      [yellow]{db_metrics['total_docs_dedup_dropped']:,}[/yellow]")
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
    rprint(f"  • Total Manifests Tracked:               [cyan]{db_metrics['manifests_count'] + db_metrics['manifests_compacted_count']}[/cyan] ({db_metrics['manifests_compacted_count']} compacted)")
    rprint(f"  • Dead Letter Queue (DLQ) Rows:         [red]{db_metrics['dlq_count']}[/red]")
    rprint()
    
    # Disk Storage Summary
    rprint("[bold white]3. Local Disk Storage & Directory Sizes[/bold white]")
    rprint(f"  • Total Storage Directory:              [yellow]{settings.data_dir}[/yellow]")
    rprint(f"  • Total Local Files Managed:            [bold]{total_local_files:,}[/bold]")
    rprint(f"  • Total Space Occupied on Disk:          [bold green]{_format_size(total_local_bytes)}[/bold green]")
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
                    name.split(" (")[0], 
                    f"{data['files']:,}", 
                    _format_size(data["size"]), 
                    data["path"]
                )
        console.print(table)
        rprint()
        
        # Calculate compression/compaction efficiency
        if total_local_bytes > 0:
            compression_ratio = db_metrics['total_bytes_processed'] / total_local_bytes
            rprint(f"  [bold]Storage Efficiency Ratio[/bold] (Raw Size / Disk Size): [bold green]{compression_ratio:.2f}x[/bold green]")
            rprint("  [dim]*Higher is better. Values > 1.0 indicate efficient storage compression/deduplication.[/dim]\n")


@app.command()
def metrics() -> None:
    """Dump in-process metrics snapshot."""
    print(json.dumps(get_metrics().snapshot(), indent=2))


# ── backfill ────────────────────────────────────────────────────────────
@backfill_app.command("submit")
def backfill_submit(
    start: str = typer.Option(..., "--start", help="Start date (ISO or yyyy-mm-dd)"),
    end: str = typer.Option("now", "--end", help="End date (ISO, yyyy-mm-dd, or 'now')"),
    sources: list[str] = typer.Option(  # noqa: B008
        [],
        "--source",
        "-s",
        help="Restrict to specific source kinds. Repeat. Default: CC-WET, FineWeb, GDELT.",
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
    match_all: bool = typer.Option(False, "--match-all", help="Require ALL --match terms (AND) instead of ANY (OR)."),
    match_regex: bool = typer.Option(False, "--match-regex", help="Treat --match terms as Python regular expressions."),
    match_field: str = typer.Option("both", "--match-field", help="Where to match: title | text | both."),
) -> None:
    state, planner = _bootstrap()
    src = [SourceKind(s) for s in sources] if sources else []
    start_dt = to_utc(start)
    if start_dt is None:
        raise typer.BadParameter("Invalid start date format")
    if match_field not in ("title", "text", "both"):
        raise typer.BadParameter("--match-field must be one of: title, text, both")
    req = BackfillRequest(
        start=start_dt,
        end=coerce_relative_end(end),
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
        rprint(f"[dim]Topic filter ({'regex' if match_regex else 'keyword'}, {match_field}): {escape(joiner.join(match))}[/dim]")
    st = planner.status(job_id)
    if int(st.get("tasks_total") or 0) == 0 or st.get("warning") == "zero_tasks":
        rprint(
            "[bold yellow]WARNING: backfill planned 0 tasks — nothing will be scraped.[/bold yellow]"
        )
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
    from rich.panel import Panel
    from rich.table import Table
    from datetime import datetime, UTC
    import dateutil.parser
    
    status_val = status.get("status", "unknown").upper()
    style = "bold green" if status_val == "COMPLETED" else "bold yellow"
    
    rprint()
    rprint(Panel(
        f"[bold white]JOB ID: {job_id}[/bold white]\n"
        f"Status: [{style}]{status_val}[/{style}]",
        title="[bold cyan]AWARENESS BACKFILL INGESTION REPORT[/bold cyan]",
        expand=False
    ))
    
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
    silent_progress: bool = typer.Option(False, "--silent-progress", help="Mute per-document ingestion logs in the terminal"),
    mute_duplicates: bool = typer.Option(None, "--mute-duplicates/--no-mute-duplicates", help="Hide duplicate/revision documents in the terminal log"),
) -> None:
    """Run pending tasks for ``job_id`` to completion (in-process)."""
    state, planner = _bootstrap()
    engine = WorkerEngine(state, planner, concurrency=concurrency or None, silent_progress=silent_progress, mute_duplicates=mute_duplicates)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _stop(*_a: object) -> None:
        engine.request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    async def listen_for_stop(job_id: str):
        if not sys.stdin.isatty():
            return
        
        while not engine.is_stopping():
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            cmd = line.strip()
            if not cmd:
                rprint("[bold yellow]\nStopping backfill cleanly requested by keyboard input...[/bold yellow]")
                engine.request_stop()
                break
            if cmd.startswith("/"):
                parts = cmd[1:].split()
                if not parts:
                    continue
                action = parts[0].lower()
                if action == "clear":
                    print("\033[H\033[2J\033[3J", end="")
                    console.print(banner.render_banner())
                    rprint(f"[green]Backfill running[/green] job_id=[bold]{job_id}[/bold]")
                    rprint("[bold cyan]Type slash commands (e.g. /help, /clear, /status, /stop) or press ENTER to stop.[/bold cyan]\n")
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
                            rprint(f"  Bytes Processed: {_format_size(j.bytes_processed)} ({j.bytes_processed:,} bytes)\n")
                        else:
                            rprint("\n[yellow]Could not fetch job status from DB.[/yellow]\n")
                    except Exception as e:
                        rprint(f"\n[red]Error fetching status: {escape(str(e))}[/red]\n")
                elif action == "stop":
                    rprint("[bold yellow]\nStopping backfill cleanly requested by /stop command...[/bold yellow]")
                    engine.request_stop()
                    break
                else:
                    rprint(f"\n[red]Unknown command: /{action}. Type /help for a list of commands.[/red]\n")
            else:
                rprint(f"\n[yellow]Ignored raw input: '{cmd}'. Press ENTER on an empty line or type /stop to exit.[/yellow]\n")

    async def _drive() -> None:
        rprint(f"[green]Backfill started[/green] job_id=[bold]{job_id}[/bold]")
        rprint("[bold cyan]Type slash commands (e.g. /help, /clear, /status, /stop) or press ENTER to stop.[/bold cyan]\n")
        
        stop_task = asyncio.create_task(listen_for_stop(job_id))
        try:
            await engine.run_job(job_id)
        finally:
            stop_task.cancel()
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
    to_cloud: bool = typer.Option(None, "--to-cloud/--no-to-cloud", help="Write to the S3/Iceberg warehouse (default from `configure`)"),
    to_local: bool = typer.Option(None, "--to-local/--no-to-local", help="Write to local JSONL/SQLite (default from `configure`)"),
    to_gdrive: bool = typer.Option(None, "--to-gdrive/--no-to-gdrive", help="Upload captures to Google Drive (default from `configure`)"),
    warehouse: str = typer.Option(None, "--warehouse", help="S3 bucket / warehouse path (e.g. s3://bucket/path)"),
    interactive: bool = typer.Option(True, "--interactive/--no-interactive", help="Prompt for storage target choice interactively"),
    gdelt: bool = typer.Option(None, "--gdelt/--no-gdelt", help="Also follow the GDELT global-news firehose (default from config)."),
    gdelt_max_urls: int = typer.Option(0, "--gdelt-max-urls", help="Cap URLs pulled per 15-min GDELT slot (0=use config default)."),
    match: list[str] = typer.Option(  # noqa: B008
        [],
        "--match",
        "-m",
        help="Topic filter: keep only live docs with this whole word/phrase (case-insensitive). Repeat for OR; use --match-regex for partial/pattern matches.",
    ),
    match_all: bool = typer.Option(False, "--match-all", help="Require ALL --match terms (AND) instead of ANY (OR)."),
    match_regex: bool = typer.Option(False, "--match-regex", help="Treat --match terms as Python regular expressions."),
    match_field: str = typer.Option("both", "--match-field", help="Where to match: title | text | both."),
    mute_duplicates: bool = typer.Option(None, "--mute-duplicates/--no-mute-duplicates", help="Hide duplicate/revision documents in the terminal log"),
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
        rprint("[dim]Tip: set this once with [bold]awareness configure[/bold] to skip this prompt next time.[/dim]")
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

    async def listen_for_stop(job_id: str):
        if not sys.stdin.isatty():
            return
        
        while not shutdown.is_set():
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            cmd = line.strip()
            if not cmd:
                rprint("[bold yellow]\nStopping capture cleanly requested by keyboard input...[/bold yellow]")
                loop.call_soon_threadsafe(shutdown.set)
                break
            if cmd.startswith("/"):
                parts = cmd[1:].split()
                if not parts:
                    continue
                action = parts[0].lower()
                if action == "clear":
                    print("\033[H\033[2J\033[3J", end="")
                    console.print(banner.render_banner())
                    rprint(f"[green]Tail running[/green] job_id=[bold]{job_id}[/bold]")
                    rprint("[bold cyan]Type slash commands (e.g. /help, /clear, /status, /stop) or press ENTER to stop.[/bold cyan]\n")
                elif action == "help":
                    rprint("\n[bold cyan]Available Slash Commands:[/bold cyan]")
                    rprint("  [bold]/clear[/bold]  - Clear the terminal screen")
                    rprint("  [bold]/status[/bold] - Show the current tail job status and counters")
                    rprint("  [bold]/stop[/bold]   - Stop the tail stream cleanly")
                    rprint("  [bold]/help[/bold]   - Display this help message")
                    rprint("  [dim]Press ENTER (empty line) to stop tail engine.[/dim]\n")
                elif action == "status":
                    try:
                        j = state.get_job(job_id)
                        if j:
                            rprint(f"\n[bold cyan]Tail Job Status ({job_id}):[/bold cyan]")
                            rprint(f"  Status:          [green]{j.status.value}[/green]")
                            rprint(f"  Tasks Processed: {j.tasks_completed}/{j.tasks_total}")
                            rprint(f"  Docs Emitted:    {j.docs_emitted}")
                            rprint(f"  Near-Dup Dropped: {j.docs_dedup_dropped}")
                            rprint(f"  Bytes Processed: {_format_size(j.bytes_processed)} ({j.bytes_processed:,} bytes)\n")
                        else:
                            rprint("\n[yellow]Could not fetch job status from DB.[/yellow]\n")
                    except Exception as e:
                        rprint(f"\n[red]Error fetching status: {escape(str(e))}[/red]\n")
                elif action == "stop":
                    rprint("[bold yellow]\nStopping capture cleanly requested by /stop command...[/bold yellow]")
                    loop.call_soon_threadsafe(shutdown.set)
                    break
                else:
                    rprint(f"\n[red]Unknown command: /{action}. Type /help for a list of commands.[/red]\n")
            else:
                rprint(f"\n[yellow]Ignored raw input: '{cmd}'. Press ENTER on an empty line or type /stop to exit.[/yellow]\n")

    async def _drive() -> None:
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
            rprint(f"[dim]Topic filter ({'regex' if match_regex else 'keyword'}, {match_field}): {escape(joiner.join(match))}[/dim]")
        rprint("[bold cyan]Type slash commands (e.g. /help, /clear, /status, /stop) or press ENTER to stop.[/bold cyan]\n")
        
        stop_task = asyncio.create_task(listen_for_stop(job_id_res))
        try:
            if duration > 0:
                try:
                    await asyncio.wait_for(shutdown.wait(), timeout=duration)
                except TimeoutError:
                    pass
            else:
                await shutdown.wait()
        finally:
            stop_task.cancel()
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
def tail_check_seeds(
    seeds: Path = typer.Option(None, "--seeds", help="Path to tail_seeds.yaml")
) -> None:
    """Validate feeds and sitemaps in tail_seeds.yaml for connectivity, robots.txt, and parseability."""
    import yaml
    import httpx
    import anyio
    from awareness.util.robots import RobotsCache
    
    if not seeds:
        settings = get_settings()
        seeds = settings.tail_seed_file
        
    if not seeds or not seeds.exists():
        rprint(f"[red]Seeds file not found at: {seeds}[/red]")
        return
        
    try:
        with open(seeds, "r", encoding="utf-8") as fh:
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
                    status_str = f"[green]{r.status_code}[/green]" if r.status_code == 200 else f"[yellow]{r.status_code}[/yellow]"
                    
                    if kind in ("RSS/Feed", "Atom"):
                        import feedparser
                        parsed = feedparser.parse(r.text)
                        if parsed.bozo:
                            parser_str = f"[yellow]Bozo Feed (Format warnings)[/yellow]"
                        else:
                            parser_str = f"[green]Parsed ({len(parsed.entries)} entries)[/green]"
                    else:
                        if "<sitemap" in r.text or "<url" in r.text:
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
    end_dt = inclusive_end(coerce_relative_end(end))
    where = ["fetch_ts >= $start", "fetch_ts <= $end"]
    params: dict[str, Any] = {"start": start_dt, "end": end_dt}
    if domain:
        where.append("domain = $dom")
        params["dom"] = domain
    if source:
        where.append("source_type = $src")
        params["src"] = source
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
    """Aggregate counts by source and domain in [start, end]."""
    state, _ = _bootstrap()
    settings = get_settings()
    idx = DuckDbIndex(
        db_path=settings.duckdb_path(),
        jsonl_dir=settings.staging_jsonl_dir(),
        iceberg_warehouse=settings.iceberg_warehouse,
    )
    start_dt = to_utc(start)
    end_dt = inclusive_end(coerce_relative_end(end))
    try:
        by_source = idx.execute(
            """
            SELECT source_type, COUNT(*) AS n
            FROM captures
            WHERE fetch_ts BETWEEN $start AND $end
            GROUP BY source_type
            ORDER BY n DESC
            """,
            {"start": start_dt, "end": end_dt},
        )
        by_domain = idx.execute(
            """
            SELECT domain, COUNT(*) AS n
            FROM captures
            WHERE fetch_ts BETWEEN $start AND $end AND domain IS NOT NULL
            GROUP BY domain
            ORDER BY n DESC LIMIT 25
            """,
            {"start": start_dt, "end": end_dt},
        )
        total = idx.execute(
            "SELECT COUNT(*) AS n FROM captures WHERE fetch_ts BETWEEN $start AND $end",
            {"start": start_dt, "end": end_dt},
        )
        print(json.dumps({"total": total, "by_source": by_source, "by_domain": by_domain}, indent=2, default=str))
    except Exception as exc:
        rprint(f"[red]Query failed:[/red] {escape(str(exc))}")


@app.command()
def clear() -> None:
    """Clear the terminal screen."""
    print("\033[H\033[2J\033[3J", end="")


def _make_tui_layout(state: StateDB, settings: Any, idx: DuckDbIndex, selected_job_idx: int = 0) -> Any:
    from rich.layout import Layout
    from rich.panel import Panel
    from rich.text import Text
    from rich.table import Table
    from datetime import datetime
    
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=4),
        Layout(name="body"),
        Layout(name="footer", size=3)
    )
    layout["body"].split_row(
        Layout(name="left", ratio=1),
        Layout(name="right", ratio=2)
    )
    layout["right"].split_column(
        Layout(name="right_top", ratio=1),
        Layout(name="right_middle", ratio=1),
        Layout(name="right_bottom", ratio=1)
    )
    
    # 1. Header
    time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header_text = Text.assemble(
        (" AWARENESS ENGINE TUI DASHBOARD ", "bold reverse cyan"),
        "  |  Local Time: ", (time_str, "yellow"),
        "  |  Controls: ", ("[Q] Quit  [C] Compact  [T] Toggle Tail  [A] Toggle API  [R] Refresh  [L] Logs  [S] Cancel  [D] Delete  [N] New", "bold green")
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
    left_text.append(_format_size(db_metrics['total_bytes_processed']), style="green")
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
    left_text.append(f"  • DLQ Failures:    ")
    left_text.append(str(db_metrics['dlq_count']), style="red" if db_metrics['dlq_count'] > 0 else "white")
    left_text.append("\n")
    
    layout["left"].update(Panel(left_text, title="[bold white]Telemetry & State[/bold white]", border_style="blue"))
    
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
        status_color = "green" if j.status.value == "completed" else "yellow" if j.status.value == "running" else "red"
        is_selected = (idx_job == selected_job_idx)
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
    layout["right_top"].update(Panel(jobs_table, title="[bold white]Recent Jobs[/bold white]", border_style="magenta"))
    
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
            m = re.search(r'(\d{2}):(\d{2}):(\d{2})', s)
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
        
    layout["right_middle"].update(Panel(captures_table, title="[bold white]Recent Captures[/bold white]", border_style="cyan"))

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
            
    layout["right_bottom"].update(Panel(storage_table, title="[bold white]Disk Storage Breakdown[/bold white]", border_style="green"))
    
    # 5. Footer
    footer_text = Text(f"Data Root: {settings.data_dir}  |  Total Local Files: {total_local_files:,}  |  Disk Space: {_format_size(total_local_bytes)}", justify="center", style="dim cyan")
    layout["footer"].update(Panel(footer_text, border_style="cyan"))
    
    return layout



def _get_key_nonblocking() -> str | None:
    import sys
    import select
    if not sys.stdin.isatty():
        return None
    try:
        import tty
        import termios
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
    from rich.layout import Layout
    from rich.panel import Panel
    from rich.text import Text
    from datetime import datetime
    import os

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=4),
        Layout(name="body"),
        Layout(name="footer", size=3)
    )
    
    # 1. Header
    time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_name = "API LOGS (api.log)" if log_type == "api" else "APPLICATION LOGS (awareness.log)"
    header_text = Text.assemble(
        (" AWARENESS ENGINE LOG VIEWER ", "bold reverse yellow"),
        "  |  Local Time: ", (time_str, "yellow"),
        "  |  Controls: ", ("[Q] Quit  [L] Dashboard  [TAB/S] Toggle Log  [Up/Down/J/K/U/D] Scroll  [G] Reset", "bold green")
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
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
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
        style="dim yellow"
    )
    layout["footer"].update(Panel(footer_text, border_style="yellow"))
    
    return layout, total_lines, visible_height


@app.command(name="tui")
def tui(
    refresh_rate: float = typer.Option(2.0, "--refresh", "-r", help="Refresh rate in seconds")
) -> None:
    """Launch the interactive Terminal User Interface (TUI) dashboard."""
    state, planner = _bootstrap()
    settings = get_settings()
    idx = DuckDbIndex(
        db_path=settings.duckdb_path(),
        jsonl_dir=settings.staging_jsonl_dir(),
        iceberg_warehouse=settings.iceberg_warehouse,
    )
    selected_job_idx = 0
    
    from rich.live import Live
    from rich.text import Text
    from rich.panel import Panel
    import time
    import subprocess
    import sys
    import os
    import signal
    
    # Clear screen before starting
    print("\033[H\033[2J\033[3J", end="")
    
    status_msg = ""
    last_update = 0.0
    
    current_view = "dashboard"
    log_scroll_offset = 0
    max_scroll = 0
    visible_log_height = 10
    
    def compact_action() -> str:
        pending = state.list_pending_manifests()
        if not pending:
            return "[green]No staging files pending compaction.[/green]"
        from awareness.storage.iceberg import IcebergWriter
        try:
            writer = IcebergWriter(catalog_db=settings.iceberg_catalog_db, warehouse=settings.iceberg_warehouse)
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
                subprocess.Popen([sys.executable, "-c", "from awareness.tail.daemon import run; run()"], start_new_session=True)
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
                        start_new_session=True
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
                            (f"Data Root: {settings.data_dir}", "dim cyan")
                        )
                        layout["footer"].update(Panel(footer_content, border_style="cyan"))
                        live.update(layout)
                        status_msg = compact_action()
                        last_update = 0.0 # Force immediate refresh
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
                        else:
                            current_view = "dashboard"
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
                        log_scroll_offset = min(max_scroll, log_scroll_offset + max(1, visible_log_height - 2))
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
                            from awareness.schemas.jobs import JobStatus, JobKind
                            if sel_job.status not in (JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED):
                                live.stop()
                                print("\033[H\033[2J\033[3J", end="")
                                rprint(f"[bold red]Cancel Job[/bold red]")
                                confirm = typer.confirm(f"Are you sure you want to cancel job {sel_job.job_id}?")
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
                                rprint(f"[bold red]Delete Job[/bold red]")
                                confirm = typer.confirm(f"Are you sure you want to delete job {sel_job.job_id}?")
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
                            job_type = typer.prompt("Job Type (backfill or tail)", default="backfill").strip().lower()
                        if job_type == "backfill":
                            start_str = typer.prompt("Start date (ISO or relative, e.g. '1 day ago', '2026-06-05')").strip()
                            end_str = typer.prompt("End date (ISO, relative, or 'now')", default="now").strip()
                            sources_str = typer.prompt("Sources (comma-separated, e.g. 'CC-WET,FineWeb,GDELT')", default="CC-WET,FineWeb,GDELT").strip()
                            domains_str = typer.prompt("Domain filters (comma-separated, optional)", default="").strip()
                            match_str = typer.prompt("Match keywords (comma-separated, optional)", default="").strip()
                            
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
                            req = BackfillRequest(
                                start=start_dt,
                                end=coerce_relative_end(end_str),
                                sources=src_kinds,
                                domains=domains_list,
                                match=matches,
                                match_all=False,
                                match_regex=False,
                                match_field="both",
                            )
                            job_id = planner.submit_backfill(req)
                            subprocess.Popen([
                                sys.executable, "-m", "awareness.cli.main", "backfill", "run", job_id, "--silent-progress"
                            ], start_new_session=True)
                            status_msg = f"[green]Launched backfill job: {job_id}[/green]"
                        else: # tail
                            duration_str = typer.prompt("Duration in seconds (0 for infinite)", default="0").strip()
                            sources_str = typer.prompt("Sources (comma-separated, e.g. 'RSS,GDELT')", default="RSS,GDELT").strip()
                            match_str = typer.prompt("Match keywords (comma-separated, optional)", default="").strip()
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
                                sys.executable, "-m", "awareness.cli.main", "tail", "start",
                                "--no-interactive",
                                "--duration", str(duration),
                                "--job-id", job_id,
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
                
                # Check refresh interval
                now = time.time()
                if now - last_update >= refresh_rate:
                    if current_view == "dashboard":
                        layout = _make_tui_layout(state, settings, idx, selected_job_idx)
                        if status_msg:
                            footer_content = Text.assemble(
                                (status_msg, "bold yellow"),
                                "  |  ",
                                (f"Data Root: {settings.data_dir}", "dim cyan")
                            )
                            layout["footer"].update(Panel(footer_content, border_style="cyan"))
                    else:
                        log_file_type = "api" if current_view == "api_logs" else "app"
                        layout, total_log_lines, visible_log_height = _make_tui_log_layout(settings, log_file_type, log_scroll_offset)
                        max_scroll = max(0, total_log_lines - visible_log_height)
                    live.update(layout)
                    last_update = now
                    
                time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
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
            if c == '&':
                amp_pos = i
                break
            if not (c.isalnum() or c == '#'):
                break
                
        if amp_pos != -1:
            # Search forwards for ';'
            semi_pos = -1
            for i in range(end, len(escaped_text)):
                c = escaped_text[i]
                if c == ';':
                    semi_pos = i
                    break
                if not (c.isalnum() or c == '#'):
                    break
            if semi_pos != -1:
                return match_str
                
        return f"[bold yellow]{match_str}[/bold yellow]"
        
    return pattern.sub(replace, escaped_text)



def highlight_tokens(text: str, query: str) -> str:
    return highlight_query(text, query)


@app.command(name="browse")
def browse(
    start: str = typer.Option("", "--start", help="Start date range (empty = all time; e.g. '30 days ago', '2026-01-01')"),
    end: str = typer.Option("now", "--end", help="End date range"),
    domain: str = typer.Option("", "--domain", help="Filter by domain"),
    source: str = typer.Option("", "--source", help="Filter by source"),
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

    # Empty start means no lower bound so historical backfills remain visible.
    start_dt = to_utc(start) if (start or "").strip() else None
    end_dt = inclusive_end(coerce_relative_end(end))
    
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
            where.append("domain = $dom")
            params["dom"] = domain
        if source:
            where.append("source_type = $src")
            params["src"] = source
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
        browse_select = "doc_id, domain, title, fetch_ts, source_type, text"
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
                if start_dt is not None or end_dt is not None:
                    range_hint = (
                        f" (filters: start={start_dt or '−∞'}, end={end_dt}; "
                        "try widening --start/--end)"
                    )
                rprint(f"[yellow]No captures found in this range.{range_hint}[/yellow]")
                break
            else:
                rprint("[yellow]No more pages. Going back...[/yellow]")
                offset = max(0, offset - limit)
                continue
                
        # Display table (surface active unique fold so operators see the mode)
        unique_label = f" unique={unique_mode}" if unique_mode != "none" else ""
        table = Table(
            title=(
                f"Awareness Documents - Page {offset // limit + 1} "
                f"(Offset: {offset}{unique_label})"
            )
        )
        table.add_column("#", justify="center", style="yellow")
        table.add_column("Domain", style="cyan")
        table.add_column("Title", style="white")
        table.add_column("Date Captured", style="dim green")
        table.add_column("Source", style="magenta")
        
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
                r["source_type"] or "N/A"
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
                rprint(f"[bold reverse cyan] DOCUMENT READ VIEW [/bold reverse cyan]\n")
                highlighted_title = highlight_tokens(doc['title'] or "No Title", query)
                rprint(f"[bold cyan]Title:[/bold cyan]       {highlighted_title}")
                rprint(f"[bold cyan]Domain:[/bold cyan]      {doc['domain']}")
                rprint(f"[bold cyan]Captured at:[/bold cyan] {doc['fetch_ts']}")
                rprint(f"[bold cyan]Source:[/bold cyan]      {doc['source_type']}")
                rprint(f"[bold cyan]Doc ID:[/bold cyan]      {doc['doc_id']}\n")
                rprint("-" * 80)
                
                # Display body text with word wrapping
                highlighted_body = highlight_tokens(doc['text'] or "[Empty Document]", query)
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
    end_dt = inclusive_end(coerce_relative_end(end))
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
    mode: str = typer.Option("", "--mode", "-m", help="Match mode: auto | fts | prefix | substring (default from config)"),
    fields: str = typer.Option("", "--fields", "-f", help="Comma-list of columns to match: title,text,domain,url (default from config)"),
    limit: int = typer.Option(0, "--limit", "-l", help="Results per page (0 = config default)"),
    max_results: int = typer.Option(0, "--max-results", help="Hard ceiling on rows returned (0 = config default; overload guard)"),
    interactive: bool = typer.Option(True, "--interactive/--no-interactive", help="Enable interactive browsing of search results"),
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
            rprint(f"  [dim]Domain: {r['domain'] or 'N/A'} | Captured: {r['fetch_ts']} | Source: {r['source_type'] or 'N/A'}[/dim]")
            if r.get("snippet"):
                highlighted_snippet = highlight_tokens(r["snippet"], query)
                rprint(f"  [italic]\"{highlighted_snippet}\"[/italic]")
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

        table = Table(title=f"Search Results for '{query}' - Page {offset // limit + 1} (Found {total} total, Mode: {used_mode}, Ranked: {ranked})")
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
                
            table.add_row(
                str(i),
                score_val,
                r["domain"] or "N/A",
                title_and_snippet,
                str(r["fetch_ts"])[:16]
            )
            
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
                full_doc_rows = idx.execute("SELECT doc_id, title, domain, fetch_ts, source_type, text FROM captures WHERE doc_id = $id LIMIT 1", {"id": doc_id})
                if full_doc_rows:
                    doc = full_doc_rows[0]
                    print("\033[H\033[2J\033[3J", end="")
                    rprint(f"[bold reverse cyan] DOCUMENT READ VIEW [/bold reverse cyan]\n")
                    highlighted_title = highlight_tokens(doc['title'] or "No Title", query)
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
    force: bool = typer.Option(False, "--force", help="Force compaction even if Iceberg is disabled in config")
) -> None:
    """Compact local JSONL staging files into the durable Iceberg warehouse."""
    state, _ = _bootstrap()
    settings = get_settings()
    
    if not settings.enable_iceberg and not force:
        rprint("[yellow]Iceberg storage is disabled in configuration. Use --force to override.[/yellow]")
        return
        
    pending = state.list_pending_manifests()
    if not pending:
        rprint("[green]No staging files pending compaction.[/green]")
        return
        
    rprint(f"[bold cyan]Found {len(pending)} manifest files pending compaction.[/bold cyan]\n")
    
    from awareness.storage.iceberg import IcebergWriter
    assert settings.iceberg_catalog_db is not None
    assert settings.iceberg_warehouse is not None
    
    writer = IcebergWriter(catalog_db=settings.iceberg_catalog_db, warehouse=settings.iceberg_warehouse)
    writer.ensure_table()
    
    compacted_count = 0
    total_records = 0
    total_bytes = 0
    
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
            continue
            
        rprint(f"Compacting [cyan]{p.name}[/cyan] ({_format_size(item['bytes'])}, {item['records']} records)...")
        
        # Read JSONL
        rows = []
        try:
            import gzip
            open_func = gzip.open if str(p).endswith(".gz") else open
            with open_func(p, "rt", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        rows.append(json.loads(line))
        except Exception as e:
            rprint(f"[red]Failed to read JSONL file {p}: {e}[/red]")
            continue
            
        if rows:
            try:
                writer.append(rows)
                state.mark_manifest_compacted(manifest_id)
                compacted_count += 1
                total_records += len(rows)
                total_bytes += item["bytes"]
            except Exception as e:
                rprint(f"[red]Failed to append manifest {manifest_id} to Iceberg: {e}[/red]")
                
    rprint(f"\n[green]✔ Compaction completed successfully![/green]")
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
                f"[green]✔ Successfully exported {n} documents to JSONL file: "
                f"[bold]{output}[/bold][/green]"
            )
        except Exception as e:
            rprint(f"[red]Export failed: {e}[/red]")
    elif format_type.lower() == "txt":
        try:
            output.mkdir(parents=True, exist_ok=True)
            written = 0
            for r in rows:
                doc_id = r["doc_id"]
                safe_title = re.sub(r"[^0-9a-zA-Z\-_]", "", r["title"] or "")[:40]
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
    repo_id: str = typer.Argument(..., help="Hugging Face Dataset Repository ID (e.g. 'username/my-dataset')"),
    token: str = typer.Option(None, "--token", "-t", help="HF Write Token (or set HF_TOKEN environment variable)"),
    private: bool = typer.Option(True, "--private/--public", help="Make the repository private or public"),
    domain: str = typer.Option("", "--domain", help="Filter documents by domain"),
    source: str = typer.Option("", "--source", help="Filter documents by source type"),
) -> None:
    """Push captured documents directly to the Hugging Face Dataset Hub."""
    state, _ = _bootstrap()
    settings = get_settings()
    
    try:
        from datasets import Dataset  # noqa: PLC0415
    except ImportError:
        rprint("[red]Hugging Face dependencies missing.[/red]")
        rprint("Please install them using: [bold]pip install \"awareness[hf]\"[/bold] or [bold]uv pip install datasets huggingface-hub[/bold]")
        return

    idx = DuckDbIndex(
        db_path=settings.duckdb_path(),
        jsonl_dir=settings.staging_jsonl_dir(),
        iceberg_warehouse=settings.iceberg_warehouse,
    )
    
    where = []
    params = {}
    if domain:
        where.append("domain = $dom")
        params["dom"] = domain
    if source:
        where.append("source_type = $src")
        params["src"] = source
        
    where_sql = " WHERE " + " AND ".join(where) if where else ""
    sql = f"""
        SELECT doc_id, capture_id, source_type, source_name, canonical_url, fetch_ts, domain, title, text, language
        FROM captures
        {where_sql}
        ORDER BY fetch_ts DESC
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
        rprint(f"[bold green]✔ Successfully pushed dataset to: https://huggingface.co/datasets/{repo_id}[/bold green]")
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
    from awareness.util.hashing import content_hash, simhash128, hamming128
    from awareness.storage.state import DedupRow

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
                rprint(f"[red]✖ EXACT DUPLICATE DETECTED![/red]")
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
            res = idx.execute("SELECT doc_id, fetch_ts, title FROM captures WHERE canonical_url = $url OR source_name = $url", {"url": url})
            if res:
                rprint(f"[red]✖ URL HAS ALREADY BEEN CAPTURED![/red]")
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
        lines.append("\n  [yellow]TERMINAL-ONLY[/yellow] — captures are displayed but [bold]not saved anywhere[/bold].")
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
    show_all: bool = typer.Option(False, "--all", "-a", help="Include every raw Settings field, not just documented knobs."),
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
    rprint(f"\n[bold {banner.C_HI}]Awareness configuration[/]  [dim](file: {_get_yaml_config_path()})[/dim]\n")

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
        print(json.dumps({
            "key": norm,
            "value": _jsonable(value),
            "source": src,
            "type": fld.type_label if fld else None,
            "default": _jsonable(fld.default) if fld else None,
            "env_var": ("AW_" + norm.upper()),
            "description": fld.description if fld else None,
        }, indent=2, default=str))
        return

    rprint(f"[bold {banner.C_HI}]{norm}[/] = [bold {banner.C_FG}]{escape(str(value))}[/]  ({_SOURCE_CHIP.get(src, src)})")
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
        rprint(f"[green]✔ Removed [bold]{norm}[/bold] — now using default:[/green] [bold {banner.C_FG}]{escape(str(new_val))}[/]")
    else:
        rprint(f"[yellow]'{escape(norm)}' was not set in {_get_yaml_config_path().name}; nothing to remove.[/yellow]")


@config_app.command("reset")
def config_reset(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Reset ALL configuration to defaults (clears awareness.yaml)."""
    path = _get_yaml_config_path()
    if not path.exists() or not _read_yaml_data():
        rprint("[yellow]Configuration is already at defaults (no overrides set).[/yellow]")
        return
    if not yes and not typer.confirm(f"Clear all overrides in {path.name} and revert to defaults?", default=False):
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
        path.write_text("# Awareness configuration overrides. See `awareness config show`.\n", encoding="utf-8")
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
    rprint(f"[green]✔ Configuration is valid[/green] ({len(data)} override(s)" + (f", {len(warnings)} warning(s)" if warnings else "") + ").")


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
        rprint(f"  {'[green]✔[/green]' if writable else '[red]✖[/red]'} Local JSONL → {target}"
               + ("" if writable else "  [red](not writable)[/red]"))
        ok = ok and writable
    else:
        rprint("  [dim]○ Local JSONL disabled[/dim]")

    # S3 / Iceberg
    if settings.enable_iceberg:
        wh = str(settings.iceberg_warehouse)
        if _is_cloud_path(wh):
            rprint(f"  [yellow]●[/yellow] Cloud warehouse → {wh}  [dim](cannot verify credentials offline)[/dim]")
            if not os.environ.get("AWS_ACCESS_KEY_ID"):
                rprint("      [yellow]⚠ AWS_ACCESS_KEY_ID is not set — S3 writes may fail.[/yellow]")
        else:
            writable = _is_writable_dir(Path(wh))
            rprint(f"  {'[green]✔[/green]' if writable else '[red]✖[/red]'} Local warehouse → {wh}"
                   + ("" if writable else "  [red](not writable)[/red]"))
            ok = ok and writable
    else:
        rprint("  [dim]○ Iceberg warehouse disabled[/dim]")

    # Google Drive
    if settings.enable_gdrive:
        if _gdrive_authorized():
            rprint(f"  [green]✔[/green] Google Drive authorized → folder “{settings.gdrive_folder_name}”")
        else:
            rprint("  [red]✖[/red] Google Drive enabled but NOT authorized — run [bold]awareness cloud auth-gdrive[/bold]")
            ok = False
    else:
        rprint("  [dim]○ Google Drive disabled[/dim]")

    if plan.terminal_only:
        rprint("\n  [yellow]All destinations are off — captures will be shown but NOT saved.[/yellow]")
    rprint(f"\n[{'green' if ok else 'yellow'}]{'✔ All enabled destinations look healthy.' if ok else '⚠ Some destinations need attention (see above).'}[/]")
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
            rprint(f"  [{i:>2}] [bold]{fld.key}[/bold] = {escape(str(value))}  [dim]({src}) — {fld.description}[/dim]")
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
    "enable_jsonl_staging", "enable_iceberg", "enable_gdrive",
    "data_dir", "iceberg_warehouse", "gdrive_folder_name", "jsonl_compress",
    "tail_poll_seconds", "tail_gdelt", "tail_gdelt_max_urls", "tail_show_captures",
    "terminal_mute_duplicates",
    "storage_flush_records", "storage_flush_seconds",
)


@app.command()
def configure(
    local: bool = typer.Option(None, "--local/--no-local", help="Write captures to local JSONL + index."),
    s3: bool = typer.Option(None, "--s3/--no-s3", help="Write captures to the S3 / Iceberg warehouse."),
    gdrive: bool = typer.Option(None, "--gdrive/--no-gdrive", help="Upload captures to Google Drive."),
    terminal_only: bool = typer.Option(False, "--terminal-only", help="Disable ALL sinks (display captures only); overrides --local/--s3/--gdrive if combined."),
    data_dir: Path = typer.Option(None, "--data-dir", help="Local data root directory."),
    warehouse: str = typer.Option(None, "--warehouse", help="Iceberg warehouse (local path or s3://bucket/path)."),
    gdrive_folder: str = typer.Option(None, "--gdrive-folder", help="Google Drive target folder name."),
    compress: bool = typer.Option(None, "--compress/--no-compress", help="Gzip local JSONL staging files."),
    flush_records: int = typer.Option(None, "--flush-records", help="Flush the write buffer after N records."),
    flush_seconds: float = typer.Option(None, "--flush-seconds", help="Flush the write buffer at least every N seconds."),
    poll_seconds: float = typer.Option(None, "--poll-seconds", help="Tail seed re-arm interval in seconds."),
    gdelt: bool = typer.Option(None, "--gdelt/--no-gdelt", help="Follow the GDELT firehose while tailing."),
    gdelt_max_urls: int = typer.Option(None, "--gdelt-max-urls", help="Cap URLs pulled per 15-min GDELT slot."),
    mute_duplicates: bool = typer.Option(None, "--mute-duplicates/--no-mute-duplicates", help="Mute duplicate captures in terminal logging."),
    show: bool = typer.Option(False, "--show", help="Print the current write-destination plan and exit."),
    reset: bool = typer.Option(False, "--reset", help="Reset destination/tail settings to defaults."),
    non_interactive: bool = typer.Option(False, "--non-interactive", help="Never prompt; apply flags (or do nothing)."),
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
        rprint("[dim]Change with [bold]awareness configure[/bold] (wizard) or flags like [bold]--local/--no-s3[/bold].[/dim]")
        return

    if reset:
        if not yes and sys.stdin.isatty() and not typer.confirm("Reset destination & tail settings to defaults?", default=False):
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
        for v in (local, s3, gdrive, data_dir, warehouse, gdrive_folder, compress,
                  flush_records, flush_seconds, poll_seconds, gdelt, gdelt_max_urls, mute_duplicates)
    )

    if not decisive and not non_interactive:
        values = _configure_wizard(settings)
        if values is None:
            rprint("[dim]Aborted — no changes made.[/dim]")
            return
    elif not decisive:  # --non-interactive with no flags → show + hint, change nothing
        _render_destination_plan(_destination_plan(settings))
        rprint("[dim]No flags given. Pass e.g. [bold]--local --no-s3[/bold], or run without --non-interactive for the wizard.[/dim]")
        return
    else:
        values, errors = _configure_from_flags(
            settings, local=local, s3=s3, gdrive=gdrive, terminal_only=terminal_only,
            data_dir=data_dir, warehouse=warehouse, gdrive_folder=gdrive_folder, compress=compress,
            flush_records=flush_records, flush_seconds=flush_seconds, poll_seconds=poll_seconds,
            gdelt=gdelt, gdelt_max_urls=gdelt_max_urls, mute_duplicates=mute_duplicates,
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
    rprint(f"\n[dim]Next:[/dim] [bold {banner.C_HI}]awareness tail start[/]  [dim](it will write to the destinations above)[/dim]")


def _configure_from_flags(
    settings: Any, *, local: bool | None, s3: bool | None, gdrive: bool | None,
    terminal_only: bool, data_dir: Path | None, warehouse: str | None,
    gdrive_folder: str | None, compress: bool | None, flush_records: int | None,
    flush_seconds: float | None, poll_seconds: float | None, gdelt: bool | None,
    gdelt_max_urls: int | None, mute_duplicates: bool | None = None,
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
    if values.get("enable_iceberg") and not _is_cloud_path(values.get("iceberg_warehouse", settings.iceberg_warehouse)):
        rprint("[yellow]⚠ S3 enabled but warehouse is a local path. Pass --warehouse s3://bucket/path for true cloud storage.[/yellow]")
    return values, errors


def _configure_wizard(settings: Any) -> dict[str, Any] | None:
    """Interactive walk-through. Returns the values mapping, or None if aborted."""
    rprint(f"[bold {banner.C_HI}]Awareness — capture destination setup[/]")
    rprint("[dim]Choose where live & historical captures are written. Enter accepts the [bold]default[/bold] in brackets.[/dim]\n")
    values: dict[str, Any] = {}

    try:
        # 1 ── Local JSONL + index
        rprint(f"[bold {banner.C_FG}]1) Local storage[/] [dim](JSONL files + the SQLite/DuckDB search index)[/dim]")
        local = typer.confirm("   Write captures locally?", default=bool(settings.enable_jsonl_staging))
        values["enable_jsonl_staging"] = local
        if local:
            dd = typer.prompt("   Local data directory", default=str(settings.data_dir))
            values["data_dir"] = str(Path(dd).expanduser().resolve())
            values["jsonl_compress"] = typer.confirm("   Gzip JSONL files (.jsonl.gz)?", default=bool(settings.jsonl_compress))

        # 2 ── Cloud S3 / Iceberg
        rprint(f"\n[bold {banner.C_FG}]2) Cloud warehouse (S3 / Iceberg)[/]")
        s3 = typer.confirm("   Write captures to the Iceberg warehouse?", default=bool(settings.enable_iceberg))
        values["enable_iceberg"] = s3
        if s3:
            wh = typer.prompt("   Warehouse (local path or s3://bucket/path)", default=str(settings.iceberg_warehouse))
            values["iceberg_warehouse"] = wh.strip()
            if not _is_cloud_path(wh):
                rprint("   [yellow]⚠ That is a local path. Use s3://… for real cloud storage.[/yellow]")

        # 3 ── Google Drive
        rprint(f"\n[bold {banner.C_FG}]3) Google Drive[/]")
        if not _gdrive_authorized():
            rprint("   [dim]Not authorized yet — run [bold]awareness cloud auth-gdrive[/bold] to connect an account.[/dim]")
        gd = typer.confirm("   Upload captures to Google Drive?", default=bool(settings.enable_gdrive))
        values["enable_gdrive"] = gd
        if gd:
            folder = typer.prompt("   Drive folder name", default=settings.gdrive_folder_name)
            values["gdrive_folder_name"] = folder.strip() or settings.gdrive_folder_name
            if not _gdrive_authorized():
                rprint("   [yellow]⚠ Remember to authorize before tailing, or uploads will be skipped.[/yellow]")

        # 4 ── Tail capture knobs
        rprint(f"\n[bold {banner.C_FG}]4) Live tail behaviour[/]")
        values["tail_poll_seconds"] = _wizard_number(
            "   Re-check feeds/sitemaps every N seconds", settings.tail_poll_seconds, "tail_poll_seconds")
        gdelt = typer.confirm("   Follow the GDELT global-news firehose?", default=bool(settings.tail_gdelt))
        values["tail_gdelt"] = gdelt
        if gdelt:
            values["tail_gdelt_max_urls"] = int(_wizard_number(
                "   Max URLs per 15-min GDELT slot", settings.tail_gdelt_max_urls, "tail_gdelt_max_urls"))
        values["tail_show_captures"] = typer.confirm(
            "   Print each capture in the terminal as it lands?", default=bool(settings.tail_show_captures))
    except (EOFError, KeyboardInterrupt):
        return None

    # 5 ── Summary + confirm
    rprint(f"\n[bold {banner.C_HI}]Review[/]")
    preview = cfg_schema.describe_destinations(
        local=values.get("enable_jsonl_staging", settings.enable_jsonl_staging),
        s3=values.get("enable_iceberg", settings.enable_iceberg),
        gdrive=values.get("enable_gdrive", settings.enable_gdrive),
        data_dir=values.get("data_dir", str(settings.data_dir) if settings.data_dir else None),
        warehouse=values.get("iceberg_warehouse", str(settings.iceberg_warehouse) if settings.iceberg_warehouse else None),
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
        from http.server import HTTPServer, BaseHTTPRequestHandler
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
                    self.wfile.write(b"<h1>Authorization successful!</h1><p>You can close this tab and return to the terminal.</p>")
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
            rprint("[green]✔ Enabled Google Drive uploads[/green] [dim](enable_gdrive=true; disable with `awareness config set enable_gdrive false`)[/dim]")
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
    rprint(f"  Authorized:                  {'[bold green]YES[/bold green]' if gdrive_authorized else '[yellow]NO[/yellow]'}")
    rprint(f"  Upload destination enabled:  {'[bold green]YES[/bold green]' if settings.enable_gdrive else '[yellow]NO[/yellow]'}  [dim](enable_gdrive)[/dim]")
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

    def completer(text: str, state: int) -> str | None:
        try:
            import shlex
            import click
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
            elif len(normalized_words) >= 2 and normalized_words[-2] == "config" and normalized_words[-1] in ("get", "set", "unset"):
                pool = [fld.key for fld in cfg_schema.CONFIG_SCHEMA]
            # Check for config key value autocomplete
            elif len(normalized_words) >= 3 and normalized_words[-3] == "config" and normalized_words[-2] == "set":
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
                    if current_group and hasattr(current_group, "commands") and word in current_group.commands:
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
                    else:
                        if hasattr(current_group, "commands"):
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


def _shell_dispatch(click_cmd: Any, argv: list[str]) -> None:
    """Run one parsed command line through the full CLI — must survive any error."""
    import click

    try:
        click_cmd.main(args=argv, prog_name="awareness", standalone_mode=False)
    except SystemExit:
        # `--help` and `typer.Exit()` raise SystemExit; that is normal here.
        pass
    except click.exceptions.UsageError as exc:
        rprint(f"[red]{escape(exc.format_message())}[/red]")
        rprint("[dim]Type [bold]help[/bold] for the command map, or [bold]<command> --help[/bold].[/dim]")
    except click.exceptions.Abort:
        rprint("[yellow]Aborted.[/yellow]")
    except click.exceptions.ClickException as exc:
        rprint(f"[red]{escape(exc.format_message())}[/red]")
    except KeyboardInterrupt:
        rprint("\n[yellow]Interrupted.[/yellow]")
    except Exception as exc:  # a bad command must never kill the REPL
        rprint(f"[red]Error:[/red] {escape(str(exc))}")


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
    port: int = typer.Option(_default_api_port, "--port", help="Port to bind to (default: AW_API_PORT or 8085)"),
    tail: bool = typer.Option(True, "--tail/--no-tail", help="Start the live tail daemon in-process after restart"),
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
