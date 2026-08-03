"""API tests for the /consume and /x routers (router.py / xrouter.py)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

import awareness.consume.router as consume_router
from awareness.api import server
from awareness.config import reset_settings
from awareness.consume.router import wire
from awareness.consume.xrouter import close_store

_FULL_KEYS = (
    "doc_id",
    "capture_id",
    "parent_doc_or_dup_group",
    "source_type",
    "source_name",
    "source_locator",
    "source_shard",
    "source_offset_or_record_id",
    "discovery_channel",
    "job_id",
    "batch_id",
    "ingest_version",
    "url",
    "canonical_url",
    "domain",
    "fetch_ts",
    "observed_ts",
    "published_ts",
    "last_modified",
    "content_type",
    "http_status",
    "etag",
    "title",
    "text",
    "language",
    "content_hash",
    "near_dup_hash",
    "robots_decision",
    "terms_note_if_relevant",
)


def _write_corpus(root: Path) -> None:
    """Seed captures into the tmp project's real data dir (data/jsonl/...)."""
    now = datetime.now(tz=UTC)
    for i, hours_ago in enumerate((1, 2, 3)):
        when = now - timedelta(hours=hours_ago)
        day_dir = root / "data" / "jsonl" / "captures" / when.strftime("%Y/%m/%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        ts = when.isoformat()
        rec: dict[str, object] = {k: None for k in _FULL_KEYS}
        rec.update(
            doc_id=f"doc-{i}",
            capture_id=f"cap-{i}",
            source_type="rss",
            domain="example.com",
            url=f"https://example.com/{i}",
            canonical_url=f"https://example.com/{i}",
            fetch_ts=ts,
            observed_ts=ts,
            title=f"Headline number {i}",
            text=f"Body text number {i}",
            language="en",
        )
        (day_dir / f"chunk-{i}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _make_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


@pytest.fixture()
def api_app(tmp_project: Path) -> FastAPI:
    """App with both consume routers wired against the tmp project root."""
    _write_corpus(tmp_project)
    server._State.index = None  # fresh process-wide index from tmp settings
    app = FastAPI()
    wire(app)
    return app


@pytest.fixture(autouse=True)
async def _reset_index() -> None:
    yield
    # aiosqlite keeps a worker thread per open connection; close the X store
    # so the pytest process can exit cleanly.
    await close_store()
    if server._State.index is not None:
        try:
            server._State.index.close()
        except Exception:  # noqa: S110 - best effort teardown
            pass
        server._State.index = None
    reset_settings()


def _assert_export_file(body: dict, tmp_project: Path) -> None:
    """Verify the exported artifact on disk (sync helper for the async test)."""
    out = Path(body["path"])
    assert out.exists()
    assert tmp_project.resolve() in out.resolve().parents  # inside data_dir/exports
    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 3
    assert lines[0]["output"].startswith("Headline number")


@pytest.mark.asyncio
async def test_export_returns_200_and_writes_file(api_app: FastAPI, tmp_project: Path) -> None:
    async with _make_client(api_app) as client:
        r = await client.post(
            "/consume/export",
            json={"format": "jsonl", "limit": 10, "dedup": True},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] == 3
        assert body["format"] == "jsonl"
        assert body["dedupe"] is True
        assert body["limit"] == 10
        assert len(body["files"]) == 1
        _assert_export_file(body, tmp_project)


@pytest.mark.asyncio
async def test_export_bad_input_returns_400(api_app: FastAPI) -> None:
    async with _make_client(api_app) as client:
        for payload in (
            {"limit": 0},
            {"limit": 200_000},
            {"format": "csv"},
            {"start": "2026-06-02T00:00:00+00:00", "end": "2026-06-01T00:00:00+00:00"},
        ):
            r = await client.post("/consume/export", json=payload)
            assert r.status_code == 400, f"{payload} → {r.status_code}"

        # Wrong JSON type is rejected by FastAPI's own validation (422).
        r = await client.post("/consume/export", json={"limit": "many"})
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_digest_returns_200_with_metrics(api_app: FastAPI) -> None:
    async with _make_client(api_app) as client:
        r = await client.get("/consume/digest?days=7")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total_captures"] == 3
        assert body["days"] == 7
        assert "growth_rate" in body
        assert datetime.fromisoformat(body["window_start"]).tzinfo is not None
        assert body["sample_titles"][0].startswith("Headline number")
        assert body["top_domains"][0]["term"] == "example.com"


@pytest.mark.asyncio
async def test_digest_bad_days_returns_400(api_app: FastAPI) -> None:
    async with _make_client(api_app) as client:
        for days in ("0", "366", "-1"):
            r = await client.get(f"/consume/digest?days={days}")
            assert r.status_code == 400, f"days={days} → {r.status_code}"


@pytest.mark.asyncio
async def test_digest_markdown_returns_text(api_app: FastAPI) -> None:
    async with _make_client(api_app) as client:
        r = await client.get("/consume/digest/markdown?days=7")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/markdown")
        assert "# " in r.text
        assert "## At a glance" in r.text
        assert "## Headlines" in r.text


@pytest.mark.asyncio
async def test_consume_returns_503_when_index_not_ready(
    monkeypatch: pytest.MonkeyPatch,
    tmp_project: Path,
) -> None:
    monkeypatch.setattr(consume_router, "_get_index", lambda: None)
    app = FastAPI()
    wire(app)
    async with _make_client(app) as client:
        assert (await client.get("/consume/digest")).status_code == 503
        assert (await client.get("/consume/digest/markdown")).status_code == 503
        assert (await client.post("/consume/export", json={})).status_code == 503


@pytest.mark.asyncio
async def test_x_sessions_roundtrip(api_app: FastAPI, tmp_project: Path) -> None:
    async with _make_client(api_app) as client:
        r = await client.post("/x/sessions", json={"keywords": ["ai"], "title": "AI watch"})
        assert r.status_code == 200, r.text
        created = r.json()
        assert created["status"] == "queued"
        assert created["title"] == "AI watch"
        session_id = created["session_id"]

        r = await client.get("/x/sessions")
        assert r.status_code == 200
        listed = r.json()
        assert listed["count"] == 1
        assert listed["sessions"][0]["session_id"] == session_id

        r = await client.get(f"/x/sessions/{session_id}")
        assert r.status_code == 200
        assert r.json()["session_id"] == session_id

        r = await client.get(f"/x/sessions/{session_id}/tweets?limit=10")
        assert r.status_code == 200
        assert r.json()["count"] == 0

        r = await client.get("/x/sessions/does-not-exist")
        assert r.status_code == 404

        r = await client.post("/x/sessions", json={})
        assert r.status_code == 400

    assert (tmp_project / "data" / "xscraper.sqlite").exists()
