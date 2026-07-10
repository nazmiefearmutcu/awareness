"""The API shares ONE process-wide DuckDbIndex instead of building one per
request (so FTS is built once and concurrent searches don't collide on
DuckDB's single-writer lock during a rebuild)."""

from __future__ import annotations

import threading
from pathlib import Path

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
