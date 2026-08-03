"""Regression tests for adversarial-review fixes F-1..F-3 (server.py).

F-1: non-loopback bind without AW_API_KEY refuses (SystemExit), in run()
     and in the lifespan startup guard.
F-2: mutating requests on CSRF-protected prefixes require a non-empty
     application/json body (415 / 422); Origin must match the CONFIGURED
     host (127.0.0.1:8085), not the spoofable Host header.
F-3: /healthz (unauthenticated) never leaks db_path / jsonl_dir.
"""

from __future__ import annotations

import httpx
import pytest

from awareness.api import server
from awareness.config import reset_settings
from awareness.storage.duckdb_index import DuckDbIndex


def _make_client(app: server.FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


class _FakeState:
    def get_tail(self) -> dict:
        return {"running": False}

    def list_jobs(self, limit: int) -> list:
        return []


class _FakeTail:
    running = False

    async def start(self, **kwargs) -> None:
        return None

    async def stop(self, **kwargs) -> None:
        return None


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.delenv("AW_API_KEY", raising=False)
    monkeypatch.delenv("AW_API_HOST", raising=False)
    monkeypatch.delenv("AW_API_PORT", raising=False)
    monkeypatch.setenv("AW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("AW_CONFIG_FILE", raising=False)
    reset_settings()
    yield
    reset_settings()


# ── F-1: refuse non-loopback bind without a key ─────────────────────────────
def test_run_refuses_non_loopback_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AW_API_HOST", "0.0.0.0")  # noqa: S104
    monkeypatch.delenv("AW_API_KEY", raising=False)
    reset_settings()

    called: dict[str, object] = {}

    def fake_uvicorn_run(app_str: str, **kwargs: object) -> None:
        called["app"] = app_str

    monkeypatch.setattr("uvicorn.run", fake_uvicorn_run)
    with pytest.raises(SystemExit) as excinfo:
        server.run()
    assert excinfo.value.code == 1
    assert "app" not in called  # never reached uvicorn


def test_run_refuses_non_loopback_without_key_lifespan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt-and-suspenders: the lifespan startup guard also refuses."""
    monkeypatch.setenv("AW_API_HOST", "0.0.0.0")  # noqa: S104
    monkeypatch.delenv("AW_API_KEY", raising=False)
    reset_settings()

    with pytest.raises(SystemExit) as excinfo:
        server._guard_non_loopback_without_key()
    assert excinfo.value.code == 1


@pytest.mark.asyncio
async def test_lifespan_startup_refuses_non_loopback_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AW_API_HOST", "0.0.0.0")  # noqa: S104
    monkeypatch.delenv("AW_API_KEY", raising=False)
    reset_settings()

    app = server.create_app()
    with pytest.raises(SystemExit):
        async with app.router.lifespan_context(app):
            pass  # pragma: no cover - guard exits before yielding


def test_run_proceeds_with_key_on_non_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AW_API_HOST", "0.0.0.0")  # noqa: S104
    monkeypatch.setenv("AW_API_KEY", "sekret-42")
    reset_settings()

    called: dict[str, object] = {}

    def fake_uvicorn_run(app_str: str, **kwargs: object) -> None:
        called["app"] = app_str
        called["kwargs"] = kwargs

    monkeypatch.setattr("uvicorn.run", fake_uvicorn_run)
    server.run()  # must NOT raise SystemExit
    assert called["app"] == "awareness.api.server:create_app"
    assert called["kwargs"]["host"] == "0.0.0.0"  # noqa: S104
    assert called["kwargs"]["port"] == 8085


def test_run_loopback_without_key_proceeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AW_API_KEY", raising=False)
    reset_settings()

    called: dict[str, object] = {}

    def fake_uvicorn_run(app_str: str, **kwargs: object) -> None:
        called["app"] = app_str

    monkeypatch.setattr("uvicorn.run", fake_uvicorn_run)
    server.run()  # default 127.0.0.1 without key is fine
    assert called["app"] == "awareness.api.server:create_app"


# ── F-2: empty-body / non-JSON mutating requests blocked ────────────────────
@pytest.mark.asyncio
async def test_empty_body_post_tail_stop_422() -> None:
    app = server.create_app()
    async with _make_client(app) as client:
        # No body at all -> no content-type -> 415.
        assert (await client.post("/tail/stop")).status_code == 415
        # Content-Type json but empty body -> 422.
        assert (
            await client.post(
                "/tail/stop",
                content=b"",
                headers={"Content-Type": "application/json"},
            )
        ).status_code == 422
        assert (
            await client.post("/tail/stop", content=b"   ", headers={"Content-Type": "application/json"})
        ).status_code == 422


@pytest.mark.asyncio
async def test_empty_body_put_tail_seeds_422() -> None:
    app = server.create_app()
    async with _make_client(app) as client:
        assert (
            await client.put(
                "/settings/tail-seeds",
                content=b"",
                headers={"Content-Type": "application/json"},
            )
        ).status_code == 422
        # No content-type at all -> 415 before the emptiness check.
        assert (await client.put("/settings/tail-seeds")).status_code == 415


@pytest.mark.asyncio
async def test_empty_body_post_tail_start_422() -> None:
    app = server.create_app()
    async with _make_client(app) as client:
        assert (await client.post("/tail/start")).status_code == 415
        assert (
            await client.post(
                "/tail/start",
                content=b"",
                headers={"Content-Type": "application/json"},
            )
        ).status_code == 422


@pytest.mark.asyncio
async def test_json_body_mutating_requests_pass_gate() -> None:
    server._State.state = _FakeState()
    server._State.tail = _FakeTail()
    app = server.create_app()
    async with _make_client(app) as client:
        r = await client.post("/tail/stop", json={})
        assert r.status_code == 200
        r = await client.post("/tail/start", json={})
        assert r.status_code == 200
        r = await client.put("/settings/tail-seeds", json={"feeds": []})
        assert r.status_code == 200


# ── F-2: Origin must match the CONFIGURED host, not the Host header ─────────
@pytest.mark.asyncio
async def test_origin_matching_configured_host_allowed() -> None:
    server._State.state = _FakeState()
    server._State.tail = _FakeTail()
    app = server.create_app()
    async with _make_client(app) as client:
        r = await client.post(
            "/tail/stop",
            json={},
            headers={"Origin": "http://127.0.0.1:8085"},
        )
        assert r.status_code == 200
        # Host-header spoofing cannot extend trust: Origin still matches the
        # configured host even when the Host header claims something else.
        r = await client.post(
            "/tail/stop",
            json={},
            headers={"Host": "evil.example", "Origin": "http://127.0.0.1:8085"},
        )
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_origin_evil_host_403() -> None:
    server._State.state = _FakeState()
    server._State.tail = _FakeTail()
    app = server.create_app()
    async with _make_client(app) as client:
        r = await client.post(
            "/tail/stop",
            json={},
            headers={"Origin": "https://evil.example"},
        )
        assert r.status_code == 403
        # Spoofed Host matching the Origin must NOT be trusted.
        r = await client.post(
            "/tail/stop",
            json={},
            headers={"Host": "127.0.0.1:8085", "Origin": "https://evil.example"},
        )
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_origin_absent_permissive() -> None:
    server._State.state = _FakeState()
    server._State.tail = _FakeTail()
    app = server.create_app()
    async with _make_client(app) as client:
        assert (await client.post("/tail/stop", json={})).status_code == 200


# ── F-3: /healthz leaks no filesystem paths ─────────────────────────────────
@pytest.mark.asyncio
async def test_healthz_contains_no_paths(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:

    idx = DuckDbIndex(
        db_path=tmp_path / "meta.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )
    server._State.index = idx
    try:
        app = server.create_app()
        async with _make_client(app) as client:
            r = await client.get("/healthz")
            assert r.status_code == 200
            payload = r.json()
            assert payload["index"] is not None
            assert "db_path" not in payload["index"]
            assert "jsonl_dir" not in payload["index"]
            assert "ready" in payload["index"]
            body = r.text
            assert str(tmp_path) not in body
            assert "/Users" not in body
            assert "/tmp" not in body  # noqa: S108
    finally:
        server._close_index()
