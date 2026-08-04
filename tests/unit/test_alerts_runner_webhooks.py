"""Alert runner webhook delivery + legacy ``webhook_url`` mirror invariants.

1. A rule with MULTIPLE webhooks delivers to every one of them (runner no
   longer delivers only ``webhooks[0]`` through the legacy mirror).
2. Patching ``webhooks`` via ``update_rule`` keeps the raw ``webhook_url``
   column in sync with the canonical list (``webhooks[0]``, or None when
   the list is emptied).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from awareness.alerts.models import AlertFiring, AlertRuleCreate
from awareness.alerts.runner import AlertRunner
from awareness.alerts.store import AlertStore

_WH_ONE = "https://hooks.example/one"
_WH_TWO = "https://hooks.example/two"


@pytest.fixture(autouse=True)
def _allow_public_webhooks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass the public-host DNS gate for unit tests (delivery is faked)."""
    monkeypatch.setattr(
        "awareness.alerts.notify.is_public_http_url",
        lambda url: True,
    )


def _firing(rule_id: str = "r1") -> AlertFiring:
    return AlertFiring(
        id=1,
        rule_id=rule_id,
        rule_name="bitcoin watch",
        kind="term_count",
        term="bitcoin",
        count=3,
        threshold=3.0,
        fired_at=datetime.now(UTC),
        detail="3 docs matched 'bitcoin' in the last 24h",
    )


class _Engine:
    """evaluate_rules returns canned firings against a real AlertStore."""

    def __init__(self, store: AlertStore, firings: list[AlertFiring]) -> None:
        self._store = store
        self.firings = firings

    def evaluate_rules(self) -> list[AlertFiring]:
        return self.firings


async def test_runner_delivers_all_webhooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AlertStore(tmp_path / "alerts" / "alerts.db")
    try:
        rule = store.create_rule(
            AlertRuleCreate(
                name="multi webhook",
                kind="term_count",
                term="bitcoin",
                threshold=1.0,
                webhooks=[_WH_ONE, _WH_TWO],
            )
        )
        delivered: list[str] = []

        async def fake_deliver(url: str, firing: AlertFiring) -> bool:
            delivered.append(url)
            return True

        monkeypatch.setattr("awareness.alerts.runner.deliver_webhook", fake_deliver)
        engine = _Engine(store, [_firing(rule.id)])
        runner = AlertRunner(lambda: engine, interval_seconds=300.0)

        firings = await runner.evaluate_once()

        assert firings == engine.firings
        assert delivered == [_WH_ONE, _WH_TWO]
    finally:
        store.close()


async def test_runner_legacy_webhook_url_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule with only the deprecated webhook_url still delivers once."""
    store = AlertStore(tmp_path / "alerts" / "alerts.db")
    try:
        rule = store.create_rule(
            AlertRuleCreate(
                name="legacy hook",
                kind="term_count",
                term="bitcoin",
                threshold=1.0,
                webhook_url=_WH_ONE,
            )
        )
        delivered: list[str] = []

        async def fake_deliver(url: str, firing: AlertFiring) -> bool:
            delivered.append(url)
            return True

        monkeypatch.setattr("awareness.alerts.runner.deliver_webhook", fake_deliver)
        engine = _Engine(store, [_firing(rule.id)])
        runner = AlertRunner(lambda: engine, interval_seconds=300.0)

        await runner.evaluate_once()

        assert delivered == [_WH_ONE]
    finally:
        store.close()


def test_update_rule_webhooks_mirror_raw_row(tmp_path: Path) -> None:
    """Patching webhooks must persist webhook_url == webhooks[0] in the raw
    row (the legacy mirror column never goes stale)."""
    store = AlertStore(tmp_path / "alerts" / "alerts.db")
    try:
        rule = store.create_rule(
            AlertRuleCreate(
                name="mirror test",
                kind="term_count",
                term="bitcoin",
                threshold=1.0,
                webhooks=["https://hooks.example/legacy"],
            )
        )
        updated = store.update_rule(
            rule.id, {"webhooks": [_WH_ONE, _WH_TWO]}
        )
        assert updated.webhooks == [_WH_ONE, _WH_TWO]
        assert updated.webhook_url == _WH_ONE

        raw = store._conn.execute(
            "SELECT webhook_url, webhooks_json FROM rules WHERE id = ?", (rule.id,)
        ).fetchone()
        assert raw is not None
        assert raw["webhook_url"] == _WH_ONE
        assert json.loads(raw["webhooks_json"]) == [_WH_ONE, _WH_TWO]
    finally:
        store.close()


def test_update_rule_empty_webhooks_clears_mirror(tmp_path: Path) -> None:
    """Patching webhooks=[] clears the legacy mirror column to NULL."""
    store = AlertStore(tmp_path / "alerts" / "alerts.db")
    try:
        rule = store.create_rule(
            AlertRuleCreate(
                name="clear hooks",
                kind="term_count",
                term="bitcoin",
                threshold=1.0,
                webhooks=[_WH_ONE],
            )
        )
        updated = store.update_rule(rule.id, {"webhooks": []})
        assert updated.webhooks == []
        assert updated.webhook_url is None

        raw = store._conn.execute(
            "SELECT webhook_url, webhooks_json FROM rules WHERE id = ?", (rule.id,)
        ).fetchone()
        assert raw is not None
        assert raw["webhook_url"] is None
        assert json.loads(raw["webhooks_json"]) == []
    finally:
        store.close()
