"""CLI tests for ``awareness alerts weekly`` (7-day summary + --json).

Firings are seeded through :class:`AlertStore` at the project's
``<data_dir>/alerts.db`` — the exact path the CLI opens — and their
``fired_at`` is backdated to fixed day offsets with a direct SQLite UPDATE
(the store stamps ``fired_at`` itself, so backdating is test-only). The
command is then driven through Typer's CliRunner.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from awareness.alerts.store import AlertStore
from awareness.cli.main import app

runner = CliRunner()

_WEEKDAY_KEYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _db_path(tmp_project: Path) -> Path:
    return tmp_project / "data" / "alerts.db"


def _store(tmp_project: Path) -> AlertStore:
    return AlertStore(_db_path(tmp_project))


def _seed(
    tmp_project: Path,
    *,
    rule_id: str,
    rule_name: str = "bitcoin mentions",
    term: str = "bitcoin",
    days_ago: list[int],
    count: float = 12.0,
) -> list[int]:
    """Record ``len(days_ago)`` firings, each backdated to ``now - d`` days."""
    store = _store(tmp_project)
    ids: list[int] = []
    try:
        for _d in days_ago:
            ids.append(
                store.record_firing(
                    rule_id=rule_id,
                    rule_name=rule_name,
                    kind="term_count",
                    term=term,
                    count=count,
                    threshold=5.0,
                    detail=f"{count} mentions in the last 24h",
                )
            )
    finally:
        store.close()
    now = datetime.now(UTC)
    conn = sqlite3.connect(str(_db_path(tmp_project)))
    try:
        for firing_id, d in zip(ids, days_ago, strict=True):
            conn.execute(
                "UPDATE firings SET fired_at = ? WHERE id = ?",
                ((now - timedelta(days=d)).isoformat(), firing_id),
            )
        conn.commit()
    finally:
        conn.close()
    return ids


def test_weekly_table_shows_both_rules_and_totals(tmp_project: Path) -> None:
    _seed(
        tmp_project, rule_id="r1", rule_name="bitcoin mentions", term="bitcoin",
        days_ago=[1, 2, 3],
    )
    _seed(
        tmp_project, rule_id="r2", rule_name="ether spike", term="ether",
        days_ago=[1, 1],
    )

    result = runner.invoke(app, ["alerts", "weekly"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "5 firing(s) in the last 7 days" in out
    assert "2/2 rules fired" in out
    assert "top: bitcoin mentions (3)" in out
    assert "bitcoin" in out
    assert "ether" in out
    assert "Last Fired" in out
    assert "█" in out  # weekday sparkline block char (3 firings on one day)


def test_weekly_json_shape(tmp_project: Path) -> None:
    _seed(
        tmp_project, rule_id="r1", rule_name="bitcoin mentions", term="bitcoin",
        days_ago=[1, 2, 3],
    )
    _seed(
        tmp_project, rule_id="r2", rule_name="ether spike", term="ether",
        days_ago=[1, 1],
    )

    result = runner.invoke(app, ["alerts", "weekly", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["window_days"] == 7
    assert payload["total_firings"] == 5
    assert payload["rules_fired"] == 2
    assert payload["rules_total"] == 2
    assert len(payload["rules"]) == 2
    top, second = payload["rules"]
    assert top["rule_id"] == "r1"  # highest count first
    assert top["rule_name"] == "bitcoin mentions"
    assert top["term"] == "bitcoin"
    assert top["count"] == 3
    assert top["last_fired"]  # ISO string
    assert second["count"] == 2
    assert payload["top_rule"] == {"rule_id": "r1", "rule_name": "bitcoin mentions", "count": 3}
    assert list(payload["by_weekday"]) == list(_WEEKDAY_KEYS)
    assert sum(payload["by_weekday"].values()) == 5
    assert payload["since"]  # ISO timestamp


def test_weekly_empty_message(tmp_project: Path) -> None:
    result = runner.invoke(app, ["alerts", "weekly"])
    assert result.exit_code == 0, result.output
    assert "No alert firings in the last 7 days." in result.output


def test_weekly_excludes_firings_older_than_7_days(tmp_project: Path) -> None:
    _seed(tmp_project, rule_id="r1", rule_name="bitcoin mentions", days_ago=[8])

    result = runner.invoke(app, ["alerts", "weekly"])
    assert result.exit_code == 0, result.output
    assert "No alert firings in the last 7 days." in result.output

    result = runner.invoke(app, ["alerts", "weekly", "--json"])
    assert result.exit_code == 0, result.output
    assert "No alert firings in the last 7 days." in result.output


def test_weekly_rule_with_no_activity_still_in_rules_total(tmp_project: Path) -> None:
    _seed(tmp_project, rule_id="r1", rule_name="bitcoin mentions", days_ago=[2])

    result = runner.invoke(app, ["alerts", "weekly", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["total_firings"] == 1
    assert payload["rules_fired"] == 1
    assert payload["rules_total"] == 1
