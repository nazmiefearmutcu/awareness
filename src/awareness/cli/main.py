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
from awareness.obs.logging import configure_logging, get_logger
from awareness.obs.metrics import get_metrics
from awareness.planner.planner import Planner
from awareness.schemas.doc import SourceKind
from awareness.schemas.jobs import BackfillRequest
from awareness.storage.duckdb_index import DuckDbIndex
from awareness.storage.state import StateDB
from awareness.tail.engine import TailEngine
from awareness.util.timeutil import coerce_relative_end, to_utc
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
console = Console()


def _get_yaml_config_path() -> Path:
    env_path = os.environ.get("AW_CONFIG_FILE")
    if env_path:
        return Path(env_path)
    return Path(__file__).resolve().parents[3] / "configs" / "awareness.yaml"


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


def _update_yaml_config(key: str, value: Any) -> None:
    path = _get_yaml_config_path()
    if path.exists():
        with open(path, "r", encoding="utf-8") as fh:
            try:
                data = yaml.safe_load(fh) or {}
            except Exception:
                data = {}
    else:
        data = {}
    if not isinstance(data, dict):
        data = {}
    
    coerced_value = _coerce_val(str(value))
    data[key] = coerced_value
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, default_flow_style=False)

BANNER = r"""
    _    _  _  _  _    ____  _____ _   _ _____ ____ ____ 
   / \  | |/ \| |/ \  |  _ \|  ___| \ | |  ___/ ___/ ___|
  / _ \ |  / \  / _ \ | |_) | |_  |  \| | |_  \___ \___ \
 / ___ \| /   \/ ___ \|  _ <|  _| | |\  |  _|  ___) |___) |
/_/   \_\/     /_/   \_|_| \_\____|_| \_|____|____/____/ 
"""

def _app_version() -> str:
    try:
        from importlib.metadata import version

        return version("awareness")
    except Exception:
        return "0.1.0"


def _version_callback(value: bool) -> None:
    if value:
        rprint(
            f"[bold cyan]awareness[/bold cyan] v{_app_version()}  "
            "·  public text internet awareness engine"
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
    }
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
    interactive: bool = typer.Option(True, "--interactive/--no-interactive", help="Prompt user for directory path")
) -> None:
    """Initialize storage paths, state DB, Iceberg catalog (idempotent)."""
    settings = get_settings()
    
    if interactive:
        rprint("[bold cyan]Awareness Environment Initialization[/bold cyan]")
        current_dir = settings.data_dir or (settings.project_root / "data")
        rprint(f"Current local data save directory: [yellow]{current_dir}[/yellow]")
        
        change = typer.confirm("Would you like to choose a different local directory for data storage?", default=False)
        if change:
            new_path = typer.prompt("Enter the absolute path to store data files", default=str(current_dir))
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
    settings = get_settings()
    pid_file = settings.data_dir / "state" / "api.pid"
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, OSError):
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
    port: int = typer.Option(8085, "--port", help="Port to bind to"),
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


