"""API tests for the /corpus router (shape, status codes, edge cases).

Mounts :func:`~awareness.corpusx.router.create_corpusx_router` on a bare
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

from awareness.corpusx.router import create_corpusx_router
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

_QUALITY_KEYS = {
    "total_captures", "empty_text", "duplicate_ratio", "near_duplicate_ratio",
    "avg_length", "languages", "top_domains", "dedup_group_count",
    "capture_rate_per_day",
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
    content_hash: str | None = None,
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
        content_hash=content_hash,
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
    app.include_router(create_corpusx_router(index))
    return TestClient(app)


def _write_small_corpus(root: Path) -> None:
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    _write_doc(root, 1, ts=base, domain="news.example", text="bitcoin surges", language="en")
    _write_doc(
        root, 2, ts=base + timedelta(hours=1), domain="news.example",
        text="bitcoin dips", language="en",
    )
    _write_doc(root, 3, ts=base + timedelta(hours=2), domain="blog.example", text="ethereum rises", language="tr")
    _write_doc(root, 4, ts=base + timedelta(hours=3), domain="blog.example", text="ethereum gains", language="tr")


def test_topic_matrix_returns_rectangular_matrix(tmp_path: Path) -> None:
    _write_small_corpus(tmp_path / "jsonl")
    with _client(tmp_path) as client:
        res = client.get(
            "/corpus/topic-matrix",
            params={"terms": "bitcoin,ethereum", "window_days": 30, "top_domains": 3},
        )
    assert res.status_code == 200
    data = res.json()
    assert data["terms"] == ["bitcoin", "ethereum"]
    assert data["domains"] == ["blog.example", "news.example"]
    assert len(data["cells"]) == 4
    cell = {(c["term"], c["domain"]): c["count"] for c in data["cells"]}
    assert cell == {
        ("bitcoin", "blog.example"): 0,
        ("bitcoin", "news.example"): 2,
        ("ethereum", "blog.example"): 2,
        ("ethereum", "news.example"): 0,
    }
    assert data["totals"] == {
        "terms": {"bitcoin": 2, "ethereum": 2},
        "domains": {"blog.example": 2, "news.example": 2},
    }


def test_empty_terms_is_400(tmp_path: Path) -> None:
    _write_small_corpus(tmp_path / "jsonl")
    with _client(tmp_path) as client:
        missing = client.get("/corpus/topic-matrix")
        empty = client.get("/corpus/topic-matrix", params={"terms": ""})
        blank = client.get("/corpus/topic-matrix", params={"terms": ",,,"})
    assert missing.status_code == 400
    assert empty.status_code == 400
    assert blank.status_code == 400


def test_too_many_terms_is_400(tmp_path: Path) -> None:
    _write_small_corpus(tmp_path / "jsonl")
    terms = ",".join(f"t{i}" for i in range(21))
    with _client(tmp_path) as client:
        res = client.get("/corpus/topic-matrix", params={"terms": terms})
    assert res.status_code == 400
    assert "20" in res.json()["detail"]


def test_bad_window_is_400(tmp_path: Path) -> None:
    _write_small_corpus(tmp_path / "jsonl")
    with _client(tmp_path) as client:
        matrix = client.get(
            "/corpus/topic-matrix", params={"terms": "bitcoin", "window_days": 0}
        )
        quality = client.get("/corpus/quality", params={"window_days": 400})
    assert matrix.status_code == 400
    assert quality.status_code == 400


def test_quality_returns_snapshot(tmp_path: Path) -> None:
    _write_small_corpus(tmp_path / "jsonl")
    with _client(tmp_path) as client:
        res = client.get("/corpus/quality")
    assert res.status_code == 200
    data = res.json()
    assert set(data) == _QUALITY_KEYS
    assert data["total_captures"] == 4
    assert data["empty_text"] == 0
    assert data["duplicate_ratio"] == 0.0
    assert data["near_duplicate_ratio"] == 0.0
    assert data["languages"] == {"en": 2, "tr": 2}
    assert data["top_domains"] == [
        {"domain": "blog.example", "count": 2},
        {"domain": "news.example", "count": 2},
    ]
    assert data["dedup_group_count"] == 0
    assert data["capture_rate_per_day"] > 0


def test_quality_empty_corpus_is_200_with_zeros(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        res = client.get("/corpus/quality")
    assert res.status_code == 200
    data = res.json()
    assert data["total_captures"] == 0
    assert data["duplicate_ratio"] == 0.0
    assert data["languages"] == {}
    assert data["top_domains"] == []
    assert data["capture_rate_per_day"] == 0.0


def test_index_not_ready_is_503(tmp_path: Path) -> None:
    index = MagicMock()
    index.health_snapshot.return_value = {"ready": False}
    app = FastAPI()
    app.include_router(create_corpusx_router(index))
    with TestClient(app) as client:
        matrix = client.get("/corpus/topic-matrix", params={"terms": "bitcoin"})
        quality = client.get("/corpus/quality")
    assert matrix.status_code == 503
    assert quality.status_code == 503
    assert "not ready" in matrix.json()["detail"]
    assert "not ready" in quality.json()["detail"]


def test_index_ready_attribute_surface(tmp_path: Path) -> None:
    index = MagicMock()
    del index.health_snapshot
    index.index_ready = False
    app = FastAPI()
    app.include_router(create_corpusx_router(index))
    with TestClient(app) as client:
        res = client.get("/corpus/quality")
    assert res.status_code == 503
