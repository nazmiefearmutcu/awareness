from __future__ import annotations

import gzip
import json

from awareness.storage.duckdb_index import DuckDbIndex


def _write_gz_chunk(jsonl_dir, row: dict) -> None:
    day = jsonl_dir / "captures" / "2026" / "06" / "08"
    day.mkdir(parents=True, exist_ok=True)
    with gzip.open(day / "chunk-0001.jsonl.gz", "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def test_gz_chunks_are_indexed(tmp_path) -> None:
    jsonl_dir = tmp_path / "jsonl"
    _write_gz_chunk(
        jsonl_dir,
        {
            "doc_id": "d1",
            "capture_id": "c1",
            "url": "http://example.test/a",
            "canonical_url": "http://example.test/a",
            "domain": "example.test",
            "title": "Bitcoin rally",
            "text": "bitcoin surged today",
            "language": "en",
            "fetch_ts": "2026-06-08T00:00:00+00:00",
            "parent_doc_or_dup_group": None,
            "source_type": None,
            "source_name": None,
            "source_locator": None,
            "source_shard": None,
            "source_offset_or_record_id": None,
            "discovery_channel": None,
            "job_id": None,
            "batch_id": None,
            "ingest_version": None,
            "observed_ts": None,
            "published_ts": None,
            "last_modified": None,
            "content_type": None,
            "http_status": None,
            "etag": None,
            "content_hash": None,
            "near_dup_hash": None,
            "robots_decision": None,
            "terms_note_if_relevant": None,
        },
    )
    idx = DuckDbIndex(db_path=tmp_path / "idx.duckdb", jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    rows = idx.execute("SELECT count(*) AS n FROM captures")
    assert rows[0]["n"] == 1
    res = idx.search("bitcoin", limit=10)
    assert res["total"] >= 1
    idx.close()
