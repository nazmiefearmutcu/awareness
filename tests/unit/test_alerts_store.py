"""Unit tests for the AlertStore (sqlite CRUD, cooldown query, firings)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from awareness.alerts.models import AlertRule, AlertRuleCreate
from awareness.alerts.store import AlertStore


def _store(tmp_path: Path) -> AlertStore:
    return AlertStore(tmp_path / "alerts" / "alerts.db")


def _create_rule(store: AlertStore, **overrides: object) -> AlertRule:
    payload = {
        "name": "bitcoin mentions",
        "kind": "term_count",
        "term": "bitcoin",
        "threshold": 3.0,
        "window_hours": 24.0,
        "cooldown_minutes": 30.0,
        "active": True,
        **overrides,
    }
    return store.create_rule(AlertRuleCreate(**payload))


def test_create_and_get_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        rule = _create_rule(store, name="bitcoin", term="Bitcoin  ", threshold=5.0)
        assert rule.id
        assert rule.name == "bitcoin"
        assert rule.term == "Bitcoin"  # stripped by the term validator
        assert rule.kind == "term_count"
        assert rule.threshold == 5.0
        assert rule.window_hours == 24.0
        assert rule.cooldown_minutes == 30.0
        assert rule.active is True
        assert rule.webhook_url is None
        assert rule.created_at.tzinfo is not None
        assert rule.updated_at == rule.created_at

        fetched = store.get_rule(rule.id)
        assert fetched is not None
        assert fetched.model_dump() == rule.model_dump()
    finally:
        store.close()


def test_get_unknown_rule_returns_none(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        assert store.get_rule("nope") is None
    finally:
        store.close()


def test_list_rules_and_active_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        r1 = _create_rule(store, name="active rule")
        _create_rule(store, name="inactive rule", active=False)
        all_rules = store.list_rules()
        assert len(all_rules) == 2
        active = store.list_rules(active_only=True)
        assert [r.id for r in active] == [r1.id]
        assert [r.name for r in all_rules] == ["inactive rule", "active rule"]
    finally:
        store.close()


def test_update_rule_patch_and_timestamps(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        rule = _create_rule(store, name="old name")
        updated = store.update_rule(rule.id, {"name": "new name", "threshold": 9.0})
        assert updated.name == "new name"
        assert updated.threshold == 9.0
        assert updated.term == rule.term
        assert updated.created_at == rule.created_at
        assert updated.updated_at >= rule.updated_at
    finally:
        store.close()


def test_update_rule_missing_raises_keyerror(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        with pytest.raises(KeyError):
            store.update_rule("ghost", {"name": "x"})
    finally:
        store.close()


def test_update_rule_bad_patch_raises_valueerror(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        rule = _create_rule(store)
        with pytest.raises(ValueError):
            store.update_rule(rule.id, {"bogus_field": 1})
        with pytest.raises(ValueError):
            store.update_rule(rule.id, {"term": "bad\x00term"})
    finally:
        store.close()


def test_delete_rule_returns_bool(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        rule = _create_rule(store)
        assert store.delete_rule(rule.id) is True
        assert store.get_rule(rule.id) is None
        assert store.delete_rule(rule.id) is False
    finally:
        store.close()


def test_last_firing_time_none_then_set(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        rule = _create_rule(store)
        assert store.last_firing_time(rule.id) is None
        store.record_firing(
            rule_id=rule.id,
            rule_name=rule.name,
            kind=rule.kind,
            term=rule.term,
            count=4.0,
            threshold=rule.threshold,
            detail="4 docs matched",
        )
        last = store.last_firing_time(rule.id)
        assert last is not None
        assert last.tzinfo is not None
        assert (datetime.now(UTC) - last).total_seconds() < 60
    finally:
        store.close()


def test_record_and_list_firings_order_and_limit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        rule = _create_rule(store)
        ids = [
            store.record_firing(
                rule_id=rule.id,
                rule_name=rule.name,
                kind=rule.kind,
                term=rule.term,
                count=float(n),
                threshold=rule.threshold,
                detail=f"{n} docs",
            )
            for n in (3, 4, 5)
        ]
        rows = store.list_firings(limit=2)
        assert len(rows) == 2
        assert rows[0]["count"] == 5  # newest first
        assert rows[1]["count"] == 4
        assert rows[0]["id"] == ids[2]
        assert rows[0]["rule_id"] == rule.id
        assert rows[0]["rule_name"] == rule.name
        assert rows[0]["fired_at"].tzinfo is not None
        # limit is clamped to 1..500
        assert len(store.list_firings(limit=0)) == 1
        assert len(store.list_firings(limit=99999)) == 3
    finally:
        store.close()


def test_firings_since_filter(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        rule = _create_rule(store)
        store.record_firing(
            rule_id=rule.id, rule_name=rule.name, kind=rule.kind,
            term=rule.term, count=1.0, threshold=rule.threshold, detail="old",
        )
        since = datetime.now(UTC) - timedelta(seconds=5)
        assert store.count_firings_since(since) == 1
        assert store.count_firings_since(datetime.now(UTC) + timedelta(hours=1)) == 0
        rows = store.list_firings(since=since)
        assert len(rows) == 1
        assert store.list_firings(since=datetime.now(UTC) + timedelta(hours=1)) == []
    finally:
        store.close()
