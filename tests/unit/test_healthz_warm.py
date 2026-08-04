"""W19: /healthz reports index_ready=True on the FIRST probe.

The lifespan now warms the search index at startup (guarded, warning-only),
so the first /healthz call after app start sees ready=True instead of the
lazy cold-start False of a previously untouched index.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from awareness.api import server
from awareness.config import get_settings


def _write_corpus(tmp_project: Path) -> None:
    settings = get_settings()
    assert settings.data_dir is not None
    day = settings.staging_jsonl_dir() / "captures" / "2026" / "06" / "01"
    day.mkdir(parents=True, exist_ok=True)
    rec = {
        "doc_id": "doc-1",
        "capture_id": "cap-1",
        "parent_doc_or_dup_group": None,
        "source_type": "rss",
        "source_name": None,
        "source_locator": None,
        "source_shard": None,
        "source_offset_or_record_id": None,
        "discovery_channel": None,
        "job_id": None,
        "batch_id": None,
        "ingest_version": None,
        "url": "https://example.com/climate",
        "canonical_url": None,
        "domain": "example.com",
        "fetch_ts": "2026-06-01T12:00:00+00:00",
        "observed_ts": None,
        "published_ts": None,
        "last_modified": None,
        "content_type": None,
        "http_status": None,
        "etag": None,
        "title": "Climate policy update",
        "text": "Global climate negotiations advanced today with new emissions targets.",
        "language": "en",
        "content_hash": "abc123",
        "near_dup_hash": None,
        "robots_decision": None,
        "terms_note_if_relevant": None,
    }
    (day / "chunk-1.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def test_healthz_index_ready_on_first_probe(tmp_project: Path) -> None:
    _write_corpus(tmp_project)
    server._State.index = None
    server._State.state = None
    app = server.create_app()
    try:
        with TestClient(app) as client:
            r = client.get("/healthz")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["ok"] is True
            assert body["index_ready"] is True, (
                "first /healthz probe must report index_ready=True "
                "(lifespan warm-up), got False"
            )
            assert body["index"]["ready"] is True
            assert int(body["index"]["captures"]) >= 1
    finally:
        server._State.index = None
        server._close_index()
        server._State.state = None


def test_healthz_warmup_failure_does_not_kill_startup(tmp_project: Path, monkeypatch) -> None:
    """A failing index warm-up logs a warning; /healthz still answers."""
    _write_corpus(tmp_project)
    server._State.index = None
    server._State.state = None

    def boom() -> object:
        raise RuntimeError("warmup boom")

    monkeypatch.setattr(server, "_get_index", boom)
    app = server.create_app()
    try:
        with TestClient(app) as client:
            r = client.get("/healthz")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["ok"] is True  # liveness survives
            assert body["index_ready"] is False
            assert "boom" in body["index"]["error"]
    finally:
        server._State.index = None
        server._close_index()
        server._State.state = None
