"""W25: FTS rebuild deferral / coalescing window.

After a write batch the FTS inverted index is stale but the materialized
``captures`` table already serves the new rows. Inside the coalescing
window (``_FTS_COALESCE_WINDOW_SECONDS``) searches fall back to the
table-backed prefix/substring path instead of paying the full
``PRAGMA create_fts_index`` rebuild; the first search after the window
elapses rebuilds once, coalescing N write batches into one rebuild.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import awareness.storage.duckdb_index as mod
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


def test_search_within_window_defers_rebuild_but_returns_correct_results(
    tmp_path: Path,
) -> None:
    """First post-write search inside the window: no rebuild, correct results."""
    jsonl_dir = tmp_path / "jsonl"
    db_path = tmp_path / "idx.duckdb"
    _write_chunk(jsonl_dir, "a.jsonl", "d1", "xylophone rambunctious")
    idx = DuckDbIndex(db_path=db_path, jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    try:
        idx._fts_coalesce_window = 3600.0  # effectively "inside window forever"
        assert idx.search("xylophone", limit=10)["total"] >= 1
        full_rebuilds = idx._fts_full_rebuilds
        appends = idx._fts_incremental_appends
        assert full_rebuilds == 1

        _write_chunk(jsonl_dir, "b.jsonl", "d2", "pluviophile zymurgy")
        res = idx.search("pluviophile", limit=10)
        assert res["total"] >= 1, "new content must be searchable via the fallback"
        assert res["ranked"] is False, "deferred FTS must use the unranked fallback"
        assert idx.search("xylophone", limit=10)["total"] >= 1
        assert idx._fts_full_rebuilds == full_rebuilds, "no rebuild inside the window"
        assert idx._fts_incremental_appends == appends, "no rebuild inside the window"
        assert idx._fts_coalesced_skips >= 1
        assert idx.health_snapshot()["fts_built"] is False, "FTS is stale during defer"
    finally:
        idx.close()


def test_explicit_fts_mode_within_window_degrades_to_prefix(tmp_path: Path) -> None:
    jsonl_dir = tmp_path / "jsonl"
    db_path = tmp_path / "idx.duckdb"
    _write_chunk(jsonl_dir, "a.jsonl", "d1", "xylophone rambunctious")
    idx = DuckDbIndex(db_path=db_path, jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    try:
        idx._fts_coalesce_window = 3600.0
        assert idx.search("xylophone", limit=10, mode="fts")["total"] >= 1
        _write_chunk(jsonl_dir, "b.jsonl", "d2", "pluviophile zymurgy")
        res = idx.search("pluviophile", limit=10, mode="fts")
        assert res["total"] >= 1, "explicit fts must degrade to prefix when deferred"
        assert res["mode"] == "prefix"
    finally:
        idx.close()


def test_two_batches_within_window_coalesce_into_one_rebuild(tmp_path: Path) -> None:
    """Two write batches inside the window → exactly one rebuild (after expiry)."""
    jsonl_dir = tmp_path / "jsonl"
    db_path = tmp_path / "idx.duckdb"
    _write_chunk(jsonl_dir, "a.jsonl", "d1", "xylophone rambunctious")
    idx = DuckDbIndex(db_path=db_path, jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    try:
        idx._fts_coalesce_window = 3600.0
        assert idx.search("xylophone", limit=10)["total"] >= 1
        assert idx._fts_full_rebuilds == 1

        # Batch 1 + search (deferred).
        _write_chunk(jsonl_dir, "b.jsonl", "d2", "pluviophile zymurgy")
        assert idx.search("pluviophile", limit=10)["total"] >= 1
        assert idx._fts_full_rebuilds == 1

        # Batch 2 + search (still deferred; window reset by the second refresh).
        _write_chunk(jsonl_dir, "c.jsonl", "d3", "quintessential quagmire")
        assert idx.search("quintessential", limit=10)["total"] >= 1
        assert idx._fts_full_rebuilds == 1
        assert idx._fts_incremental_appends == 0
        assert idx._fts_coalesced_skips == 2

        # Window expiry (simulated) → the next search rebuilds ONCE.
        idx._fts_dirty_since = time.monotonic() - 4000.0
        res = idx.search("quintessential", limit=10)
        assert res["total"] >= 1
        assert res["ranked"] is True, "rebuild after the window must serve ranked FTS"
        assert idx._fts_incremental_appends == 1, "pure append rebuilds incrementally"
        assert idx._fts_full_rebuilds == 1
        assert idx.health_snapshot()["fts_built"] is True
        assert idx.health_snapshot()["fts_built_for_count"] == 3

        # Subsequent searches are warm.
        assert idx.search("xylophone", limit=10)["total"] >= 1
        assert idx._fts_incremental_appends == 1
        assert idx._fts_full_rebuilds == 1
    finally:
        idx.close()


def test_window_zero_disables_coalescing(tmp_path: Path) -> None:
    """Window 0 = immediate rebuild on the next search (test-mode contract)."""
    jsonl_dir = tmp_path / "jsonl"
    db_path = tmp_path / "idx.duckdb"
    _write_chunk(jsonl_dir, "a.jsonl", "d1", "xylophone rambunctious")
    idx = DuckDbIndex(db_path=db_path, jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    try:
        idx._fts_coalesce_window = 0.0
        assert idx.search("xylophone", limit=10)["total"] >= 1
        _write_chunk(jsonl_dir, "b.jsonl", "d2", "pluviophile zymurgy")
        assert idx.search("pluviophile", limit=10)["total"] >= 1
        assert idx._fts_incremental_appends == 1, "window 0 must rebuild immediately"
        assert idx._fts_coalesced_skips == 0
    finally:
        idx.close()


def test_content_swap_within_window_still_serves_updated_content(tmp_path: Path) -> None:
    """The fallback reads the materialized table, so updated rows are served
    even while the FTS rebuild is deferred."""
    jsonl_dir = tmp_path / "jsonl"
    db_path = tmp_path / "idx.duckdb"
    _write_chunk(jsonl_dir, "a.jsonl", "d1", "xylophone rambunctious")
    idx = DuckDbIndex(db_path=db_path, jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    try:
        idx._fts_coalesce_window = 3600.0
        assert idx.search("xylophone", limit=10)["total"] >= 1
        (jsonl_dir / "captures" / "2026" / "06" / "08" / "a.jsonl").unlink()
        _write_chunk(jsonl_dir, "b.jsonl", "d2", "pluviophile zymurgy")
        assert idx.search("pluviophile", limit=10)["total"] >= 1
        assert idx.search("xylophone", limit=10)["total"] == 0, "stale content must be gone"
        assert idx._fts_full_rebuilds == 1, "rebuild still deferred inside the window"
    finally:
        idx.close()


def test_fts_dirty_reset_after_rebuild(tmp_path: Path) -> None:
    """After a rebuild the dirty timestamp clears; further searches are warm."""
    jsonl_dir = tmp_path / "jsonl"
    db_path = tmp_path / "idx.duckdb"
    _write_chunk(jsonl_dir, "a.jsonl", "d1", "xylophone rambunctious")
    idx = DuckDbIndex(db_path=db_path, jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    try:
        idx._fts_coalesce_window = 0.0
        assert idx.search("xylophone", limit=10)["total"] >= 1
        _write_chunk(jsonl_dir, "b.jsonl", "d2", "pluviophile zymurgy")
        assert idx.search("pluviophile", limit=10)["total"] >= 1
        assert idx._fts_dirty_since is None, "rebuild must clear the dirty marker"
        assert idx._fts_coalesced_skips == 0
    finally:
        idx.close()


def test_module_constant_default_is_thirty_seconds() -> None:
    assert mod._FTS_COALESCE_WINDOW_SECONDS == 30.0


def test_no_write_no_defer(tmp_path: Path) -> None:
    """Steady-state searches (no corpus change) never defer or rebuild."""
    jsonl_dir = tmp_path / "jsonl"
    db_path = tmp_path / "idx.duckdb"
    _write_chunk(jsonl_dir, "a.jsonl", "d1", "xylophone rambunctious")
    idx = DuckDbIndex(db_path=db_path, jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    try:
        assert idx.search("xylophone", limit=10)["total"] >= 1
        assert idx.search("xylophone", limit=10)["total"] >= 1
        assert idx._fts_full_rebuilds == 1
        assert idx._fts_coalesced_skips == 0
        assert idx.health_snapshot()["fts_built"] is True
    finally:
        idx.close()
