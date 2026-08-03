"""API tests for the /origin router (shape, status codes, edge cases).

Mounts :func:`~awareness.origin.router.create_origin_router` on a bare
FastAPI app and drives it with FastAPI's TestClient.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from awareness.origin.router import create_origin_router
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

_STORY_KEYS = {
    "term", "origin_domain", "origin_url", "origin_title", "origin_ts",
    "replica_count", "replicas", "lead_minutes",
}
_REPLICA_KEYS = {"domain", "first_ts"}
_PUBLISHER_KEYS = {"domain", "origin_count", "total_stories"}


def _write_doc(
    root: Path,
    idx: int,
    *,
    ts: datetime,
    domain: str,
    group: str | None = None,
    text: str = "breaking news about bitcoin",
    title: str = "t",
    url: str | None = None,
) -> None:
    day = root / "captures" / f"{ts:%Y}" / f"{ts:%m}" / f"{ts:%d}"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}",
        capture_id=f"cap-{idx}",
        parent_doc_or_dup_group=group,
        source_type="rss",
        domain=domain,
        url=url or f"https://{domain}/{idx}",
        canonical_url=url or f"https://{domain}/{idx}",
        fetch_ts=ts.isoformat(),
        observed_ts=ts.isoformat(),
        title=title,
        text=text,
        content_hash=f"hash-{idx}",
    )
    (day / f"chunk-{idx}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _client(tmp_path: Path) -> TestClient:
    index = DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )
    app = FastAPI()
    app.include_router(create_origin_router(index))
    return TestClient(app)


def test_stories_endpoint_returns_cluster_shape(tmp_path: Path) -> None:
    root = tmp_path / "jsonl"
    now = datetime.now(UTC)
    origin_ts = now - timedelta(days=3, minutes=90)
    replica_ts = origin_ts + timedelta(minutes=90)
    _write_doc(
        root, 1, ts=origin_ts, domain="wire.example", group="grp-1",
        title="Bitcoin breaks out", url="https://wire.example/story-1",
    )
    _write_doc(root, 2, ts=replica_ts, domain="echo.example", group="grp-1")
    _write_doc(root, 3, ts=now - timedelta(days=2), domain="solo.example", group=None)

    with _client(tmp_path) as client:
        res = client.get("/origin/stories", params={"term": "bitcoin"})
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    assert len(data) == 1
    story = data[0]
    assert set(story) == _STORY_KEYS
    assert story["term"] == "bitcoin"
    assert story["origin_domain"] == "wire.example"
    assert story["origin_url"] == "https://wire.example/story-1"
    assert story["origin_title"] == "Bitcoin breaks out"
    assert story["replica_count"] == 1
    assert set(story["replicas"][0]) == _REPLICA_KEYS
    assert story["replicas"][0]["domain"] == "echo.example"
    assert story["lead_minutes"] == 90


def test_publishers_endpoint_returns_ranking(tmp_path: Path) -> None:
    root = tmp_path / "jsonl"
    now = datetime.now(UTC)
    _write_doc(root, 1, ts=now - timedelta(days=3, minutes=90), domain="wire.example", group="grp-1")
    _write_doc(root, 2, ts=now - timedelta(days=3), domain="echo.example", group="grp-1")

    with _client(tmp_path) as client:
        res = client.get("/origin/publishers", params={"term": "bitcoin"})
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 1
    assert set(data[0]) == _PUBLISHER_KEYS
    assert data[0] == {"domain": "wire.example", "origin_count": 1, "total_stories": 1}


def test_bad_inputs_are_400(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        empty_term = client.get("/origin/stories", params={"term": ""})
        long_term = client.get("/origin/stories", params={"term": "x" * 201})
        bad_window = client.get("/origin/stories", params={"term": "bitcoin", "window_days": 0})
        huge_window = client.get("/origin/stories", params={"term": "bitcoin", "window_days": 9999})
        pubs_empty = client.get("/origin/publishers", params={"term": ""})
    assert empty_term.status_code == 400
    assert long_term.status_code == 400
    assert bad_window.status_code == 400
    assert huge_window.status_code == 400
    assert pubs_empty.status_code == 400


def test_limit_is_clamped_not_rejected(tmp_path: Path) -> None:
    root = tmp_path / "jsonl"
    now = datetime.now(UTC)
    _write_doc(root, 1, ts=now - timedelta(days=3, minutes=90), domain="wire.example", group="grp-1")
    _write_doc(root, 2, ts=now - timedelta(days=3), domain="echo.example", group="grp-1")

    with _client(tmp_path) as client:
        res = client.get("/origin/stories", params={"term": "bitcoin", "limit": 999999})
    assert res.status_code == 200
    assert len(res.json()) == 1


def test_empty_corpus_returns_empty_lists(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        stories = client.get("/origin/stories", params={"term": "bitcoin"})
        publishers = client.get("/origin/publishers", params={"term": "bitcoin"})
    assert stories.status_code == 200
    assert stories.json() == []
    assert publishers.status_code == 200
    assert publishers.json() == []


def test_index_not_ready_is_503(tmp_path: Path) -> None:
    index = MagicMock()
    index.health_snapshot.return_value = {"ready": False}
    app = FastAPI()
    app.include_router(create_origin_router(index))
    with TestClient(app) as client:
        stories = client.get("/origin/stories", params={"term": "bitcoin"})
        publishers = client.get("/origin/publishers", params={"term": "bitcoin"})
    assert stories.status_code == 503
    assert "not ready" in stories.json()["detail"]
    assert publishers.status_code == 503


def test_index_ready_attribute_surface(tmp_path: Path) -> None:
    index = MagicMock()
    del index.health_snapshot
    index.index_ready = False
    app = FastAPI()
    app.include_router(create_origin_router(index))
    with TestClient(app) as client:
        res = client.get("/origin/stories", params={"term": "bitcoin"})
    assert res.status_code == 503
