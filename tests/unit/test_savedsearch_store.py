"""Unit tests for SavedSearchStore (sqlite CRUD, pin, touch, persistence)."""

from __future__ import annotations

from pathlib import Path

import pytest

from awareness.savedsearch.models import SavedSearch
from awareness.savedsearch.store import SavedSearchStore


def _store(tmp_path: Path) -> SavedSearchStore:
    return SavedSearchStore(tmp_path / "saved" / "saved_searches.db")


def _create(store: SavedSearchStore, **overrides: object) -> SavedSearch:
    payload = {
        "name": "alpha watch",
        "query": "alpha  ",
        "mode": "auto",
        "fields": "title,text",
        "limit": 10,
        **overrides,
    }
    return store.create(**payload)  # type: ignore[arg-type]


def test_create_and_get_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        saved = _create(store, name="  btc news  ", query="bitcoin   surge")
        assert saved.id
        assert saved.name == "btc news"  # stripped by the name validator
        assert saved.query == "bitcoin   surge"  # query kept verbatim (no strip inside)
        assert saved.mode == "auto"
        assert saved.fields == "title,text"
        assert saved.limit == 10
        assert saved.pinned is False
        assert saved.created_at.tzinfo is not None
        assert saved.updated_at == saved.created_at

        fetched = store.get(saved.id)
        assert fetched is not None
        assert fetched.model_dump() == saved.model_dump()
    finally:
        store.close()


def test_get_unknown_returns_none(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        assert store.get("nope") is None
    finally:
        store.close()


def test_create_validates_input(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        with pytest.raises(ValueError):
            _create(store, name="")
        with pytest.raises(ValueError):
            _create(store, query="")
        with pytest.raises(ValueError):
            _create(store, query="bad\x00query")
        with pytest.raises(ValueError):
            _create(store, name="bad\x00name")
        with pytest.raises(ValueError):
            _create(store, mode="nope")
        with pytest.raises(ValueError):
            _create(store, limit=0)
        with pytest.raises(ValueError):
            _create(store, limit=201)
        with pytest.raises(ValueError):
            _create(store, fields="   ")
    finally:
        store.close()


def test_list_pinned_first_then_touch_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        a = _create(store, name="a")
        b = _create(store, name="b")
        c = _create(store, name="c")
        # Default: pinned first, then most recently updated.
        store.pin(c.id, True)
        store.touch(a.id)
        assert [s.id for s in store.list()] == [c.id, a.id, b.id]
        # pinned_first=False: pure updated_at DESC.
        assert [s.id for s in store.list(pinned_first=False)] == [a.id, c.id, b.id]
    finally:
        store.close()


def test_update_patch_and_timestamps(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        saved = _create(store, name="old name")
        updated = store.update(saved.id, {"name": "new name", "limit": 25})
        assert updated.name == "new name"
        assert updated.limit == 25
        assert updated.query == saved.query
        assert updated.mode == saved.mode
        assert updated.created_at == saved.created_at
        assert updated.updated_at >= saved.updated_at
    finally:
        store.close()


def test_update_missing_raises_keyerror(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        with pytest.raises(KeyError):
            store.update("ghost", {"name": "x"})
    finally:
        store.close()


def test_update_bad_patch_raises_valueerror(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        saved = _create(store)
        with pytest.raises(ValueError):
            store.update(saved.id, {"bogus_field": 1})
        with pytest.raises(ValueError):
            store.update(saved.id, {"query": "bad\x00query"})
        with pytest.raises(ValueError):
            store.update(saved.id, {"mode": "wildcard"})
    finally:
        store.close()


def test_delete_returns_bool(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        saved = _create(store)
        assert store.delete(saved.id) is True
        assert store.get(saved.id) is None
        assert store.delete(saved.id) is False
    finally:
        store.close()


def test_pin_flips_flag(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        saved = _create(store)
        assert saved.pinned is False
        pinned = store.pin(saved.id, True)
        assert pinned.pinned is True
        assert store.get(saved.id).pinned is True  # type: ignore[union-attr]
        assert store.pin(saved.id, False).pinned is False
        with pytest.raises(KeyError):
            store.pin("ghost", True)
    finally:
        store.close()


def test_touch_bumps_updated_at(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        saved = _create(store)
        touched = store.touch(saved.id)
        assert touched.updated_at >= saved.updated_at
        assert touched.created_at == saved.created_at
        with pytest.raises(KeyError):
            store.touch("ghost")
    finally:
        store.close()


def test_persists_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "saved_searches.db"
    store = SavedSearchStore(path)
    created = _create(store)
    pinned = store.pin(created.id, True)
    store.close()

    reopened = SavedSearchStore(path)
    try:
        assert reopened.get(created.id).model_dump() == pinned.model_dump()  # type: ignore[union-attr]
        assert reopened.get(created.id).pinned is True  # type: ignore[union-attr]
    finally:
        reopened.close()
