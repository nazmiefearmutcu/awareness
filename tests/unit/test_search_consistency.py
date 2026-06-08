from __future__ import annotations

import json
from pathlib import Path

from awareness.storage.duckdb_index import DuckDbIndex

# Mirror the production 29-field schema used in test_search_matching.py
_FULL_KEYS = (
    "doc_id", "capture_id", "parent_doc_or_dup_group", "source_type",
    "source_name", "source_locator", "source_shard",
    "source_offset_or_record_id", "discovery_channel", "job_id", "batch_id",
    "ingest_version", "url", "canonical_url", "domain", "fetch_ts",
    "observed_ts", "published_ts", "last_modified", "content_type",
    "http_status", "etag", "title", "text", "language", "content_hash",
    "near_dup_hash", "robots_decision", "terms_note_if_relevant",
)


def _write(jsonl_dir: Path, doc_id: str, title: str, text: str) -> None:
    day = jsonl_dir / "captures" / "2026" / "06" / "08"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=doc_id,
        capture_id=doc_id,
        url=f"http://x.test/{doc_id}",
        canonical_url=f"http://x.test/{doc_id}",
        domain="x.test",
        source_type="rss",
        title=title,
        text=text,
        language="en",
        fetch_ts="2026-06-08T00:00:00+00:00",
    )
    (day / f"{doc_id}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _idx(tmp_path: Path) -> DuckDbIndex:
    jsonl_dir = tmp_path / "jsonl"
    _write(jsonl_dir, "d1", "Bitcoin news", "bitcoin price moved")
    _write(jsonl_dir, "d2", "Ethereum update", "ethereum staking grew")
    return DuckDbIndex(db_path=tmp_path / "idx.duckdb", jsonl_dir=jsonl_dir, iceberg_warehouse=None)


def test_reversed_field_order_still_ranks(tmp_path: Path) -> None:
    idx = _idx(tmp_path)
    res = idx.search("bitcoin", mode="auto", fields=["text", "title"], limit=10)
    assert res["total"] >= 1
    assert res["ranked"] is True
    idx.close()


def test_multiword_prefix_fallback_is_or(tmp_path: Path) -> None:
    idx = _idx(tmp_path)
    res = idx.search("bitcoin ethereum", mode="prefix", limit=10)
    assert res["total"] == 2
    idx.close()
