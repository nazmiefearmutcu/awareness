"""API tests for the /source-intel router (200 / 400 / 404 / 503 handling)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from awareness.api import server
from awareness.sourceintel.engine import SourceIntelEngine
from awareness.sourceintel.router import get_engine, router
from awareness.storage.duckdb_index import DuckDbIndex

_FULL_KEYS = (
    "doc_id", "capture_id", "parent_doc_or_dup_group", "source_type",
    "source_name", "source_locator", "source_shard",
    "source_offset_or_record_id", "discovery_channel", "job_id", "batch_id",
    "ingest_version", "url", "canonical_url", "domain", "fetch_ts",
    "observed_ts", "published_ts", "last_modified", "content_type",
    "http_status", "etag", "title", "text", "language", "content_hash",
    "near_dup_hash", "robots_decision", "terms_note_if_relevant",
)


def _write_doc(
    root: Path,
    idx: int,
    *,
    domain: str,
    group: str | None = None,
    text: str = "body text here",
    days_ago: int = 1,
) -> None:
    day = root / "captures" / "2026" / "06" / "01"
    day.mkdir(parents=True, exist_ok=True)
    ts = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}",
        capture_id=f"cap-{idx}",
        parent_doc_or_dup_group=group,
        source_type="rss",
        domain=domain,
        url=f"https://{domain}/{idx}",
        canonical_url=f"https://{domain}/{idx}",
        fetch_ts=ts,
        observed_ts=ts,
        title="t",
        text=text,
        language="en",
        content_hash=f"hash-{idx}",
    )
    (day / f"chunk-{idx}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _build_corpus(root: Path) -> None:
    _write_doc(root, 0, domain="origin.com", group="grp-1", text="original long report", days_ago=10)
    _write_doc(root, 1, domain="mirror.com", group="grp-1", text="copy of original", days_ago=1)
    _write_doc(root, 2, domain="writer.com", group=None, text="unique content piece", days_ago=2)
    _write_doc(root, 3, domain="writer.com", group=None, text="another unique piece", days_ago=3)


@pytest.fixture()
async def client(tmp_path: Path):
    _build_corpus(tmp_path / "jsonl")
    idx = DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_engine] = lambda: SourceIntelEngine(idx)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as c:
        yield c
    idx.close()


async def test_domains_rank_ok(client: httpx.AsyncClient) -> None:
    resp = await client.get("/source-intel/domains")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 3
    assert {"domain", "score", "captures", "replication_ratio", "avg_length", "velocity"} <= set(rows[0])
    assert rows[0]["domain"] == "writer.com"
    mirror = next(r for r in rows if r["domain"] == "mirror.com")
    assert mirror["replication_ratio"] == 1.0


async def test_domains_respects_limit(client: httpx.AsyncClient) -> None:
    resp = await client.get("/source-intel/domains?limit=2")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


async def test_domains_bad_limit_422(client: httpx.AsyncClient) -> None:
    assert (await client.get("/source-intel/domains?limit=0")).status_code == 422
    assert (await client.get("/source-intel/domains?limit=1001")).status_code == 422
    assert (await client.get("/source-intel/domains?limit=abc")).status_code == 422


async def test_domains_date_window(client: httpx.AsyncClient) -> None:
    start = (datetime.now(UTC) - timedelta(days=4)).isoformat()
    end = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    resp = await client.get("/source-intel/domains", params={"start": start, "end": end})
    assert resp.status_code == 200
    assert {r["domain"] for r in resp.json()} == {"writer.com"}


async def test_domain_profile_ok(client: httpx.AsyncClient) -> None:
    resp = await client.get("/source-intel/domain/origin.com")
    assert resp.status_code == 200
    body = resp.json()
    assert body["domain"] == "origin.com"
    assert body["total_captures"] == 1
    assert body["first_seen"] is not None
    assert body["last_seen"] is not None
    assert isinstance(body["languages"], list)
    assert isinstance(body["top_terms"], list)
    assert isinstance(body["source_types"], list)


async def test_domain_profile_normalizes_www(client: httpx.AsyncClient) -> None:
    resp = await client.get("/source-intel/domain/www.origin.com")
    assert resp.status_code == 200
    assert resp.json()["domain"] == "origin.com"


async def test_domain_unknown_404(client: httpx.AsyncClient) -> None:
    resp = await client.get("/source-intel/domain/ghost.example")
    assert resp.status_code == 404


async def test_domain_invalid_400(client: httpx.AsyncClient) -> None:
    resp = await client.get("/source-intel/domain/%20%20")
    assert resp.status_code == 400


async def test_replication_ok(client: httpx.AsyncClient) -> None:
    resp = await client.get("/source-intel/replication")
    assert resp.status_code == 200
    edges = resp.json()
    assert len(edges) == 1
    assert edges[0]["origin"] == "origin.com"
    assert edges[0]["replica"] == "mirror.com"
    assert edges[0]["count"] == 1
    assert len(edges[0]["sample_urls"]) == 2


async def test_replication_bad_window_422(client: httpx.AsyncClient) -> None:
    assert (await client.get("/source-intel/replication?window_days=0")).status_code == 422
    assert (await client.get("/source-intel/replication?window_days=5000")).status_code == 422


async def test_replicators_ok(client: httpx.AsyncClient) -> None:
    resp = await client.get("/source-intel/replicators")
    assert resp.status_code == 200
    rows = resp.json()
    assert rows[0]["domain"] == "mirror.com"
    assert rows[0]["score"] == 1.0


async def test_freshness_ok(client: httpx.AsyncClient) -> None:
    resp = await client.get("/source-intel/freshness")
    assert resp.status_code == 200
    rows = resp.json()
    assert {"domain", "last_seen", "days_since_last", "captures_7d", "captures_30d"} <= set(rows[0])
    assert rows[0]["domain"] == "mirror.com"  # most recent first


async def test_503_when_index_unavailable(tmp_path: Path, monkeypatch) -> None:
    def _boom() -> SourceIntelEngine:
        raise RuntimeError("index down")

    monkeypatch.setattr(server, "_get_index", _boom)
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as c:
        assert (await c.get("/source-intel/domains")).status_code == 503
        assert (await c.get("/source-intel/replication")).status_code == 503
        assert (await c.get("/source-intel/freshness")).status_code == 503


async def test_503_when_query_fails(tmp_path: Path) -> None:
    app = FastAPI()
    app.include_router(router)

    class _BrokenEngine(SourceIntelEngine):
        def domain_rank(self, **kwargs):
            raise RuntimeError("query boom")

    idx = DuckDbIndex(
        db_path=tmp_path / "duckdb" / "meta.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )
    app.dependency_overrides[get_engine] = lambda: _BrokenEngine(idx)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as c:
        resp = await c.get("/source-intel/domains")
        assert resp.status_code == 503
    idx.close()


