from __future__ import annotations

import json
from pathlib import Path

from awareness.storage.duckdb_index import DuckDbIndex

# Mirror the production 29-field schema (same as test_search_matching.py)
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


def test_fts_reflects_new_content_at_same_row_count(tmp_path: Path) -> None:
    jsonl_dir = tmp_path / "jsonl"
    # Use fully disjoint vocabulary so FTS cannot cross-match.
    _write_chunk(jsonl_dir, "a.jsonl", "d1", "xylophone rambunctious")
    idx = DuckDbIndex(db_path=tmp_path / "idx.duckdb", jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    assert idx.search("xylophone", limit=10)["total"] >= 1

    (jsonl_dir / "captures" / "2026" / "06" / "08" / "a.jsonl").unlink()
    _write_chunk(jsonl_dir, "b.jsonl", "d2", "pluviophile zymurgy")

    assert idx.search("pluviophile", limit=10)["total"] >= 1, "FTS must reflect new content"
    assert idx.search("xylophone", limit=10)["total"] == 0, "stale content must be gone"
    idx.close()
