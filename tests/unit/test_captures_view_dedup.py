"""H-12: the captures view dedups by capture_id even when Iceberg is disabled.

The no-Iceberg (default) branch previously projected staging rows straight
through, so duplicate capture_ids (re-fetches with the same id) were served
twice. The captures view must always keep the newest row per capture_id.
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


def _write(jsonl_dir: Path, name: str, capture_id: str, title: str, fetch_ts: str) -> None:
    day = jsonl_dir / "captures" / "2026" / "06" / "01"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{capture_id}",
        capture_id=capture_id,
        url=f"https://example.test/{capture_id}",
        canonical_url=f"https://example.test/{capture_id}",
        domain="example.test",
        source_type="rss",
        fetch_ts=fetch_ts,
        title=title,
        text=f"body {title}",
    )
    (day / name).write_text(json.dumps(rec) + "\n", encoding="utf-8")


def test_no_iceberg_captures_dedup_by_capture_id(tmp_path: Path) -> None:
    jsonl_dir = tmp_path / "jsonl"
    _write(jsonl_dir, "a.jsonl", "c1", "old", "2026-06-01T10:00:00+00:00")
    _write(jsonl_dir, "b.jsonl", "c1", "new", "2026-06-01T12:00:00+00:00")
    _write(jsonl_dir, "c.jsonl", "c2", "solo", "2026-06-01T11:00:00+00:00")

    idx = DuckDbIndex(db_path=tmp_path / "i.duckdb", jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    try:
        n = idx.execute("SELECT COUNT(*) AS n FROM captures")
        assert n[0]["n"] == 2, "duplicate capture_id must be deduped to one row"

        rows = idx.execute(
            "SELECT capture_id, title FROM captures ORDER BY capture_id"
        )
        by_id = {r["capture_id"]: r["title"] for r in rows}
        assert by_id["c1"] == "new", "newest fetch_ts must win"
        assert by_id["c2"] == "solo"

        # The backwards-compat alias follows the same deduped view.
        alias = idx.execute("SELECT COUNT(*) AS n FROM staging_captures")
        assert alias[0]["n"] == 2
    finally:
        idx.close()


def test_iceberg_disabled_duplicate_rows_in_raw_but_not_captures(tmp_path: Path) -> None:
    """staging_captures_raw still exposes both rows; captures hides the older."""
    jsonl_dir = tmp_path / "jsonl"
    _write(jsonl_dir, "a.jsonl", "c1", "old", "2026-06-01T10:00:00+00:00")
    _write(jsonl_dir, "b.jsonl", "c1", "new", "2026-06-01T12:00:00+00:00")

    idx = DuckDbIndex(db_path=tmp_path / "i.duckdb", jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    try:
        raw = idx.execute("SELECT COUNT(*) AS n FROM staging_captures_raw")
        assert raw[0]["n"] == 2
        dedup = idx.execute("SELECT COUNT(*) AS n FROM captures")
        assert dedup[0]["n"] == 1
    finally:
        idx.close()
