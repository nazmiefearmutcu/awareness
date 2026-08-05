"""API tests for the /qualityx router (status codes, shapes, edge cases).

Mounts :func:`~awareness.qualityx.router.create_qualityx_router` on a bare
FastAPI app (the real server wiring lives outside this subsystem's scope) and
drives it with FastAPI's TestClient.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from awareness.qualityx.router import create_qualityx_router
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

_BASE = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


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


def _write_small_corpus(root: Path) -> None:
    # Day 1: exact-dup pair on news.example. Day 2: one unique doc on a new
    # domain (blog.example).
    _write_doc(root, 1, ts=_BASE, domain="news.example", text="alpha one", content_hash="h1")
    _write_doc(root, 2, ts=_BASE + timedelta(hours=1), domain="news.example", text="alpha two", content_hash="h1")
    _write_doc(root, 3, ts=_BASE + timedelta(days=1), domain="blog.example", text="beta one", content_hash="h3")


def _client(tmp_path: Path) -> TestClient:
    index = DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )
    app = FastAPI()
    app.include_router(create_qualityx_router(index))
    return TestClient(app)


def _mock_index_ready(ready: bool) -> MagicMock:
    index = MagicMock()
    index.health_snapshot.return_value = {"ready": ready}
    return index


# ── history ─────────────────────────────────────────────────────────────────


def test_history_returns_per_day_points(tmp_path: Path) -> None:
    _write_small_corpus(tmp_path / "jsonl")
    with _client(tmp_path) as client:
        res = client.get("/qualityx/history", params={"days": 30})
    assert res.status_code == 200
    data = res.json()
    assert data["days"] == 30
    assert len(data["points"]) == 30
    assert set(data["points"][0]) == {
        "ts", "total", "duplicate_ratio", "near_duplicate_ratio",
        "avg_length", "new_domains", "capture_rate",
    }
    by_ts = {p["ts"]: p for p in data["points"]}
    assert by_ts["2026-06-01"]["total"] == 2
    assert by_ts["2026-06-01"]["duplicate_ratio"] == pytest.approx(1.0)
    assert by_ts["2026-06-01"]["new_domains"] == 1
    assert by_ts["2026-06-02"]["total"] == 1
    assert by_ts["2026-06-02"]["duplicate_ratio"] == 0.0
    assert by_ts["2026-06-02"]["new_domains"] == 1


def test_history_defaults_to_30_days(tmp_path: Path) -> None:
    _write_small_corpus(tmp_path / "jsonl")
    with _client(tmp_path) as client:
        res = client.get("/qualityx/history")
    assert res.status_code == 200
    data = res.json()
    assert data["days"] == 30
    assert len(data["points"]) == 30


def test_history_bad_days_is_400(tmp_path: Path) -> None:
    _write_small_corpus(tmp_path / "jsonl")
    with _client(tmp_path) as client:
        zero = client.get("/qualityx/history", params={"days": 0})
        too_many = client.get("/qualityx/history", params={"days": 366})
    for res in (zero, too_many):
        assert res.status_code == 400
        assert "bad request" in res.json()["detail"]
    # Non-integer days never reaches the handler: FastAPI rejects it as 422.
    bad = client.get("/qualityx/history", params={"days": "abc"})
    assert bad.status_code == 422


def test_history_bad_granularity_is_400(tmp_path: Path) -> None:
    _write_small_corpus(tmp_path / "jsonl")
    with _client(tmp_path) as client:
        res = client.get("/qualityx/history", params={"granularity": "hour"})
    assert res.status_code == 400
    assert "granularity" in res.json()["detail"]


def test_history_week_granularity_ok(tmp_path: Path) -> None:
    _write_small_corpus(tmp_path / "jsonl")
    with _client(tmp_path) as client:
        res = client.get("/qualityx/history", params={"days": 7, "granularity": "week"})
    assert res.status_code == 200
    data = res.json()
    assert data["days"] == 7
    assert data["points"][-1]["ts"] == date(2026, 6, 1).isoformat()  # Monday
    assert data["points"][-1]["total"] == 3


def test_history_empty_corpus_is_200_with_zeroed_points(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        res = client.get("/qualityx/history", params={"days": 3})
    assert res.status_code == 200
    data = res.json()
    assert len(data["points"]) == 3
    assert all(p["total"] == 0 for p in data["points"])
    assert all(p["duplicate_ratio"] == 0.0 for p in data["points"])


def test_history_index_not_ready_is_503(tmp_path: Path) -> None:
    index = _mock_index_ready(False)
    app = FastAPI()
    app.include_router(create_qualityx_router(index))
    with TestClient(app) as client:
        res = client.get("/qualityx/history", params={"days": 7})
    assert res.status_code == 503
    assert "not ready" in res.json()["detail"]


def test_history_index_ready_attribute_surface(tmp_path: Path) -> None:
    index = MagicMock()
    del index.health_snapshot
    index.index_ready = False
    app = FastAPI()
    app.include_router(create_qualityx_router(index))
    with TestClient(app) as client:
        res = client.get("/qualityx/history")
    assert res.status_code == 503


# ── current snapshot ────────────────────────────────────────────────────────


def test_current_returns_snapshot(tmp_path: Path) -> None:
    _write_small_corpus(tmp_path / "jsonl")
    with _client(tmp_path) as client:
        res = client.get("/qualityx/current")
    assert res.status_code == 200
    data = res.json()
    assert data["total_captures"] == 3
    assert data["duplicate_ratio"] == pytest.approx(2 / 3)
    assert data["top_domains"] == [
        {"domain": "news.example", "count": 2},
        {"domain": "blog.example", "count": 1},
    ]
    assert "languages" in data and "capture_rate_per_day" in data


def test_current_empty_corpus_is_200_zeroed(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        res = client.get("/qualityx/current")
    assert res.status_code == 200
    data = res.json()
    assert data["total_captures"] == 0
    assert data["duplicate_ratio"] == 0.0


def test_current_index_not_ready_is_503(tmp_path: Path) -> None:
    index = _mock_index_ready(False)
    app = FastAPI()
    app.include_router(create_qualityx_router(index))
    with TestClient(app) as client:
        res = client.get("/qualityx/current")
    assert res.status_code == 503
    assert "not ready" in res.json()["detail"]
