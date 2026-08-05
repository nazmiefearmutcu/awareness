"""``awareness alerts`` — alert rule management and evaluation.

Commands:

* ``list``    — show all alert rules
* ``create``  — add a term_count / term_spike rule
* ``delete``  — remove a rule by id
* ``check``   — evaluate all active rules and print firings

The store lives at ``<data_dir>/alerts.db`` and evaluation runs against the
same DuckDbIndex the API uses.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import typer
from rich.console import Console
from rich.table import Table

from awareness.alerts.engine import AlertEngine
from awareness.alerts.models import AlertRuleCreate
from awareness.alerts.notify import validate_webhook_url
from awareness.alerts.store import AlertStore
from awareness.config import get_settings
from awareness.obs.logging import get_logger
from awareness.storage.duckdb_index import DuckDbIndex

app = typer.Typer(
    no_args_is_help=True, help="Alert rules: create, list, delete, check, export, import, weekly"
)
console = Console()
logger = get_logger("alerts.cli")

_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"
_WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _week_sparkline(counts: list[int]) -> str:
    """One block char per weekday count (min-max scaled, 8 levels).

    Local mirror of ``cli.main._sparkline`` for exactly seven values —
    this module cannot import it at module level without a circular import
    (``cli.main`` wires the alerts subcommand from here).
    """
    values = [float(c) for c in counts]
    low = min(values)
    top = max(values)
    if not values or low == top:
        return _SPARK_BLOCKS[0] * len(values)
    scale = 7.0 / (top - low)
    out = []
    for value in values:
        idx = round((value - low) * scale)
        out.append(_SPARK_BLOCKS[min(max(idx, 0), 7)])
    return "".join(out)


def _store() -> AlertStore:
    """AlertStore at ``<data_dir>/alerts.db``."""
    settings = get_settings()
    assert settings.data_dir is not None
    return AlertStore(settings.data_dir / "alerts.db")


def _index() -> DuckDbIndex:
    """Process index over the same paths the API server uses."""
    settings = get_settings()
    assert settings.data_dir is not None
    return DuckDbIndex(
        db_path=settings.duckdb_path(),
        jsonl_dir=settings.staging_jsonl_dir(),
        iceberg_warehouse=settings.iceberg_warehouse,
    )


@app.command(name="list")
def list_rules() -> None:
    """List all alert rules."""
    store = _store()
    try:
        rules = store.list_rules()
    finally:
        store.close()
    if not rules:
        console.print("No alert rules configured.")
        return
    table = Table(title="Alert rules")
    for col in (
        "ID", "Name", "Kind", "Term", "Threshold", "Window(h)",
        "Cooldown(min)", "Webhooks", "Active",
    ):
        table.add_column(col)
    for r in rules:
        table.add_row(
            r.id, r.name, r.kind, r.term, f"{r.threshold:g}", f"{r.window_hours:g}",
            f"{r.cooldown_minutes:g}", ", ".join(r.webhooks) or "-",
            "yes" if r.active else "no",
        )
    console.print(table)


@app.command(name="create")
def create_rule(  # noqa: PLR0917 - spec-mandated option surface
    name: str = typer.Option(..., "--name", "-n", help="Rule name"),
    kind: Literal["term_count", "term_spike"] = typer.Option(
        "term_count", "--kind", "-k", help="Rule kind: term_count or term_spike"
    ),
    term: str = typer.Option(..., "--term", "-t", help="Term to watch for"),
    threshold: float = typer.Option(
        ..., "--threshold", "--threshold-value", help="Count threshold that fires the rule"
    ),
    window_hours: float = typer.Option(
        24.0, "--window-hours", help="Rolling window length in hours"
    ),
    webhook_url: str | None = typer.Option(
        None, "--webhook-url", help="Optional webhook URL notified on firing (deprecated)"
    ),
    webhooks: list[str] | None = typer.Option(  # noqa: B008
        None, "--webhook", "-w", help="Optional webhook URL (repeatable)"
    ),
    webhook_format: Literal["json", "slack"] = typer.Option(
        "json", "--webhook-format", help="Delivery format: json or slack"
    ),
    cooldown_minutes: float = typer.Option(
        30.0, "--cooldown-minutes", help="Cooldown between firings (minutes)"
    ),
    active: bool = typer.Option(
        True, "--active/--no-active", help="Create the rule active"
    ),
) -> None:
    """Create a new alert rule."""
    urls = list(webhooks or [])
    if webhook_url:
        urls.append(webhook_url)
    for url in urls:
        try:
            validate_webhook_url(url)
        except ValueError as exc:
            raise typer.BadParameter(f"invalid webhook URL {url!r}: {exc}") from exc
    try:
        payload = AlertRuleCreate(
            name=name,
            kind=kind,
            term=term,
            threshold=threshold,
            window_hours=window_hours,
            webhooks=webhooks or [],
            webhook_url=webhook_url,
            webhook_format=webhook_format,
            cooldown_minutes=cooldown_minutes,
            active=active,
        )
    except ValueError as exc:
        console.print(f"[red]invalid rule: {exc}[/red]")
        raise typer.Exit(code=2) from exc
    store = _store()
    try:
        rule = store.create_rule(payload)
    finally:
        store.close()
    console.print(
        f"Created rule [bold cyan]{rule.name}[/bold cyan] ({rule.id}) "
        f"kind={rule.kind} term={rule.term!r} threshold={rule.threshold:g}"
    )


@app.command(name="delete")
def delete_rule(rule_id: str = typer.Argument(..., help="Rule id to delete")) -> None:
    """Delete an alert rule by id."""
    store = _store()
    try:
        deleted = store.delete_rule(rule_id)
    finally:
        store.close()
    if not deleted:
        console.print(f"[yellow]No rule with id {rule_id}[/yellow]")
        raise typer.Exit(code=2)
    console.print(f"Deleted rule {rule_id}")


@app.command(name="check")
def check() -> None:
    """Evaluate all active rules against the corpus and print firings."""
    store = _store()
    index = _index()
    try:
        try:
            firings = AlertEngine(index, store).evaluate_rules()
        except RuntimeError as exc:
            console.print(f"[yellow]index not ready: {exc}[/yellow]")
            raise typer.Exit(code=2) from exc
    finally:
        index.close()
        store.close()
    if not firings:
        console.print("No alert firings.")
        return
    table = Table(title="Alert firings")
    for col in (
        "Firing ID", "Rule ID", "Rule", "Kind", "Term", "Count",
        "Threshold", "Fired At", "Detail",
    ):
        table.add_column(col)
    for f in firings:
        table.add_row(
            str(f.id), f.rule_id, f.rule_name, f.kind, f.term, str(f.count),
            f"{f.threshold:g}", f.fired_at.isoformat(), f.detail,
        )
    console.print(table)


@app.command(name="export")
def export_rules(
    out: str | None = typer.Option(
        None, "--out", help="Write rules JSON to FILE instead of stdout"
    ),
) -> None:
    """Export all alert rules as a JSON array (webhooks included)."""
    store = _store()
    try:
        try:
            rules = store.export_rules()
        finally:
            store.close()
    except Exception as exc:
        console.print(f"[red]export failed: {exc}[/red]")
        raise typer.Exit(code=1) from exc
    text = json.dumps(rules, indent=2, ensure_ascii=False) + "\n"
    if out:
        try:
            Path(out).write_text(text, encoding="utf-8")
        except OSError as exc:
            console.print(f"[red]export failed: {exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(f"Exported {len(rules)} rules to {out}")
    else:
        console.print(text)


@app.command(name="import")
def import_rules(
    file: Path = typer.Argument(  # noqa: B008
        ..., help="JSON file with an array of rules"
    ),
    replace: bool = typer.Option(
        False, "--replace", help="Delete and recreate rules whose name already exists"
    ),
) -> None:
    """Import alert rules from a JSON file (skips existing names; exit 1 on error)."""
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        console.print(f"[red]import failed: {exc}[/red]")
        raise typer.Exit(code=1) from exc
    if not isinstance(data, list):
        console.print("[red]import failed: file must contain a JSON array of rules[/red]")
        raise typer.Exit(code=1)
    store = _store()
    try:
        existing = {r.name for r in store.list_rules()}
        try:
            created, skipped = store.import_rules(data, replace=replace)
        except ValueError as exc:
            console.print(f"[red]import failed: {exc}[/red]")
            raise typer.Exit(code=1) from exc
    finally:
        store.close()
    if skipped and not replace:
        for raw in data:
            if isinstance(raw, dict) and raw.get("name") in existing:
                console.print(
                    f"[yellow]skipped existing rule {raw.get('name')!r} "
                    "(use --replace to overwrite)[/yellow]"
                )
    console.print(f"Imported {created} rules, skipped {skipped}.")


@app.command(name="history")
def history(
    limit: int = typer.Option(
        50, "--limit", "-l", min=1, max=500, help="Max firings to show"
    ),
    since_hours: int | None = typer.Option(
        None, "--since", min=0, help="Only show firings from the last N hours"
    ),
    rule_id: str | None = typer.Option(
        None, "--rule", help="Only show firings for this rule id"
    ),
    json_out: bool = typer.Option(False, "--json", help="Output a raw JSON array"),
) -> None:
    """Show recent alert firings (newest first)."""
    since: datetime | None = None
    if since_hours is not None:
        since = datetime.now(UTC) - timedelta(hours=since_hours)
    store = _store()
    try:
        # The store filters in SQL by time only; a --rule filter is applied
        # here, so fetch the max clamp when filtering to keep --limit honest.
        firings = store.list_firings(limit=500 if rule_id else limit, since=since)
    finally:
        store.close()
    if rule_id:
        firings = [f for f in firings if f["rule_id"] == rule_id][:limit]
    if not firings:
        console.print("No firings recorded.")
        return
    if json_out:
        from datetime import datetime as _dt  # noqa: PLC0415

        print(
            json.dumps(
                firings,
                indent=2,
                ensure_ascii=False,
                default=lambda o: o.isoformat() if isinstance(o, _dt) else str(o),
            )
        )
        return
    table = Table(title="Alert firing history")
    for col in ("Fired At", "Rule", "Kind", "Term", "Count", "Threshold", "Detail"):
        table.add_column(col)
    for f in firings:
        fired_at = f["fired_at"].astimezone().strftime("%Y-%m-%d %H:%M")
        detail = f["detail"]
        if len(detail) > 80:
            detail = detail[:77] + "..."
        table.add_row(
            fired_at, f["rule_name"], f["kind"], f["term"], str(f["count"]),
            f"{f['threshold']:g}", detail,
        )
    console.print(table)


@app.command(name="weekly")
def weekly(
    json_out: bool = typer.Option(False, "--json", help="Output the summary as raw JSON"),
) -> None:
    """7-day alert summary: firings per rule + weekday distribution.

    Covers the last 7 days (UTC): total firings, an exact per-rule count
    (via SQL, so it survives the 500-row list clamp), each rule's last
    firing, the top rule, and a Monday..Sunday distribution rendered as a
    block sparkline. An empty week prints a clean message (exit 0).
    """
    since = datetime.now(UTC) - timedelta(days=7)
    store = _store()
    try:
        total = store.count_firings_since(since)
        rules = store.list_rules()
        # The list powers the weekday distribution and last-fired per rule.
        firings = store.list_firings(limit=500, since=since)
        # Per-rule counts are exact SQL aggregates; rule_ids span configured
        # rules plus any firing whose rule was deleted since (folded in with
        # the firing-row name/term snapshot).
        rule_ids = sorted({r.id for r in rules} | {f["rule_id"] for f in firings})
        counts = {rid: store.count_firings_since(since, rid) for rid in rule_ids}
    finally:
        store.close()

    by_name: dict[str, str] = {r.id: r.name for r in rules}
    by_term: dict[str, str] = {r.id: r.term for r in rules}
    by_weekday = [0] * 7
    last_fired: dict[str, datetime] = {}
    for f in firings:
        by_weekday[f["fired_at"].weekday()] += 1
        if f["rule_id"] not in last_fired or f["fired_at"] > last_fired[f["rule_id"]]:
            last_fired[f["rule_id"]] = f["fired_at"]
        by_name.setdefault(f["rule_id"], f["rule_name"])
        by_term.setdefault(f["rule_id"], f["term"])

    fired: list[dict[str, Any]] = [
        {
            "rule_id": rid,
            "rule_name": by_name.get(rid, rid),
            "term": by_term.get(rid, ""),
            "count": counts[rid],
            "last_fired": last_fired.get(rid),
        }
        for rid in rule_ids
        if counts[rid] > 0
    ]
    fired.sort(key=lambda row: (-row["count"], row["rule_name"]))
    top = fired[0] if fired else None

    if not fired:
        console.print("No alert firings in the last 7 days.")
        return
    if json_out:
        print(
            json.dumps(
                {
                    "window_days": 7,
                    "since": since.isoformat(),
                    "total_firings": total,
                    "rules": [
                        {
                            "rule_id": row["rule_id"],
                            "rule_name": row["rule_name"],
                            "term": row["term"],
                            "count": row["count"],
                            "last_fired": row["last_fired"].isoformat()
                            if row["last_fired"] is not None
                            else None,
                        }
                        for row in fired
                    ],
                    "top_rule": {
                        "rule_id": top["rule_id"],
                        "rule_name": top["rule_name"],
                        "count": top["count"],
                    }
                    if top is not None
                    else None,
                    "rules_fired": len(fired),
                    "rules_total": len(rule_ids),
                    "by_weekday": {
                        label: by_weekday[i] for i, label in enumerate(_WEEKDAY_LABELS)
                    },
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    console.print(
        f"[bold]{total}[/bold] firing(s) in the last 7 days · "
        f"[bold]{len(fired)}/{len(rule_ids)}[/bold] rules fired"
        + (f" · top: [bold cyan]{top['rule_name']}[/bold cyan] ({top['count']})" if top else "")
    )
    table = Table(title=f"Firings per rule (since {since:%Y-%m-%d})")
    for col in ("Rule", "Term", "Count", "Last Fired"):
        table.add_column(col)
    for row in fired:
        table.add_row(
            row["rule_name"],
            row["term"],
            str(row["count"]),
            row["last_fired"].astimezone().strftime("%Y-%m-%d %H:%M")
            if row["last_fired"] is not None
            else "-",
        )
    console.print(table)
    console.print(
        "[dim]weekday firings (Mon-Sun):[/dim] "
        + _week_sparkline(by_weekday)
        + "  "
        + " ".join(str(c) for c in by_weekday)
    )
