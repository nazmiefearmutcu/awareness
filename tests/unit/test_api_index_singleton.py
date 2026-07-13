"""The API shares ONE process-wide DuckDbIndex instead of building one per
request (so FTS is built once and concurrent searches don't collide on
DuckDB's single-writer lock during a rebuild)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

import awareness.api.server as server
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


def test_get_index_returns_same_instance(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(server, "get_settings", lambda: _FakeSettings(tmp_path))
    server._State.index = None
    try:
        a = server._get_index()
        b = server._get_index()
        assert isinstance(a, DuckDbIndex)
        assert a is b                      # reused, not rebuilt per call
    finally:
        server._State.index = None


def test_get_index_is_threadsafe_single_instance(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(server, "get_settings", lambda: _FakeSettings(tmp_path))
    server._State.index = None
    try:
        seen: list[int] = []
        barrier = threading.Barrier(8)

        def worker() -> None:
            barrier.wait()                 # maximize the race on first creation
            seen.append(id(server._get_index()))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(set(seen)) == 1, "double-checked locking must yield exactly one instance"
    finally:
        server._State.index = None


def test_close_index_clears_singleton(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(server, "get_settings", lambda: _FakeSettings(tmp_path))
    server._State.index = None
    try:
        idx = server._get_index()
        assert server._State.index is idx
        server._close_index()
        assert server._State.index is None
        # Next call rebuilds a fresh instance
        again = server._get_index()
        assert again is not idx
        assert server._State.index is again
    finally:
        server._close_index()


def test_settings_put_config_closes_index_when_applied(monkeypatch, tmp_path: Path) -> None:
    """PUT /settings/config must drop the DuckDbIndex singleton after apply."""
    app = server.create_app()
    monkeypatch.setattr(server, "get_settings", lambda: _FakeSettings(tmp_path))
    server._State.index = None
    try:
        idx = server._get_index()
        assert server._State.index is idx

        def fake_apply(values: dict) -> dict:
            return {"ok": True, "applied": {"search_max_results": 50}, "errors": {}, "values": {}}

        monkeypatch.setattr(
            "awareness.config.persist.apply_updates",
            fake_apply,
        )
        for route in app.routes:
            if getattr(route, "path", None) == "/settings/config" and "PUT" in getattr(route, "methods", set()):
                result = route.endpoint({"values": {"search_max_results": 50}})
                break
        else:
            raise AssertionError("PUT /settings/config route not found")

        assert result["ok"] is True
        assert server._State.index is None
    finally:
        server._close_index()


def test_settings_put_config_skips_close_when_nothing_applied(monkeypatch, tmp_path: Path) -> None:
    app = server.create_app()
    monkeypatch.setattr(server, "get_settings", lambda: _FakeSettings(tmp_path))
    server._State.index = None
    try:
        idx = server._get_index()

        def fake_apply(values: dict) -> dict:
            return {"ok": False, "applied": {}, "errors": {"nope": "unknown key"}, "values": {}}

        monkeypatch.setattr(
            "awareness.config.persist.apply_updates",
            fake_apply,
        )
        for route in app.routes:
            if getattr(route, "path", None) == "/settings/config" and "PUT" in getattr(route, "methods", set()):
                route.endpoint({"nope": 1})
                break
        else:
            raise AssertionError("PUT /settings/config route not found")

        assert server._State.index is idx  # unchanged
    finally:
        server._close_index()


def test_lifespan_source_closes_shared_http_clients() -> None:
    """API lifespan shutdown must aclose process-wide pooled httpx clients."""
    import inspect

    src = inspect.getsource(server.create_app)
    assert "aclose_shared_async_clients" in src
    assert "from awareness.util.http import aclose_shared_async_clients" in src


@pytest.mark.asyncio
async def test_lifespan_aclose_shared_http_clients(monkeypatch, tmp_path: Path) -> None:
    """Exiting the app lifespan drains the shared AsyncClient pool."""
    from awareness.config import get_settings
    from awareness.util.http import (
        get_shared_async_client,
        reset_shared_async_clients,
        shared_async_client_pool_size,
    )

    # Isolate StateDB into tmp so we do not touch the workspace sqlite file.
    real = get_settings()
    monkeypatch.setattr(
        real,
        "state_db_url",
        f"sqlite:///{tmp_path / 'lifespan-state.sqlite'}",
    )
    monkeypatch.setattr(real, "reaper_enabled", False)
    monkeypatch.setattr(server, "get_settings", lambda: real)

    app = server.create_app()
    reset_shared_async_clients()
    server._State.index = None
    try:
        async with app.router.lifespan_context(app):
            client = await get_shared_async_client(timeout=5.0, follow_redirects=True)
            assert client is not None
            assert shared_async_client_pool_size() >= 1
        # After lifespan finally: pool drained.
        assert shared_async_client_pool_size() == 0
    finally:
        server._close_index()
        reset_shared_async_clients()
