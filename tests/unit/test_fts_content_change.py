"""H-10: FTS must not serve stale content after a capture_id's text changes.

The append-only FTS path only checks row-count growth and missing
capture_ids — a capture_id whose content was re-fetched/updated keeps its id,
so naive detection would serve the OLD text forever. Content-change detection
(overlapping content_hash comparison) must force a full rebuild.
"""

from __future__ import annotations

import json
from pathlib import Path

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


def _write_chunk(jsonl_dir: Path, name: str, capture_id: str, text: str, content_hash: str) -> None:
    day = jsonl_dir / "captures" / "2026" / "06" / "08"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{capture_id}",
        capture_id=capture_id,
        url=f"http://example.test/{capture_id}",
        canonical_url=f"http://example.test/{capture_id}",
        domain="example.test",
        source_type="rss",
        title=text,
        text=text,
        language="en",
        content_hash=content_hash,
        fetch_ts="2026-06-08T00:00:00+00:00",
    )
    (day / name).write_text(json.dumps(rec) + "\n", encoding="utf-8")


def test_updated_capture_content_is_searchable(tmp_path: Path) -> None:
    jsonl_dir = tmp_path / "jsonl"
    db_path = tmp_path / "idx.duckdb"
    _write_chunk(jsonl_dir, "a.jsonl", "c1", "xylophone rambunctious", "hash-v1")

    idx = DuckDbIndex(db_path=db_path, jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    assert idx.search("xylophone", limit=10)["total"] >= 1
    assert idx._fts_full_rebuilds == 1

    # Same capture_id, NEW text/hash + one genuinely new capture appended.
    # Row count grows (1 -> 2) so the naive append path would run — the stale
    # content check must reject it and force a full rebuild.
    (jsonl_dir / "captures" / "2026" / "06" / "08" / "a.jsonl").unlink()
    _write_chunk(jsonl_dir, "a.jsonl", "c1", "pluviophile zymurgy", "hash-v2")
    _write_chunk(jsonl_dir, "b.jsonl", "c2", "quintessential quagmire", "hash-v3")

    assert idx.search("pluviophile", limit=10)["total"] >= 1, (
        "updated capture text must be searchable (H-10)"
    )
    assert idx.search("xylophone", limit=10)["total"] == 0, (
        "stale pre-update text must be gone"
    )
    assert idx.search("quintessential", limit=10)["total"] >= 1
    assert idx._fts_full_rebuilds == 2, "content change must force a full rebuild"
    assert idx._fts_incremental_appends == 0, "content change must not take the append path"

    # H-10: indexed_rows gauge tracks captures_idx, which now holds 2 rows.
    snap = idx.health_snapshot()
    assert snap["fts_built_for_count"] == 2
    idx.close()


def test_pure_append_still_uses_incremental_path(tmp_path: Path) -> None:
    """Unchanged existing rows + a new capture_id keep the append fast path."""
    jsonl_dir = tmp_path / "jsonl"
    db_path = tmp_path / "idx.duckdb"
    _write_chunk(jsonl_dir, "a.jsonl", "c1", "xylophone rambunctious", "hash-v1")

    idx = DuckDbIndex(db_path=db_path, jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    assert idx.search("xylophone", limit=10)["total"] >= 1

    _write_chunk(jsonl_dir, "b.jsonl", "c2", "pluviophile zymurgy", "hash-v2")
    assert idx.search("pluviophile", limit=10)["total"] >= 1
    assert idx._fts_incremental_appends == 1
    assert idx._fts_full_rebuilds == 1
    assert idx.health_snapshot()["fts_built_for_count"] == 2
    idx.close()
