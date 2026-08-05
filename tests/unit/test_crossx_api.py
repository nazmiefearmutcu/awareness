"""API tests for the /crossx router (shape, status codes, edge cases).

Mounts :func:`~awareness.crossx.router.create_crossx_router` on a bare
FastAPI app (the real server wiring lives outside this subsystem's scope)
and drives it with FastAPI's TestClient. The X store is a temp
:class:`~awareness.xscraper.store.SessionStore` with a simulated session,
bound through an explicit ``x_store_getter``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from awareness.crossx.router import create_crossx_router
from awareness.storage.duckdb_index import DuckDbIndex
from awareness.xscraper.models import SearchRequest
from awareness.xscraper.simulate import simulate_session
from awareness.xscraper.store import SessionStore

_FULL_KEYS = (
    "doc_id", "capture_id", "parent_doc_or_dup_group", "source_type",
    "source_name", "source_locator", "source_shard",
    "source_offset_or_record_id", "discovery_channel", "job_id", "batch_id",
    "ingest_version", "url", "canonical_url", "domain", "fetch_ts",
    "observed_ts", "published_ts", "last_modified", "content_type",
    "http_status", "etag", "title", "text", "language", "content_hash",
    "near_dup_hash", "robots_decision", "terms_note_if_relevant",
)

_VIEW_KEYS = {
    "term", "news_phase", "news_series", "news_sentiment", "x_sentiment",
    "news_avg_score", "x_avg_score", "correlation_r", "convergence", "note",
}


def _write_doc(root: Path, idx: int, *, ts: datetime, text: str) -> None:
    day = root / "captures" / f"{ts:%Y}" / f"{ts:%m}" / f"{ts:%d}"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}",
        capture_id=f"cap-{idx:04d}",
        source_type="rss",
        domain="news.example",
        url=f"https://news.example/{idx}",
        fetch_ts=ts.isoformat(),
        observed_ts=ts.isoformat(),
        title=f"Headline {idx}",
        text=text,
        language="en",
    )
    (day / f"chunk-{idx:04d}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _write_corpus(root: Path) -> None:
    now = datetime.now(tz=UTC)
    for i in range(8):
        _write_doc(
            root, i, ts=now - timedelta(days=14 - 2 * i), text="bitcoin surges"
        )


def _make_x_store(tmp_path: Path) -> tuple[Path, str]:
    """Create + simulate a session in a temp store; return (path, session_id)."""

    async def _build() -> tuple[Path, str]:
        path = tmp_path / "xscraper.sqlite"
        store = SessionStore(path)
        await store.open()
        await store.init()
        request = SearchRequest(keywords=["bitcoin"], title="api test", lookback="14d")
        session = await store.create_session(request, "bitcoin")
        await simulate_session(store, session.session_id, n_tweets=40, seed=4)
        await store.close()
        return path, session.session_id

    return asyncio.run(_build())


def _client(tmp_path: Path) -> tuple[TestClient, str]:
    _write_corpus(tmp_path / "jsonl")
    index = DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )
    store_path, session_id = _make_x_store(tmp_path)
    app = FastAPI()
    app.include_router(create_crossx_router(index, lambda: store_path))
    return TestClient(app), session_id


def test_view_returns_aligned_payload(tmp_path: Path) -> None:
    client, session_id = _client(tmp_path)
    with client as c:
        res = c.get(
            "/crossx/view",
            params={"term": "bitcoin", "session_id": session_id, "window_days": 14},
        )
    assert res.status_code == 200, res.text
    data = res.json()
    assert set(data) == _VIEW_KEYS
    assert data["term"] == "bitcoin"
    assert data["news_phase"] in {
        "EMERGING", "EXPANDING", "PEAKING", "DECLINING", "DORMANT", "STABLE",
    }
    assert data["x_sentiment"] is not None
    assert len(data["news_sentiment"]) == len(data["x_sentiment"]) == 15
    assert data["convergence"] in {"aligned bullish", "aligned bearish", "divergence", "neutral"}
    assert -1.0 <= data["correlation_r"] <= 1.0


def test_view_unknown_session_is_200_with_x_none(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    with client as c:
        res = c.get(
            "/crossx/view",
            params={"term": "bitcoin", "session_id": "does-not-exist"},
        )
    assert res.status_code == 200
    data = res.json()
    assert data["x_sentiment"] is None
    assert data["x_avg_score"] == 0.0
    assert data["correlation_r"] == 0.0
    assert data["convergence"] == "neutral"
    assert "news side only" in data["note"]
    # The news side is still fully present.
    assert data["news_phase"] != ""


def test_view_missing_term_is_400(tmp_path: Path) -> None:
    client, session_id = _client(tmp_path)
    with client as c:
        missing = c.get("/crossx/view", params={"session_id": session_id})
        empty = c.get("/crossx/view", params={"term": "", "session_id": session_id})
        blank = c.get("/crossx/view", params={"term": "   ", "session_id": session_id})
    assert missing.status_code == 400
    assert empty.status_code == 400
    assert blank.status_code == 400


def test_view_missing_session_is_400(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    with client as c:
        missing = c.get("/crossx/view", params={"term": "bitcoin"})
        empty = c.get("/crossx/view", params={"term": "bitcoin", "session_id": ""})
    assert missing.status_code == 400
    assert empty.status_code == 400


def test_view_bad_window_is_400(tmp_path: Path) -> None:
    client, session_id = _client(tmp_path)
    with client as c:
        too_small = c.get(
            "/crossx/view", params={"term": "bitcoin", "session_id": session_id, "window_days": 0}
        )
        too_big = c.get(
            "/crossx/view", params={"term": "bitcoin", "session_id": session_id, "window_days": 400}
        )
    assert too_small.status_code == 400
    assert too_big.status_code == 400


def test_view_index_not_ready_is_503(tmp_path: Path) -> None:
    index = MagicMock()
    index.health_snapshot.return_value = {"ready": False}
    app = FastAPI()
    app.include_router(create_crossx_router(index, lambda: tmp_path / "xscraper.sqlite"))
    with TestClient(app) as client:
        res = client.get(
            "/crossx/view",
            params={"term": "bitcoin", "session_id": "s1"},
        )
    assert res.status_code == 503
    assert "not ready" in res.json()["detail"]


def test_view_defaults_window_days_to_14(tmp_path: Path) -> None:
    client, session_id = _client(tmp_path)
    with client as c:
        res = c.get("/crossx/view", params={"term": "bitcoin", "session_id": session_id})
    assert res.status_code == 200, res.text
    assert len(res.json()["news_sentiment"]) == 15
