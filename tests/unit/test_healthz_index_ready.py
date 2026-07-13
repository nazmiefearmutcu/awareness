"""GET /healthz reports search index readiness for operators and load balancers."""

from __future__ import annotations

from pathlib import Path

import awareness.api.server as server
from awareness.storage.duckdb_index import DuckDbIndex


class _FakeSettings:
    def __init__(self, root: Path) -> None:
        self.data_dir = root
        self.iceberg_warehouse = None
        self._root = root

    def duckdb_path(self) -> Path:
        return self._root / "duckdb" / "metadata.duckdb"

    def staging_jsonl_dir(self) -> Path:
        return self._root / "jsonl"


def _healthz_endpoint(app):
    for route in app.routes:
        if getattr(route, "path", None) == "/healthz" and "GET" in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError("/healthz route not found")


def test_healthz_includes_index_ready(monkeypatch, tmp_path: Path) -> None:
    # create_app needs real Settings for lifespan wiring; swap get_settings after.
    app = server.create_app()
    monkeypatch.setattr(server, "get_settings", lambda: _FakeSettings(tmp_path))
    server._State.index = None
    server._State.state = None
    try:
        payload = _healthz_endpoint(app)()
        assert payload["ok"] is True
        assert "index_ready" in payload
        assert isinstance(payload["index_ready"], bool)
        assert payload["index_ready"] is True
        assert isinstance(payload["index"], dict)
        assert payload["index"]["ready"] is True
        assert "captures" in payload["index"]
        assert "fts_extension" in payload["index"]
        assert "fts_built" in payload["index"]
        assert "version" in payload
    finally:
        server._close_index()


def test_healthz_index_ready_false_on_probe_error(monkeypatch, tmp_path: Path) -> None:
    app = server.create_app()
    monkeypatch.setattr(server, "get_settings", lambda: _FakeSettings(tmp_path))
    server._State.index = None

    def boom() -> DuckDbIndex:
        raise RuntimeError("index boom")

    monkeypatch.setattr(server, "_get_index", boom)
    try:
        payload = _healthz_endpoint(app)()
        assert payload["ok"] is True  # liveness still passes
        assert payload["index_ready"] is False
        assert payload["index"]["ready"] is False
        assert "boom" in payload["index"]["error"]
    finally:
        server._State.index = None


def test_duckdb_health_snapshot_ready_empty_corpus(tmp_path: Path) -> None:
    idx = DuckDbIndex(
        db_path=tmp_path / "meta.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )
    try:
        snap = idx.health_snapshot()
        assert snap["ready"] is True
        assert snap["captures"] == 0
        assert isinstance(snap["fts_extension"], bool)
        assert snap["fts_built"] is False
    finally:
        idx.close()
