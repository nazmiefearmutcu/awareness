"""API tests for the /topicx router (shape, status codes, edge cases).

Mounts :func:`~awareness.topicx.router.create_topicx_router` on a bare
FastAPI app (the real server wiring lives outside this subsystem's scope)
and drives it with FastAPI's TestClient.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from awareness.storage.duckdb_index import DuckDbIndex
from awareness.topicx.router import create_topicx_router

_FULL_KEYS = (
    "doc_id", "capture_id", "parent_doc_or_dup_group", "source_type",
    "source_name", "source_locator", "source_shard",
    "source_offset_or_record_id", "discovery_channel", "job_id", "batch_id",
    "ingest_version", "url", "canonical_url", "domain", "fetch_ts",
    "observed_ts", "published_ts", "last_modified", "content_type",
    "http_status", "etag", "title", "text", "language", "content_hash",
    "near_dup_hash", "robots_decision", "terms_note_if_relevant",
)

_LIFECYCLE_KEYS = {
    "term", "phase", "counts", "slope_7d", "peak_count",
    "peak_date", "first_seen", "last_seen",
}


def _write_doc(
    root: Path,
    idx: int,
    *,
    ts: datetime,
    title: str = "",
    text: str = "",
    domain: str = "example.com",
    language: str | None = None,
    parent_doc_or_dup_group: str | None = None,
) -> None:
    day = root / "captures" / f"{ts:%Y}" / f"{ts:%m}" / f"{ts:%d}"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}",
        capture_id=f"cap-{idx:04d}",
        source_type="rss",
        domain=domain,
        url=f"https://{domain}/{idx}",
        fetch_ts=ts.isoformat(),
        observed_ts=ts.isoformat(),
        title=title,
        text=text,
        language=language,
        parent_doc_or_dup_group=parent_doc_or_dup_group,
    )
    (day / f"chunk-{idx:04d}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _client(tmp_path: Path) -> TestClient:
    index = DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )
    app = FastAPI()
    app.include_router(create_topicx_router(index))
    return TestClient(app)


def _write_small_corpus(root: Path) -> None:
    # Anchor to noon UTC so +3h docs never roll across midnight (same
    # day-boundary flake fixed in test_cli_trends / test_tui_analytics).
    base = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    _write_doc(root, 1, ts=base, text="alphaflare signal")
    _write_doc(root, 2, ts=base + timedelta(hours=1), text="alphaflare update", domain="news.example")
    _write_doc(root, 3, ts=base + timedelta(hours=2), text="alphaflare recap")
    _write_doc(root, 4, ts=base + timedelta(hours=3), text="betawave mention", domain="news.example")


def test_lifecycle_returns_shape(tmp_path: Path) -> None:
    _write_small_corpus(tmp_path / "jsonl")
    with _client(tmp_path) as client:
        res = client.get("/topicx/lifecycle", params={"term": "alphaflare"})
    assert res.status_code == 200
    data = res.json()
    assert set(data) == _LIFECYCLE_KEYS
    assert data["term"] == "alphaflare"
    assert data["phase"] == "EMERGING"
    assert data["peak_count"] == 3
    assert data["first_seen"].startswith(data["peak_date"][:10])
    assert data["slope_7d"] > 0
    assert len(data["counts"]) == 31  # window_days=30 -> 31 zero-filled buckets
    assert set(data["counts"][0]) == {"ts", "count"}


def test_emerging_returns_shape(tmp_path: Path) -> None:
    _write_small_corpus(tmp_path / "jsonl")
    with _client(tmp_path) as client:
        res = client.get("/topicx/emerging", params={"window_days": 7, "limit": 20})
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    assert set(data[0]) == {"term", "count", "first_seen", "domains_covered"}
    by_term = {item["term"]: item for item in data}
    assert by_term["alphaflare"]["count"] == 3
    assert by_term["alphaflare"]["domains_covered"] == 2


def test_impact_returns_origin_domain(tmp_path: Path) -> None:
    root = tmp_path / "jsonl"
    base = datetime.now(UTC)
    _write_doc(
        root, 1, ts=base - timedelta(days=10), domain="origin.com",
        text="shared market analysis", parent_doc_or_dup_group="grp-1",
    )
    _write_doc(
        root, 2, ts=base - timedelta(days=1), domain="replica.com",
        text="shared market analysis", parent_doc_or_dup_group="grp-1",
    )
    with _client(tmp_path) as client:
        res = client.get("/topicx/impact", params={"window_days": 30, "limit": 20})
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 1
    assert set(data[0]) == {
        "domain", "impact_score", "captures", "replica_edges", "avg_lead_minutes",
    }
    assert data[0]["domain"] == "origin.com"
    assert data[0]["replica_edges"] == 1
    assert data[0]["avg_lead_minutes"] > 0


def test_dominance_returns_fractions(tmp_path: Path) -> None:
    root = tmp_path / "jsonl"
    base = datetime.now(UTC)
    for i in range(3):
        _write_doc(root, i + 1, ts=base + timedelta(hours=i), domain="news.example", text=f"dominwave rally {i}")
    _write_doc(root, 10, ts=base + timedelta(hours=3), text="dominwave note", domain="blog.example")
    with _client(tmp_path) as client:
        res = client.get("/topicx/dominance", params={"term": "dominwave", "window_days": 14})
    assert res.status_code == 200
    data = res.json()
    assert data[0]["domain"] == "news.example"
    assert sum(item["doc_fraction"] for item in data) == 1.0


def test_bad_input_is_400(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        empty_term = client.get("/topicx/lifecycle", params={"term": ""})
        too_long = client.get("/topicx/lifecycle", params={"term": "x" * 201})
        dom_empty = client.get("/topicx/dominance", params={"term": ""})
        bad_window = client.get("/topicx/lifecycle", params={"term": "alphaflare", "window_days": 0})
        bad_window_em = client.get("/topicx/emerging", params={"window_days": 400})
    assert empty_term.status_code == 400
    assert too_long.status_code == 400
    assert dom_empty.status_code == 400
    assert bad_window.status_code == 400
    assert bad_window_em.status_code == 400


def test_empty_corpus_returns_zeroed_results(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        lc = client.get("/topicx/lifecycle", params={"term": "alphaflare"})
        em = client.get("/topicx/emerging")
        im = client.get("/topicx/impact")
        dom = client.get("/topicx/dominance", params={"term": "alphaflare"})
    assert lc.status_code == 200
    assert lc.json()["phase"] == "DORMANT"
    assert lc.json()["counts"] == []
    assert em.status_code == 200 and em.json() == []
    assert im.status_code == 200 and im.json() == []
    assert dom.status_code == 200 and dom.json() == []


def test_index_not_ready_is_503(tmp_path: Path) -> None:
    index = MagicMock()
    index.health_snapshot.return_value = {"ready": False}
    app = FastAPI()
    app.include_router(create_topicx_router(index))
    with TestClient(app) as client:
        lc = client.get("/topicx/lifecycle", params={"term": "alphaflare"})
        em = client.get("/topicx/emerging")
        im = client.get("/topicx/impact")
        dom = client.get("/topicx/dominance", params={"term": "alphaflare"})
    for res in (lc, em, im, dom):
        assert res.status_code == 503
        assert "not ready" in res.json()["detail"]


def test_index_ready_attribute_surface(tmp_path: Path) -> None:
    index = MagicMock()
    del index.health_snapshot
    index.index_ready = False
    app = FastAPI()
    app.include_router(create_topicx_router(index))
    with TestClient(app) as client:
        res = client.get("/topicx/lifecycle", params={"term": "alphaflare"})
    assert res.status_code == 503