@app.command()
def stop() -> None:
    """Stop the background Awareness API server (which also stops the tail daemon)."""
    settings = get_settings()
    pid_file = settings.data_dir / "state" / "api.pid"

    launchd_active = False
    try:
        res = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
        if "com.awareness.api.8085" in res.stdout:
            launchd_active = True
    except Exception:
        pass

    if launchd_active:
        rprint("[yellow]Detected com.awareness.api.8085 running via launchd. Unloading it to stop completely...[/yellow]")
        plist_path = Path.home() / "Library" / "LaunchAgents" / "com.awareness.api.8085.plist"
        if plist_path.exists():
            subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        else:
            subprocess.run(["launchctl", "stop", "com.awareness.api.8085"])
        rprint("[green]✔ Stopped and unloaded launchd service[/green]")

    if not pid_file.exists():
        if not launchd_active:
            rprint("[yellow]No background API server process found (PID file does not exist).[/yellow]")
            if _is_port_active("127.0.0.1", 8085):
                rprint("[yellow]Note: Port 8085 is active. Another process might be holding it.[/yellow]")
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
    port: int = typer.Option(8085, "--port", help="Port"),
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
def service_install() -> None:
    """Install and load the API server as a macOS Launch Agent."""
    import plistlib
    settings = get_settings()
    root = settings.project_root
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_path = plist_dir / "com.awareness.api.8085.plist"

    venv_python = root / ".venv" / "bin" / "python"
    if not venv_python.exists():
        venv_python = Path(sys.executable)

    plist_data = {
        "Label": "com.awareness.api.8085",
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {
            "PYTHONPATH": str(root / "src")
        },
        "ProgramArguments": [
            str(venv_python),
            "-c",
            "from awareness.api.server import run; run()"
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": "/tmp/awareness-api-8085.launch.out",
        "StandardErrorPath": "/tmp/awareness-api-8085.launch.err"
    }

    try:
        plist_dir.mkdir(parents=True, exist_ok=True)
        with open(plist_path, "wb") as f:
            plistlib.dump(plist_data, f)
        rprint(f"[green]✔ Plist file created at: {plist_path}[/green]")
        subprocess.run(["launchctl", "load", str(plist_path)], check=True)
        rprint("[green]✔ Service loaded successfully via launchctl.[/green]")
    except Exception as e:
        rprint(f"[red]Error installing service: {e}[/red]")


@service_app.command("uninstall")
def service_uninstall() -> None:
    """Unload and remove the macOS Launch Agent plist."""
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.awareness.api.8085.plist"

    if plist_path.exists():
        try:
            subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
            rprint("[green]✔ Service unloaded via launchctl.[/green]")
            plist_path.unlink()
            rprint("[green]✔ Plist file removed.[/green]")
        except Exception as e:
            rprint(f"[red]Error uninstalling service: {e}[/red]")
    else:
        rprint("[yellow]Service plist file not found.[/yellow]")


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
    pid = _get_api_pid()
    if pid:
        rprint(f"  API Server:  [green]RUNNING[/green] (PID {pid}) on http://127.0.0.1:8085")
    else:
        if _is_port_active("127.0.0.1", 8085):
            rprint("  API Server:  [green]RUNNING[/green] (Port 8085 active, managed externally)")
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
    """Print dedup index statistics."""
    state, _ = _bootstrap()
    print(json.dumps(state.dedup_stats(), indent=2))


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
    rprint("[bold cyan]" + BANNER + "[/bold cyan]")
    rprint("[bold cyan]================================================================[/bold cyan]")
    rprint("[bold cyan]       AWARENESS ENGINE — INGESTION & STORAGE PERFORMANCE       [/bold cyan]")
    rprint("[bold cyan]================================================================[/bold cyan]\n")
    
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
) -> None:
    state, planner = _bootstrap()
    src = [SourceKind(s) for s in sources] if sources else []
    start_dt = to_utc(start)
    if start_dt is None:
        raise typer.BadParameter("Invalid start date format")
    req = BackfillRequest(
        start=start_dt,
        end=coerce_relative_end(end),
        sources=src,
        domains=domains or None,
        languages=languages or None,
        max_tasks=max_tasks or None,
        notes=notes or None,
    )
    job_id = planner.submit_backfill(req)
    rprint(f"[green]Submitted backfill[/green] job_id=[bold]{job_id}[/bold]")
    print(json.dumps(planner.status(job_id), indent=2, default=str))


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
) -> None:
    """Run pending tasks for ``job_id`` to completion (in-process)."""
    state, planner = _bootstrap()
    engine = WorkerEngine(state, planner, concurrency=concurrency or None, silent_progress=silent_progress)
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
                    rprint("[bold cyan]" + BANNER + "[/bold cyan]")
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
    to_cloud: bool = typer.Option(None, "--to-cloud", help="Enable cloud S3/GDrive storage"),
    to_local: bool = typer.Option(None, "--to-local", help="Enable local JSONL/SQLite storage"),
    warehouse: str = typer.Option(None, "--warehouse", help="S3 bucket / warehouse path (e.g. s3://bucket/path)"),
    interactive: bool = typer.Option(True, "--interactive/--no-interactive", help="Prompt for storage target choice interactively"),
) -> None:
    """Start the tail engine in foreground. Ctrl-C or pressing ENTER stops it cleanly."""
    is_tty = sys.stdin.isatty()
    
    if interactive and is_tty and to_cloud is None and to_local is None:
        rprint("[bold cyan]Tail Storage Configuration[/bold cyan]")
        rprint("Where would you like to save the live captured data?")
        rprint("  [1] Local storage only (JSONL & SQLite/DuckDB index)")
        rprint("  [2] Cloud storage only (S3 and/or Google Drive)")
        rprint("  [3] Both Local and Cloud")
        rprint("  [4] Nowhere (display captures in terminal, do not save)")
        
        choice = typer.prompt("Select option [1-4]", default="1")
        if choice == "1":
            to_local = True
            to_cloud = False
        elif choice == "2":
            to_local = False
            to_cloud = True
        elif choice == "3":
            to_local = True
            to_cloud = True
        elif choice == "4":
            to_local = False
            to_cloud = False
    else:
        if to_local is None:
            to_local = True
        if to_cloud is None:
            to_cloud = False

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
        
    if not to_local:
        os.environ["AW_ENABLE_JSONL_STAGING"] = "False"
    else:
        os.environ["AW_ENABLE_JSONL_STAGING"] = "True"

    reset_settings()
    state, planner = _bootstrap()
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
                    rprint("[bold cyan]" + BANNER + "[/bold cyan]")
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
        job_id = await tail.start(seeds_path=seeds)
        rprint(f"[green]Tail started[/green] job_id=[bold]{job_id}[/bold]")
        rprint("[bold cyan]Type slash commands (e.g. /help, /clear, /status, /stop) or press ENTER to stop.[/bold cyan]\n")
        
        stop_task = asyncio.create_task(listen_for_stop(job_id))
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
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            robots = RobotsCache()
            robots._client = client
            
            table = Table("Type", "Seed URL", "HTTP Status", "Robots.txt", "Parser Status")
            for url, kind in all_feeds:
                allowed = await robots.is_allowed(url, "AwarenessBot/0.1")
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
    end_dt = coerce_relative_end(end)
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
    end_dt = coerce_relative_end(end)
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


