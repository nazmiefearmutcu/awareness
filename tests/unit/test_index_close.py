"""Closing the API index singleton must actually close (api/server.py).

Callers used to null ``server._State.index`` BEFORE ``server._close_index()``
— which only closes a non-None index — so the DuckDbIndex.close() (and its
``DuckDbIndex._instances`` registry cleanup) never ran and the connection
leaked for the process lifetime. ``_close_index()`` does both jobs itself:
it must be called FIRST (or alone).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from awareness.api import server
from awareness.storage.duckdb_index import DuckDbIndex


class _FakeSettings:
    """Minimal stand-in exposing just what _get_index() reads."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self.iceberg_warehouse = None

    def duckdb_path(self) -> Path:
        return self._root / "duckdb" / "metadata.duckdb"

    def staging_jsonl_dir(self) -> Path:
        return self._root / "jsonl"


def _registry_key(tmp_path: Path) -> tuple[str, str, str]:
    return (
        str((tmp_path / "duckdb" / "metadata.duckdb").resolve()),
        str((tmp_path / "jsonl").resolve()),
        "",
    )


def test_close_index_removes_instance_from_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(server, "get_settings", lambda: _FakeSettings(tmp_path))
    server._State.index = None
    key = _registry_key(tmp_path)
    try:
        idx = server._get_index()
        assert server._State.index is idx
        assert key in DuckDbIndex._instances
        assert DuckDbIndex._instances[key] is idx

        server._close_index()

        assert server._State.index is None
        assert key not in DuckDbIndex._instances
    finally:
        server._close_index()


def test_close_index_twice_is_safe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(server, "get_settings", lambda: _FakeSettings(tmp_path))
    server._State.index = None
    key = _registry_key(tmp_path)
    try:
        server._get_index()
        assert key in DuckDbIndex._instances
        server._close_index()
        server._close_index()  # idempotent: no crash on an already-clear state
        assert server._State.index is None
        assert key not in DuckDbIndex._instances
    finally:
        server._close_index()


def test_nulling_before_close_leaks_registry_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Documents WHY the order matters: the old ``_State.index = None`` then
    ``_close_index()`` pattern left the instance (and its connection) in the
    registry; the fixed pattern cleans it up."""
    monkeypatch.setattr(server, "get_settings", lambda: _FakeSettings(tmp_path))
    server._State.index = None
    key = _registry_key(tmp_path)
    try:
        server._get_index()
        assert key in DuckDbIndex._instances

        # Old buggy order: null first → _close_index() is a no-op.
        server._State.index = None
        server._close_index()
        assert key in DuckDbIndex._instances

        # Recovery: rebuilding through _get_index() re-attaches the leaked
        # instance, and the fixed close order then cleans it up.
        leaked = server._get_index()
        assert leaked is DuckDbIndex._instances[key]
        server._close_index()
        assert server._State.index is None
        assert key not in DuckDbIndex._instances
    finally:
        server._close_index()
