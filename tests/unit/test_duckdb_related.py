"""DuckDbIndex.related(): lock-guarded sibling lookup for the shared singleton.

Equivalent to the module-level find_related_captures(conn, ...) but routed
through self._lock so a process-wide singleton can serve /related safely
across FastAPI's threadpool without touching the raw connection unguarded.
"""

from __future__ import annotations

import json
from pathlib import Path

from awareness.storage.duckdb_index import DuckDbIndex, find_related_captures

_FULL_KEYS = (
    "doc_id", "capture_id", "parent_doc_or_dup_group", "source_type",
    "source_name", "source_locator", "source_shard",
    "source_offset_or_record_id", "discovery_channel", "job_id", "batch_id",
    "ingest_version", "url", "canonical_url", "domain", "fetch_ts",
    "observed_ts", "published_ts", "last_modified", "content_type",
    "http_status", "etag", "title", "text", "language", "content_hash",
    "near_dup_hash", "robots_decision", "terms_note_if_relevant",
)


def _write_doc(root: Path, idx: int, *, group: str, title: str = "t", text: str = "body") -> None:
    day = root / "captures" / "2026" / "06" / "01"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}", capture_id=f"cap-{idx}", parent_doc_or_dup_group=group,
        source_type="rss", domain="example.com", url=f"https://example.com/{idx}",
        fetch_ts=f"2026-06-01T12:0{idx}:00+00:00", title=title, text=text,
    )
    (day / f"chunk-{idx}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _index(tmp_path: Path) -> DuckDbIndex:
    return DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )


def test_related_returns_siblings_in_same_group(tmp_path: Path) -> None:
    jsonl = tmp_path / "jsonl"
    _write_doc(jsonl, 0, group="grp-A")
    _write_doc(jsonl, 1, group="grp-A")     # sibling of cap-0
    _write_doc(jsonl, 2, group="grp-B")     # unrelated
    idx = _index(tmp_path)
    sibs = idx.related("cap-0", limit=12)
    ids = {r["capture_id"] for r in sibs}
    assert ids == {"cap-1"}                 # same group, excludes self, excludes other group


def test_related_matches_module_function(tmp_path: Path) -> None:
    jsonl = tmp_path / "jsonl"
    _write_doc(jsonl, 0, group="grp-A")
    _write_doc(jsonl, 1, group="grp-A")
    idx = _index(tmp_path)
    via_method = idx.related("cap-0", limit=5)
    via_func = find_related_captures(idx.connect(), "cap-0", limit=5)
    assert [r["capture_id"] for r in via_method] == [r["capture_id"] for r in via_func]


def test_related_respects_limit(tmp_path: Path) -> None:
    jsonl = tmp_path / "jsonl"
    for i in range(5):
        _write_doc(jsonl, i, group="grp-A")
    idx = _index(tmp_path)
    assert len(idx.related("cap-0", limit=2)) == 2


def test_related_unknown_capture_is_empty(tmp_path: Path) -> None:
    jsonl = tmp_path / "jsonl"
    _write_doc(jsonl, 0, group="grp-A")
    idx = _index(tmp_path)
    assert idx.related("cap-does-not-exist") == []
