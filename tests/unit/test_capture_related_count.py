"""Capture detail / related endpoints expose full related_count."""

from __future__ import annotations

import json
from pathlib import Path

from awareness.storage.duckdb_index import DuckDbIndex, count_related_captures


def _write_doc(jsonl_dir: Path, n: int, *, group: str, title: str | None = None) -> None:
    day = jsonl_dir / "captures" / "2026" / "01" / "01"
    day.mkdir(parents=True, exist_ok=True)
    path = day / "chunk.jsonl"
    row = {
        "doc_id": f"doc-{n}",
        "capture_id": f"cap-{n}",
        "source_type": "rss",
        "source_name": "fixture",
        "discovery_channel": "rss",
        "url": f"https://example.com/{n}",
        "canonical_url": f"https://example.com/{n}",
        "domain": "example.com",
        "fetch_ts": f"2026-01-01T00:0{n}:00Z",
        "observed_ts": f"2026-01-01T00:0{n}:00Z",
        "published_ts": None,
        "title": title or f"Title {n}",
        "text": f"Body text for document number {n} with enough content.",
        "language": "en",
        "content_hash": f"hash-{n}",
        "near_dup_hash": None,
        "parent_doc_or_dup_group": group,
        "robots_decision": "allow",
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def test_related_count_matches_siblings(tmp_path: Path) -> None:
    jsonl = tmp_path / "jsonl"
    for i in range(4):
        _write_doc(jsonl, i, group="grp-A")
    _write_doc(jsonl, 9, group="grp-B")  # unrelated

    idx = DuckDbIndex(db_path=tmp_path / "m.duckdb", jsonl_dir=jsonl, iceberg_warehouse=None)
    try:
        assert idx.related_count("cap-0") == 3
        sibs = idx.related("cap-0", limit=2)
        assert len(sibs) == 2  # limit truncates list
        assert idx.related_count("cap-9") == 0
        assert idx.related_count("missing") == 0
    finally:
        idx.close()


def test_count_related_captures_helper(tmp_path: Path) -> None:
    jsonl = tmp_path / "jsonl"
    for i in range(3):
        _write_doc(jsonl, i, group="g")
    idx = DuckDbIndex(db_path=tmp_path / "m.duckdb", jsonl_dir=jsonl, iceberg_warehouse=None)
    try:
        conn = idx.connect()
        assert count_related_captures(conn, "cap-1") == 2
    finally:
        idx.close()
