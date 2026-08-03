"""API tests for the /sentiment router (shape, status codes, edge cases).

Mounts :func:`~awareness.sentiment.router.create_sentiment_router` on a bare
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

from awareness.sentiment.router import create_sentiment_router
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

_TERM_KEYS = {
    "term",
    "buckets",
    "total_docs",
    "pos_docs",
    "neg_docs",
    "sentiment_ratio",
    "volatility",
    "last_7d_trend",
}
_HEAT_KEYS = {
    "total_docs",
    "pos_docs",
    "neg_docs",
    "sentiment_ratio",
    "volatility",
    "last_7d_trend",
}
_BUCKET_KEYS = {"ts", "doc_count", "pos_count", "neg_count", "avg_score"}


def _write_doc(
    root: Path,
    idx: int,
    *,
    ts: datetime,
    title: str = "",
    text: str = "",
    domain: str = "example.com",
    language: str | None = None,
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
    )
    (day / f"chunk-{idx:04d}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _client(tmp_path: Path) -> TestClient:
    index = DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )
    app = FastAPI()
    app.include_router(create_sentiment_router(index))
    return TestClient(app)


def test_term_endpoint_returns_result_shape(tmp_path: Path) -> None:
    root = tmp_path / "jsonl"
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    _write_doc(root, 1, ts=base, title="Bitcoin rally continues", text="strong gains")
    _write_doc(root, 2, ts=base + timedelta(hours=3), title="Bitcoin crash", text="panic")
    _write_doc(root, 3, ts=base + timedelta(days=1), title="Bitcoin surges", text="record high")

    with _client(tmp_path) as client:
        res = client.get(
            "/sentiment/term", params={"term": "bitcoin", "window_days": 3}
        )
    assert res.status_code == 200
    data = res.json()
    assert set(data) == _TERM_KEYS
    assert data["term"] == "bitcoin"
    assert len(data["buckets"]) == 4
    assert set(data["buckets"][0]) == _BUCKET_KEYS
    assert data["buckets"][0]["ts"].startswith("2026-05-30")
    assert data["total_docs"] == 3
    assert data["pos_docs"] == 2
    assert data["neg_docs"] == 1


def test_heat_endpoint_returns_heat_shape(tmp_path: Path) -> None:
    root = tmp_path / "jsonl"
    base = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    _write_doc(root, 1, ts=base, title="Bitcoin slump", text="losses")
    _write_doc(root, 2, ts=base + timedelta(days=1), title="Bitcoin crash", text="panic")
    _write_doc(root, 3, ts=base + timedelta(days=2), title="Bitcoin rally", text="gains")

    with _client(tmp_path) as client:
        res = client.get(
            "/sentiment/heat", params={"term": "bitcoin", "window_days": 2}
        )
    assert res.status_code == 200
    data = res.json()
    assert set(data) == _HEAT_KEYS
    assert data["total_docs"] == 3
    assert data["pos_docs"] == 1
    assert data["neg_docs"] == 2
    assert data["sentiment_ratio"] < 0
    assert data["volatility"] > 0
    assert data["last_7d_trend"] > 0


def test_bad_inputs_are_400(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        empty_term = client.get("/sentiment/term", params={"term": ""})
        long_term = client.get("/sentiment/term", params={"term": "x" * 201})
        bad_granularity = client.get(
            "/sentiment/term", params={"term": "bitcoin", "granularity": "hourly"}
        )
        bad_window = client.get("/sentiment/term", params={"term": "bitcoin", "window_days": 0})
        heat_empty = client.get("/sentiment/heat", params={"term": ""})
        heat_bad_window = client.get("/sentiment/heat", params={"term": "bitcoin", "window_days": 9999})
    assert empty_term.status_code == 400
    assert long_term.status_code == 400
    assert bad_granularity.status_code == 400
    assert bad_window.status_code == 400
    assert heat_empty.status_code == 400
    assert heat_bad_window.status_code == 400


def test_empty_corpus_returns_zeroed_result(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        term = client.get("/sentiment/term", params={"term": "bitcoin"})
        heat = client.get("/sentiment/heat", params={"term": "bitcoin"})
    assert term.status_code == 200
    assert term.json()["buckets"] == []
    assert term.json()["total_docs"] == 0
    assert heat.status_code == 200
    assert heat.json()["total_docs"] == 0


def test_index_not_ready_is_503(tmp_path: Path) -> None:
    index = MagicMock()
    index.health_snapshot.return_value = {"ready": False}
    app = FastAPI()
    app.include_router(create_sentiment_router(index))
    with TestClient(app) as client:
        term = client.get("/sentiment/term", params={"term": "bitcoin"})
        heat = client.get("/sentiment/heat", params={"term": "bitcoin"})
    assert term.status_code == 503
    assert "not ready" in term.json()["detail"]
    assert heat.status_code == 503


def test_index_ready_attribute_surface(tmp_path: Path) -> None:
    index = MagicMock()
    del index.health_snapshot
    index.index_ready = False
    app = FastAPI()
    app.include_router(create_sentiment_router(index))
    with TestClient(app) as client:
        res = client.get("/sentiment/heat", params={"term": "bitcoin"})
    assert res.status_code == 503


def test_index_as_callable_is_resolved_lazily(tmp_path: Path) -> None:
    index = DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )
    app = FastAPI()
    app.include_router(create_sentiment_router(lambda: index))
    with TestClient(app) as client:
        res = client.get("/sentiment/heat", params={"term": "bitcoin"})
    assert res.status_code == 200
