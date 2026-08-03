"""API tests for the /analytics router (shape, status codes, edge cases).

Mounts :func:`~awareness.analytics.router.create_analytics_router` on a bare
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

from awareness.analytics.router import create_analytics_router
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
    app.include_router(create_analytics_router(index))
    return TestClient(app)


def test_term_frequency_returns_bucket_shape(tmp_path: Path) -> None:
    root = tmp_path / "jsonl"
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    _write_doc(root, 1, ts=base, title="Bitcoin hits record", text="market rally")
    _write_doc(root, 2, ts=base + timedelta(hours=3), title="Bitcoin crash", text="dip")
    _write_doc(root, 3, ts=base + timedelta(days=1), title="Bitcoin analysis", text="deep dive")

    with _client(tmp_path) as client:
        res = client.get(
            "/analytics/term-frequency",
            params={
                "term": "bitcoin",
                "start": "2026-06-01T00:00:00Z",
                "end": "2026-06-03T00:00:00Z",
            },
        )
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    assert len(data) == 3
    assert set(data[0]) == {"ts", "count"}
    assert data[0]["ts"].startswith("2026-06-01")
    assert data[0]["count"] == 2
    assert data[1]["count"] == 1
    assert data[2]["count"] == 0


def test_bad_term_is_400(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        empty = client.get("/analytics/term-frequency", params={"term": ""})
        too_long = client.get("/analytics/term-frequency", params={"term": "x" * 201})
        spike_empty = client.get("/analytics/spikes", params={"term": ""})
        bad_window = client.get("/analytics/term-frequency", params={"term": "bitcoin", "window_days": 0})
        bad_granularity = client.get(
            "/analytics/term-frequency", params={"term": "bitcoin", "granularity": "hourly"}
        )
    assert empty.status_code == 400
    assert too_long.status_code == 400
    assert spike_empty.status_code == 400
    assert bad_window.status_code == 400
    assert bad_granularity.status_code == 400


def test_start_after_end_is_400(tmp_path: Path) -> None:
    root = tmp_path / "jsonl"
    _write_doc(
        root, 1, ts=datetime(2026, 6, 1, 12, 0, tzinfo=UTC), title="Bitcoin", text="x"
    )
    with _client(tmp_path) as client:
        res = client.get(
            "/analytics/term-frequency",
            params={
                "term": "bitcoin",
                "start": "2026-06-05T00:00:00Z",
                "end": "2026-06-01T00:00:00Z",
            },
        )
    assert res.status_code == 400
    assert "start" in res.json()["detail"]


def test_empty_corpus_returns_empty_lists(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        freq = client.get("/analytics/term-frequency", params={"term": "bitcoin"})
        terms = client.get("/analytics/top-terms")
        spikes = client.get("/analytics/spikes", params={"term": "bitcoin"})
        domains = client.get("/analytics/domains")
        langs = client.get("/analytics/languages")
        co = client.get("/analytics/co-occurring", params={"term": "bitcoin"})
    for res in (freq, terms, spikes, domains, langs, co):
        assert res.status_code == 200
        assert res.json() == []


def test_spikes_endpoint_detects_outlier(tmp_path: Path) -> None:
    root = tmp_path / "jsonl"
    base = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)
    for i in range(14):
        _write_doc(root, i + 1, ts=base + timedelta(days=i), title="Pump report", text="daily pump")
    spike_day = base + timedelta(days=14)
    for i in range(10):
        _write_doc(root, 100 + i, ts=spike_day + timedelta(hours=i), title="Pump alert", text="bitcoin pump")

    with _client(tmp_path) as client:
        res = client.get("/analytics/spikes", params={"term": "pump", "window_days": 14})
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 1
    assert set(data[0]) == {"bucket", "count", "zscore", "mean", "std", "vs_mean"}
    assert data[0]["count"] == 10
    assert data[0]["zscore"] > 2.5


def test_breakdown_endpoints_shape(tmp_path: Path) -> None:
    root = tmp_path / "jsonl"
    base = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    _write_doc(root, 1, ts=base, domain="news.example", language="en", text="Bitcoin price surges in market")
    _write_doc(root, 2, ts=base + timedelta(hours=1), domain="news.example", language="en", text="Bitcoin adoption grows in market")
    _write_doc(root, 3, ts=base + timedelta(hours=2), domain="blog.example", language="tr", text="Bitcoin rally")

    with _client(tmp_path) as client:
        doms = client.get("/analytics/domains")
        langs = client.get("/analytics/languages")
        co = client.get("/analytics/co-occurring", params={"term": "bitcoin"})
    assert doms.status_code == 200
    assert doms.json()[0] == {"domain": "news.example", "count": 2}
    assert langs.status_code == 200
    assert langs.json()[0]["language"] == "en"
    assert langs.json()[0]["count"] == 2
    assert co.status_code == 200
    assert co.json()[0]["term"] == "market"
    assert co.json()[0]["count"] == 2


def test_top_terms_endpoint_shape(tmp_path: Path) -> None:
    root = tmp_path / "jsonl"
    base = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    for i in range(3):
        _write_doc(root, i + 1, ts=base + timedelta(hours=i), text="bitcoin market rally")
    with _client(tmp_path) as client:
        res = client.get("/analytics/top-terms", params={"limit": 2, "min_count": 2})
    assert res.status_code == 200
    assert res.json() == [
        {"term": "bitcoin", "count": 3},
        {"term": "market", "count": 3},
    ]


def test_limits_are_clamped_not_rejected(tmp_path: Path) -> None:
    root = tmp_path / "jsonl"
    _write_doc(
        root, 1, ts=datetime(2026, 6, 1, 9, 0, tzinfo=UTC), domain="example.com", text="bitcoin"
    )
    with _client(tmp_path) as client:
        doms = client.get("/analytics/domains", params={"limit": 100000})
        terms = client.get("/analytics/top-terms", params={"limit": -5})
    assert doms.status_code == 200
    assert terms.status_code == 200
    assert len(terms.json()) == 0  # no term reaches min_count in this corpus


def test_index_not_ready_is_503(tmp_path: Path) -> None:
    index = MagicMock()
    index.health_snapshot.return_value = {"ready": False}
    app = FastAPI()
    app.include_router(create_analytics_router(index))
    with TestClient(app) as client:
        res = client.get("/analytics/domains")
    assert res.status_code == 503
    assert "not ready" in res.json()["detail"]


def test_index_ready_attribute_surface(tmp_path: Path) -> None:
    index = MagicMock()
    del index.health_snapshot
    index.index_ready = False
    app = FastAPI()
    app.include_router(create_analytics_router(index))
    with TestClient(app) as client:
        res = client.get("/analytics/domains")
    assert res.status_code == 503
