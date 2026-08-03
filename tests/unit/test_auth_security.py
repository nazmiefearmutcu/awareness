"""API security: bearer auth, CSRF posture, input validation, run guards."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from awareness.api import server
from awareness.config import reset_settings
from awareness.schemas.jobs import JobStatus


def _make_client(app: server.FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


class _FakeState:
    def __init__(self, jobs: dict | None = None) -> None:
        self._jobs = jobs or {}

    def get_job(self, job_id: str):
        return self._jobs.get(job_id)

    def list_jobs(self, limit: int) -> list:
        return []

    def get_tail(self) -> dict:
        return {"running": False}


class _FakePlanner:
    def status(self, job_id: str) -> dict:
        return {"job_id": job_id, "status": "pending"}

    def submit_backfill(self, req) -> str:
        return "job-1"


class _FakeTail:
    running = False

    async def start(self, **kwargs) -> None:
        return None


class _FakeEngine:
    def __init__(self, state, planner) -> None:
        self._release = asyncio.Event()

    async def run_job(self, job_id: str) -> None:
        await self._release.wait()

    async def aclose(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.delenv("AW_API_KEY", raising=False)
    monkeypatch.setenv("AW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("AW_CONFIG_FILE", raising=False)
    reset_settings()
    _reset_state()
    yield
    _reset_state()
    reset_settings()


def _reset_state() -> None:
    for t in list(server._State.background_tasks):
        t.cancel()
    server._State.state = None
    server._State.planner = None
    server._State.tail = None
    server._State.index = None
    server._State.active_job_runs.clear()
    server._State.background_tasks.clear()


def _boom_index() -> None:
    raise RuntimeError("index boom")


# ── C-03: optional bearer auth ───────────────────────────────────────────────
@pytest.mark.asyncio
async def test_auth_required_when_key_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AW_API_KEY", "sekret-42")
    reset_settings()
    monkeypatch.setattr(server, "_get_index", _boom_index)
    app = server.create_app()
    async with _make_client(app) as client:
        assert (await client.post("/backfill", json={"start": "2026-01-01"})).status_code == 401
        assert (await client.get("/jobs")).status_code == 401
        assert (
            await client.get("/jobs", headers={"Authorization": "Bearer wrong"})
        ).status_code == 401
        r = await client.get("/jobs", headers={"Authorization": "Bearer sekret-42"})
        assert r.status_code == 500  # auth passed; endpoint not initialized
        assert (await client.get("/healthz")).status_code == 200


@pytest.mark.asyncio
async def test_no_key_keeps_localhost_trust() -> None:
    app = server.create_app()
    async with _make_client(app) as client:
        r = await client.get("/jobs")
        assert r.status_code == 500  # no auth gate; just uninitialized state


# ── H-19: CSRF posture ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_csrf_rejects_non_json_body() -> None:
    app = server.create_app()
    async with _make_client(app) as client:
        r = await client.post(
            "/tail/start",
            content='{"match": []}',
            headers={"Content-Type": "text/plain"},
        )
        assert r.status_code == 415
        r = await client.post("/tail/start", json={})
        assert r.status_code == 500  # json body accepted; uninitialized tail


@pytest.mark.asyncio
async def test_csrf_rejects_cross_origin() -> None:
    app = server.create_app()
    async with _make_client(app) as client:
        r = await client.post("/tail/stop", headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        r = await client.post("/tail/stop", headers={"Origin": "http://testserver"})
        assert r.status_code == 500  # same-origin accepted


# ── H-20/C-09: 400s not 500s ─────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_backfill_bad_source_is_400() -> None:
    server._State.planner = _FakePlanner()
    app = server.create_app()
    async with _make_client(app) as client:
        r = await client.post(
            "/backfill", json={"start": "2026-01-01", "sources": ["bogus_source"]}
        )
        assert r.status_code == 400
        r = await client.post("/backfill", json={"start": "2026-01-01", "end_str": "not-a-date"})
        assert r.status_code == 400


# ── H-03: jobs limit bounds ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_jobs_limit_clamped() -> None:
    server._State.state = _FakeState()
    app = server.create_app()
    async with _make_client(app) as client:
        assert (await client.get("/jobs?limit=0")).status_code == 422
        assert (await client.get("/jobs?limit=501")).status_code == 422
        assert (await client.get("/jobs?limit=20")).status_code == 200
        assert (await client.get("/jobs?limit=abc")).status_code == 422


# ── M-08: gdelt_max_urls bounds ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_tail_start_gdelt_max_urls_clamped() -> None:
    server._State.state = _FakeState()
    server._State.tail = _FakeTail()
    app = server.create_app()
    async with _make_client(app) as client:
        assert (await client.post("/tail/start", json={"gdelt_max_urls": 999_999})).status_code == 400
        assert (await client.post("/tail/start", json={"gdelt_max_urls": -5})).status_code == 400
        assert (await client.post("/tail/start", json={"gdelt_max_urls": 0})).status_code == 200
        assert (await client.post("/tail/start", json={"gdelt_max_urls": 100_000})).status_code == 200


# ── H-22: per-job in-flight guard ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_backfill_run_conflict_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _FakeEngine(None, None)
    monkeypatch.setattr(server, "WorkerEngine", lambda state, planner: engine)
    running = SimpleNamespace(status=JobStatus.RUNNING)
    server._State.state = _FakeState(jobs={"job-1": running})
    server._State.planner = _FakePlanner()
    app = server.create_app()
    async with _make_client(app) as client:
        assert (await client.post("/backfill/job-1/run")).status_code == 409
        server._State.active_job_runs.add("job-2")
        assert (await client.post("/backfill/job-2/run")).status_code == 409
        assert (await client.post("/backfill/job-3/run")).status_code == 200
        assert "job-3" in server._State.active_job_runs
        assert (await client.post("/backfill/job-3/run")).status_code == 409
        engine._release.set()
    await asyncio.gather(*list(server._State.background_tasks), return_exceptions=True)
    assert "job-3" not in server._State.active_job_runs


# ── H-21: /healthz leaks nothing ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_healthz_does_not_leak_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_get_index", _boom_index)
    app = server.create_app()
    async with _make_client(app) as client:
        payload = (await client.get("/healthz")).json()
        assert payload["ok"] is True
        assert payload["index_ready"] is False
        assert "state_db" not in payload
        assert "data_dir" not in payload
