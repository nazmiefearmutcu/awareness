"""CLI tests for ``awareness alerts history`` (firing-history table + --json).

Firings are seeded directly through :class:`AlertStore` at the project's
``<data_dir>/alerts.db`` — the exact path the CLI opens — then the command is
driven through Typer's CliRunner.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from awareness.alerts.store import AlertStore
from awareness.cli.main import app
from awareness.config import get_settings

runner = CliRunner()


def _store() -> AlertStore:
    settings = get_settings()
    assert settings.data_dir is not None
    return AlertStore(settings.data_dir / "alerts.db")


def _seed_firing(
    store: AlertStore,
    *,
    rule_id: str,
    rule_name: str = "bitcoin mentions",
    kind: str = "term_count",
    term: str = "bitcoin",
    count: float = 12.0,
    threshold: float = 5.0,
    detail: str = "12 mentions in the last 24h",
) -> None:
    store.record_firing(
        rule_id=rule_id,
        rule_name=rule_name,
        kind=kind,
        term=term,
        count=count,
        threshold=threshold,
        detail=detail,
    )


def test_history_shows_firings(tmp_project: Path) -> None:
    store = _store()
    try:
        _seed_firing(store, rule_id="r1", rule_name="bitcoin mentions", term="bitcoin")
        _seed_firing(
            store,
            rule_id="r2",
            rule_name="ether spike",
            kind="term_spike",
            term="ether",
            count=88.0,
            threshold=20.0,
        )
    finally:
        store.close()

    result = runner.invoke(app, ["alerts", "history"])
    assert result.exit_code == 0, result.output
    assert "Fired At" in result.output
    assert "bitcoin" in result.output
    assert "ether" in result.output
    assert "12" in result.output  # count column
    assert "88" in result.output


def test_history_json_output(tmp_project: Path) -> None:
    store = _store()
    try:
        _seed_firing(store, rule_id="r1", detail="first")
        _seed_firing(
            store,
            rule_id="r2",
            rule_name="ether spike",
            kind="term_spike",
            term="ether",
            count=88.0,
            threshold=20.0,
            detail="second",
        )
    finally:
        store.close()

    result = runner.invoke(app, ["alerts", "history", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload) == 2
    assert payload[0]["rule_name"] == "ether spike"  # newest first
    assert payload[1]["term"] == "bitcoin"
    assert payload[1]["count"] == 12
    assert payload[1]["threshold"] == 5.0
    assert payload[1]["fired_at"]  # datetime serialized as an ISO string


def test_history_empty_message(tmp_project: Path) -> None:
    result = runner.invoke(app, ["alerts", "history"])
    assert result.exit_code == 0, result.output
    assert "No firings recorded" in result.output


def test_history_since_filter(tmp_project: Path) -> None:
    store = _store()
    try:
        _seed_firing(store, rule_id="r1")
    finally:
        store.close()

    result = runner.invoke(app, ["alerts", "history", "--since", "24"])
    assert result.exit_code == 0, result.output
    assert "bitcoin" in result.output

    result = runner.invoke(app, ["alerts", "history", "--since", "0"])
    assert result.exit_code == 0, result.output
    assert "No firings recorded" in result.output


def test_history_rule_filter(tmp_project: Path) -> None:
    store = _store()
    try:
        _seed_firing(store, rule_id="r1", rule_name="bitcoin mentions")
        _seed_firing(store, rule_id="r2", rule_name="ether spike", term="ether")
    finally:
        store.close()

    result = runner.invoke(app, ["alerts", "history", "--rule", "r1"])
    assert result.exit_code == 0, result.output
    assert "bitcoin" in result.output
    assert "ether" not in result.output


def test_history_detail_truncated_in_table_only(tmp_project: Path) -> None:
    long_detail = "x" * 100 + "ENDMARK"
    store = _store()
    try:
        _seed_firing(store, rule_id="r1", detail=long_detail)
    finally:
        store.close()

    result = runner.invoke(app, ["alerts", "history"])
    assert result.exit_code == 0, result.output
    assert "ENDMARK" not in result.output  # table truncates at 80 chars

    result = runner.invoke(app, ["alerts", "history", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload[0]["detail"] == long_detail  # JSON keeps the full detail
