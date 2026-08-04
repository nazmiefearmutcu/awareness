"""import_rules must be all-or-nothing (alerts/store.py).

An invalid entry mid-list raises BEFORE any rule is written — the store is
left unchanged (no partial import).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from awareness.alerts.store import AlertStore


def _valid(name: str) -> dict[str, object]:
    return {
        "name": name,
        "kind": "term_count",
        "term": "bitcoin",
        "threshold": 3.0,
        "window_hours": 24.0,
        "cooldown_minutes": 30.0,
        "active": True,
    }


def _store(tmp_path: Path) -> AlertStore:
    return AlertStore(tmp_path / "alerts" / "alerts.db")


def test_import_rules_atomic_no_partial_write(tmp_path: Path) -> None:
    """[valid, invalid, valid] → raises AND nothing is persisted."""
    store = _store(tmp_path)
    try:
        with pytest.raises(ValueError, match="invalid rule 'bad rule'"):
            store.import_rules(
                [
                    _valid("good one"),
                    {"name": "bad rule"},  # missing kind/threshold/term
                    _valid("good two"),
                ]
            )
        assert store.list_rules() == []
    finally:
        store.close()


def test_import_rules_atomic_even_with_replace(tmp_path: Path) -> None:
    """Invalid entries abort an import that would otherwise delete rows."""
    store = _store(tmp_path)
    try:
        existing = store.import_rules([_valid("dup")], replace=True)
        assert existing == (1, 0)
        with pytest.raises(ValueError):
            store.import_rules([_valid("dup"), {"name": "broken"}], replace=True)
        # The existing row survived — the invalid import never wrote.
        names = [r.name for r in store.list_rules()]
        assert names == ["dup"]
    finally:
        store.close()


def test_import_rules_all_valid_imports(tmp_path: Path) -> None:
    """All-valid imports still work and dedupe by name."""
    store = _store(tmp_path)
    try:
        created, skipped = store.import_rules([_valid("a"), _valid("b"), _valid("a")])
        assert (created, skipped) == (2, 1)
        assert {r.name for r in store.list_rules()} == {"a", "b"}
    finally:
        store.close()
