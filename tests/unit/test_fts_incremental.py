"""Persisted + append-only incremental FTS (C3-T1).

DuckDB's FTS tables survive file reconnect. We must:
1. Reuse captures_idx + FTS across DuckDbIndex process restarts when the
   on-disk corpus fingerprint is unchanged (no full rebuild).
2. On pure append (new capture_ids only), INSERT the delta into captures_idx
   instead of CREATE OR REPLACE from the full JSONL view.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from awareness.storage.duckdb_index import DuckDbIndex


@pytest.fixture(autouse=True)
def _disable_fts_coalescing(monkeypatch: pytest.MonkeyPatch) -> None:
    """W25: these tests pin the immediate-rebuild FTS behavior — the new
    ``_FTS_COALESCE_WINDOW_SECONDS`` coalescing window would defer the
    rebuild past the assertion points. Disable it (window 0 = rebuild on
    the next search, exactly the pre-W25 contract)."""
    monkeypatch.setattr("awareness.storage.duckdb_index._FTS_COALESCE_WINDOW_SECONDS", 0.0)

_FULL_KEYS = (
    "doc_id", "capture_id", "parent_doc_or_dup_group", "source_type",
    "source_name", "source_locator", "source_shard",
    "source_offset_or_record_id", "discovery_channel", "job_id", "batch_id",
    "ingest_version", "url", "canonical_url", "domain", "fetch_ts",
    "observed_ts", "published_ts", "last_modified", "content_type",
    "http_status", "etag", "title", "text", "language", "content_hash",
    "near_dup_hash", "robots_decision", "terms_note_if_relevant",
)


def _write_chunk(jsonl_dir: Path, name: str, doc_id: str, text: str) -> None:
    day = jsonl_dir / "captures" / "2026" / "06" / "08"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=doc_id,
        capture_id=doc_id,
        url=f"http://example.test/{doc_id}",
        canonical_url=f"http://example.test/{doc_id}",
        domain="example.test",
        source_type="rss",
        title=text,
        text=text,
        language="en",
        fetch_ts="2026-06-08T00:00:00+00:00",
    )
    (day / name).write_text(json.dumps(rec) + "\n", encoding="utf-8")


def test_fts_reused_after_reopen_same_corpus(tmp_path: Path) -> None:
    """Second process open must not rebuild FTS when JSONL fingerprint matches."""
    jsonl_dir = tmp_path / "jsonl"
    db_path = tmp_path / "idx.duckdb"
    _write_chunk(jsonl_dir, "a.jsonl", "d1", "xylophone rambunctious")

    idx = DuckDbIndex(db_path=db_path, jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    assert idx.search("xylophone", limit=10)["total"] >= 1
    assert idx._fts_full_rebuilds == 1
    assert idx._fts_restores == 0
    idx.close()

    idx2 = DuckDbIndex(db_path=db_path, jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    assert idx2.search("xylophone", limit=10)["total"] >= 1
    assert idx2._fts_full_rebuilds == 0, "unchanged corpus must reuse persisted FTS"
    assert idx2._fts_restores == 1
    idx2.close()


def test_fts_append_only_skips_full_rematerialize(tmp_path: Path) -> None:
    """New JSONL chunk → delta INSERT into captures_idx, not full REPLACE."""
    jsonl_dir = tmp_path / "jsonl"
    db_path = tmp_path / "idx.duckdb"
    _write_chunk(jsonl_dir, "a.jsonl", "d1", "xylophone rambunctious")

    idx = DuckDbIndex(db_path=db_path, jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    assert idx.search("xylophone", limit=10)["total"] >= 1
    assert idx._fts_full_rebuilds == 1
    assert idx._fts_incremental_appends == 0

    _write_chunk(jsonl_dir, "b.jsonl", "d2", "pluviophile zymurgy")
    assert idx.search("pluviophile", limit=10)["total"] >= 1
    assert idx.search("xylophone", limit=10)["total"] >= 1
    assert idx._fts_incremental_appends == 1
    assert idx._fts_full_rebuilds == 1, "append must not trigger second full rebuild"
    idx.close()


def test_fts_content_swap_still_full_rebuilds(tmp_path: Path) -> None:
    """Same row count but replaced content cannot take the append path."""
    jsonl_dir = tmp_path / "jsonl"
    db_path = tmp_path / "idx.duckdb"
    _write_chunk(jsonl_dir, "a.jsonl", "d1", "xylophone rambunctious")

    idx = DuckDbIndex(db_path=db_path, jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    assert idx.search("xylophone", limit=10)["total"] >= 1
    assert idx._fts_full_rebuilds == 1

    (jsonl_dir / "captures" / "2026" / "06" / "08" / "a.jsonl").unlink()
    _write_chunk(jsonl_dir, "b.jsonl", "d2", "pluviophile zymurgy")

    assert idx.search("pluviophile", limit=10)["total"] >= 1
    assert idx.search("xylophone", limit=10)["total"] == 0
    assert idx._fts_full_rebuilds == 2
    assert idx._fts_incremental_appends == 0
    idx.close()


def test_fts_paths_emit_process_metrics(tmp_path: Path) -> None:
    """Full rebuild, restore, and incremental append each bump fts.* metrics."""
    from awareness.obs.metrics import get_metrics

    m = get_metrics()
    before_full = m.counter_value("fts.builds", labels={"mode": "full"})
    before_restore = m.counter_value("fts.builds", labels={"mode": "restore"})
    before_incr = m.counter_value("fts.builds", labels={"mode": "incremental"})

    jsonl_dir = tmp_path / "jsonl"
    db_path = tmp_path / "idx.duckdb"
    _write_chunk(jsonl_dir, "a.jsonl", "d1", "xylophone rambunctious")

    idx = DuckDbIndex(db_path=db_path, jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    assert idx.search("xylophone", limit=10)["total"] >= 1
    assert m.counter_value("fts.builds", labels={"mode": "full"}) >= before_full + 1
    snap = m.snapshot()
    gauges = {g["name"]: g["value"] for g in snap["gauges"]}
    assert gauges.get("fts.indexed_rows", 0) >= 1
    hists = [
        h
        for h in snap["histograms"]
        if h["name"] == "fts.build_seconds" and (h.get("labels") or {}).get("mode") == "full"
    ]
    assert hists and sum(h["count"] for h in hists) >= 1
    idx.close()

    idx2 = DuckDbIndex(db_path=db_path, jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    assert idx2.search("xylophone", limit=10)["total"] >= 1
    assert m.counter_value("fts.builds", labels={"mode": "restore"}) >= before_restore + 1

    _write_chunk(jsonl_dir, "b.jsonl", "d2", "pluviophile zymurgy")
    assert idx2.search("pluviophile", limit=10)["total"] >= 1
    assert m.counter_value("fts.builds", labels={"mode": "incremental"}) >= before_incr + 1
    assert m.counter_value("fts.builds", labels={"mode": "full"}) >= before_full + 1
    gauges2 = {g["name"]: g["value"] for g in m.snapshot()["gauges"]}
    assert gauges2.get("fts.indexed_rows", 0) >= 2
    idx2.close()