def _make_tui_layout(state: StateDB, settings: Any) -> Any:
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
        Layout(name="right_bottom", ratio=1)
    )
    
    # 1. Header
    time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header_text = Text.assemble(
        (" AWARENESS ENGINE TUI DASHBOARD ", "bold reverse cyan"),
        "  |  Local Time: ", (time_str, "yellow"),
        "  |  Controls: ", ("[Q] Quit  [C] Compact  [T] Toggle Tail  [A] Toggle API  [R] Refresh  [L] Logs", "bold green")
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
    jobs_table = Table(expand=True, box=None)
    jobs_table.add_column("Job ID", style="cyan")
    jobs_table.add_column("Kind", style="white")
    jobs_table.add_column("Status", style="bold green")
    jobs_table.add_column("Tasks", style="white")
    jobs_table.add_column("Docs", style="green")
    jobs_table.add_column("Dedup", style="yellow")
    
    for j in jobs:
        status_color = "green" if j.status.value == "completed" else "yellow" if j.status.value == "running" else "red"
        jobs_table.add_row(
            j.job_id[:12],
            j.kind.value,
            f"[{status_color}]{j.status.value}[/{status_color}]",
            f"{j.tasks_completed}/{j.tasks_total}",
            str(j.docs_emitted),
            str(j.docs_dedup_dropped),
        )
    layout["right_top"].update(Panel(jobs_table, title="[bold white]Recent Jobs[/bold white]", border_style="magenta"))
    
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
    state, _ = _bootstrap()
    settings = get_settings()
    
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
                env["AW_API_HOST"] = "127.0.0.1"
                env["AW_API_PORT"] = "8085"
                log_dir = settings.log_dir
                log_dir.mkdir(parents=True, exist_ok=True)
                api_log_path = log_dir / "api.log"
                with open(api_log_path, "a", encoding="utf-8") as lf:
                    subprocess.Popen(
                        [sys.executable, "-c", "from awareness.api.server import run; run()"],
                        env=env,
                        stdout=lf,
                        stderr=subprocess.STDOUT,
                        start_new_session=True
                    )
                return "[green]Spawning API server on http://127.0.0.1:8085...[/green]"
            except Exception as e:
                return f"[red]Failed to start API: {e}[/red]"
                
    try:
        layout = _make_tui_layout(state, settings)
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
                
                # Check refresh interval
                now = time.time()
                if now - last_update >= refresh_rate:
                    if current_view == "dashboard":
                        layout = _make_tui_layout(state, settings)
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


@app.command(name="browse")
def browse(
    start: str = typer.Option("30 days ago", "--start", help="Start date range"),
    end: str = typer.Option("now", "--end", help="End date range"),
    domain: str = typer.Option("", "--domain", help="Filter by domain"),
    source: str = typer.Option("", "--source", help="Filter by source"),
) -> None:
    """Interactively browse and read captured text documents from the terminal."""
    state, _ = _bootstrap()
    settings = get_settings()
    idx = DuckDbIndex(
        db_path=settings.duckdb_path(),
        jsonl_dir=settings.staging_jsonl_dir(),
        iceberg_warehouse=settings.iceberg_warehouse,
    )
    
    start_dt = to_utc(start)
    end_dt = coerce_relative_end(end)
    
    # Clear screen
    print("\033[H\033[2J\033[3J", end="")
    
    offset = 0
    limit = 10
    
    while True:
        where = ["fetch_ts >= $start", "fetch_ts <= $end"]
        params = {"start": start_dt, "end": end_dt}
        if domain:
            where.append("domain = $dom")
            params["dom"] = domain
        if source:
            where.append("source_type = $src")
            params["src"] = source
            
        where_sql = " AND ".join(where)
        sql = f"""
            SELECT doc_id, domain, title, fetch_ts, source_type, text
            FROM captures
            WHERE {where_sql}
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
                rprint("[yellow]No captures found in this range.[/yellow]")
                break
            else:
                rprint("[yellow]No more pages. Going back...[/yellow]")
                offset = max(0, offset - limit)
                continue
                
        # Display table
        table = Table(title=f"Awareness Documents - Page {offset // limit + 1} (Offset: {offset})")
        table.add_column("#", justify="center", style="yellow")
        table.add_column("Domain", style="cyan")
        table.add_column("Title", style="white")
        table.add_column("Date Captured", style="dim green")
        table.add_column("Source", style="magenta")
        
        for i, r in enumerate(rows, 1):
            title = r["title"] or "No Title"
            if len(title) > 50:
                title = title[:47] + "..."
            table.add_row(
                str(i),
                r["domain"] or "N/A",
                title,
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
                rprint(f"[bold cyan]Title:[/bold cyan]       {doc['title']}")
                rprint(f"[bold cyan]Domain:[/bold cyan]      {doc['domain']}")
                rprint(f"[bold cyan]Captured at:[/bold cyan] {doc['fetch_ts']}")
                rprint(f"[bold cyan]Source:[/bold cyan]      {doc['source_type']}")
                rprint(f"[bold cyan]Doc ID:[/bold cyan]      {doc['doc_id']}\n")
                rprint("-" * 80)
                
                # Display body text with word wrapping
                rprint(doc["text"] or "[Empty Document]")
                rprint("-" * 80)
                typer.prompt("\nPress ENTER to return to list")
                print("\033[H\033[2J\033[3J", end="")
            else:
                rprint("[red]Invalid document index.[/red]")


@app.command(name="search")
def search(
    query: str = typer.Argument(..., help="FTS search query (BM25 ranked if FTS is available)"),
    start: str = typer.Option("30 days ago", "--start", help="Start date range"),
    end: str = typer.Option("now", "--end", help="End date range"),
    domain: str = typer.Option("", "--domain", help="Filter by domain"),
    source: str = typer.Option("", "--source", help="Filter by source"),
    limit: int = typer.Option(10, "--limit", "-l", help="Number of search results per page"),
    interactive: bool = typer.Option(True, "--interactive/--no-interactive", help="Enable interactive browsing of search results"),
) -> None:
    """Search ingested documents using Full-Text Search (FTS) or substring match."""
    state, _ = _bootstrap()
    settings = get_settings()
    idx = DuckDbIndex(
        db_path=settings.duckdb_path(),
        jsonl_dir=settings.staging_jsonl_dir(),
        iceberg_warehouse=settings.iceberg_warehouse,
    )
    
    start_dt = to_utc(start)
    end_dt = coerce_relative_end(end)
    
    if not interactive or not sys.stdin.isatty():
        res = idx.search(
            query=query,
            limit=limit,
            offset=0,
            source=source if source else None,
            domain=domain if domain else None,
            start=start_dt,
            end=end_dt,
        )
        total = res["total"]
        rows = res["rows"]
        ranked = res["ranked"]
        
        rprint(f"[bold cyan]Search Results for:[/bold cyan] '{query}' (Found {total} documents, showing top {len(rows)}, Ranked: {ranked})")
        rprint("-" * 80)
        for r in rows:
            title = r["title"] or "No Title"
            score_str = f" [score: {r['score']:.4f}]" if r["score"] is not None else ""
            rprint(f"[bold yellow]• {title}[/bold yellow]{score_str}")
            rprint(f"  [dim]Domain: {r['domain'] or 'N/A'} | Captured: {r['fetch_ts']} | Source: {r['source_type'] or 'N/A'}[/dim]")
            if r.get("snippet"):
                rprint(f"  [italic]\"{r['snippet']}\"[/italic]")
            rprint()
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
            start=start_dt,
            end=end_dt,
        )
        
        total = res["total"]
        rows = res["rows"]
        ranked = res["ranked"]
        
        if not rows:
            if offset == 0:
                rprint(f"[yellow]No documents matched query '{query}'.[/yellow]")
                break
            else:
                rprint("[yellow]No more pages. Going back...[/yellow]")
                offset = max(0, offset - limit)
                continue

        table = Table(title=f"Search Results for '{query}' - Page {offset // limit + 1} (Found {total} total, Ranked: {ranked})")
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
            snippet = r.get("snippet", "")
            if snippet:
                title_and_snippet = f"[bold]{title}[/bold]\n  [dim]{snippet}[/dim]"
            else:
                title_and_snippet = f"[bold]{title}[/bold]"
                
            table.add_row(
                str(i),
                score_val,
                r["domain"] or "N/A",
                title_and_snippet,
                str(r["fetch_ts"])[:16]
            )
            
        console.print(table)
        
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
                    rprint(f"[bold cyan]Title:[/bold cyan]       {doc['title']}")
                    rprint(f"[bold cyan]Domain:[/bold cyan]      {doc['domain']}")
                    rprint(f"[bold cyan]Captured at:[/bold cyan] {doc['fetch_ts']}")
                    rprint(f"[bold cyan]Source:[/bold cyan]      {doc['source_type']}")
                    rprint(f"[bold cyan]Doc ID:[/bold cyan]      {doc['doc_id']}\n")
                    rprint("-" * 80)
                    
                    import re
                    from rich.markup import escape
                    text_body = doc["text"] or "[Empty Document]"
                    escaped_text = escape(text_body)
                    terms = [t for t in re.findall(r"[A-Za-z0-9']+", query.lower()) if len(t) >= 2]
                    if terms:
                        pattern = re.compile(r"\b(" + "|".join(re.escape(t) for t in terms) + r")\b", re.IGNORECASE)
                        highlighted_text = pattern.sub(lambda m: f"[bold yellow]{m.group(0)}[/bold yellow]", escaped_text)
                    else:
                        highlighted_text = escaped_text
                    
                    rprint(highlighted_text)
                    rprint("-" * 80)
                    typer.prompt("\nPress ENTER to return to search results")
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
    output: Path = typer.Option(..., "--output", "-o", help="File path or folder to save exported documents"),
    domain: str = typer.Option("", "--domain", help="Filter documents by domain"),
    source: str = typer.Option("", "--source", help="Filter documents by source type"),
    format_type: str = typer.Option("jsonl", "--format", help="Export format: 'jsonl' or 'txt'"),
) -> None:
    """Export captured documents into a single JSONL file or raw text files folder."""
    state, _ = _bootstrap()
    settings = get_settings()
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
    
    rprint("[yellow]Fetching documents to export...[/yellow]")
    try:
        rows = idx.execute(sql, params)
    except Exception as e:
        rprint(f"[red]Failed to query captures: {e}[/red]")
        return
        
    if not rows:
        rprint("[yellow]No captures matched your filters.[/yellow]")
        return
        
    rprint(f"[bold cyan]Found {len(rows)} documents to export.[/bold cyan]")
    
    if format_type.lower() == "jsonl":
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            with open(output, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, default=str, ensure_ascii=False) + "\n")
            rprint(f"[green]✔ Successfully exported documents to JSONL file: [bold]{output}[/bold][/green]")
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
                p = output / filename
                p.write_text(r["text"] or "", encoding="utf-8")
                written += 1
            rprint(f"[green]✔ Successfully exported {written} document text files to folder: [bold]{output}[/bold][/green]")
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
) -> None:
    """Check if a URL or text has already been ingested (exact or near-duplicate check)."""
    state, _ = _bootstrap()
    from awareness.util.hashing import content_hash, simhash64, hamming64
    from awareness.storage.state import DedupRow
    from sqlalchemy import select
    
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
                
        sh = simhash64(text_content)
        rprint(f"Computed Simhash Value:     [bold cyan]{sh}[/bold cyan]")
        
        candidates = state.find_near_dup_candidates(sh)
        near_match = None
        min_dist = 64
        
        for doc_id, other_hash in candidates:
            other_hash_unsigned = other_hash & 0xFFFFFFFFFFFFFFFF
            dist = hamming64(sh, other_hash_unsigned)
            if dist <= 3:
                if dist < min_dist:
                    min_dist = dist
                    near_match = doc_id
                    
        if near_match:
            rprint(f"[yellow]⚠ NEAR-DUPLICATE DETECTED (Hamming Distance: {min_dist}/64)![/yellow]")
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


# ── config subcommand group ─────────────────────────────────────────────
@config_app.command("show")
def config_show() -> None:
    """Show current configuration values."""
    settings = get_settings()
    rprint("[bold cyan]Current Configuration:[/bold cyan]")
    table = Table("Setting", "Value")
    table.add_row("Data Directory (data_dir)", str(settings.data_dir))
    table.add_row("Iceberg Warehouse", str(settings.iceberg_warehouse))
    table.add_row("Iceberg Catalog DB", str(settings.iceberg_catalog_db))
    table.add_row("State DB URL", str(settings.state_db_url))
    table.add_row("Log Level", settings.log_level)
    table.add_row("Log JSON Logs", str(settings.log_json))
    table.add_row("Iceberg Enabled", str(settings.enable_iceberg))
    table.add_row("JSONL Staging Enabled", str(settings.enable_jsonl_staging))
    console.print(table)


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Config parameter name (e.g. data-dir or data_dir)"),
    value: str = typer.Argument(..., help="Value to assign"),
) -> None:
    """Set a configuration parameter persistently."""
    normalized_key = key.replace("-", "_")
    from awareness.config.settings import Settings
    if normalized_key not in Settings.model_fields:
        rprint(f"[red]Error: '{key}' is not a valid configuration setting.[/red]")
        return
    try:
        _update_yaml_config(normalized_key, value)
        reset_settings()
        rprint(f"[green]✔ Successfully set [bold]{key}[/bold] to [bold]{escape(value)}[/bold] in awareness.yaml[/green]")
    except Exception as e:
        rprint(f"[red]Error writing config change: {escape(str(e))}[/red]")


@config_app.command("interactive")
def config_interactive() -> None:
    """Interactively browse and modify configuration settings."""
    settings = get_settings()
    rprint("[bold cyan]Interactive Configuration Editor[/bold cyan]\n")
    
    from awareness.config.settings import Settings
    fields = list(Settings.model_fields.keys())
    
    for i, field in enumerate(fields, 1):
        current_val = getattr(settings, field, None)
        rprint(f"  [{i}] [bold]{field}[/bold]: {current_val}")
    rprint("\n  [0] Save and Exit")
    
    while True:
        choice = typer.prompt("\nSelect a setting to modify [0-N]", default="0")
        if choice == "0":
            rprint("[green]Exited config editor.[/green]")
            break
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(fields):
                field_name = fields[idx]
                current_val = getattr(settings, field_name, None)
                new_val = typer.prompt(f"Enter new value for '{field_name}'", default=str(current_val))
                if new_val != str(current_val):
                    _update_yaml_config(field_name, new_val)
                    reset_settings()
                    settings = get_settings()
                    rprint(f"[green]✔ Set '{field_name}' to '{new_val}' successfully.[/green]")
            else:
                rprint("[red]Invalid selection.[/red]")
        except ValueError:
            rprint("[red]Please enter a valid number.[/red]")


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
            access_token = tokens.get("access_token")
            if access_token:
                folder_id = gdrive._get_or_create_folder(access_token)
                if folder_id:
                    rprint(f"[green]Folder 'Awareness Captures' resolved with ID: {folder_id}[/green]")
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
    if gdrive_authorized:
        auth_data = gdrive.load_auth()
        if auth_data:
            rprint(f"    Client ID:               {auth_data.get('client_id')[:10]}...")
            rprint(f"    Folder Name:             Awareness Captures")


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
            buffer = readline.get_line_buffer()
            tokens = buffer.lstrip().split()
            if len(tokens) <= 1 and not buffer.endswith(" "):
                options = [c for c in top if c.startswith(text)]
            else:
                subs = _shell_subcommands(click_cmd, tokens[0])
                pool = subs if subs else top
                options = [c for c in pool if c.startswith(text)]
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


@app.command(name="commands")
def commands_map() -> None:
    """Show the full, categorised map of every Awareness command."""
    console.print(banner.render_command_map())


@app.command()
def restart(
    host: str = typer.Option("127.0.0.1", "--host", help="Host address to bind to"),
    port: int = typer.Option(8085, "--port", help="Port to bind to"),
) -> None:
    """Restart the background Awareness API server (stop, then start)."""
    import time

    rprint("[cyan]Restarting Awareness API…[/cyan]")
    try:
        stop()
    except Exception as exc:  # restart should proceed even if stop() hiccups
        rprint(f"[yellow]stop() reported: {escape(str(exc))}[/yellow]")
    time.sleep(1.0)
    _shell_dispatch(_shell_click_command(), ["start", "--host", host, "--port", str(port)])


if __name__ == "__main__":
    app()
