"""Tests for awareness export helper (JSONL dump + optional unique fold)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from awareness.cli.export_util import (
    export_fold_key_sql,
    query_export_captures,
    write_export_jsonl,
)
from awareness.storage.duckdb_index import DuckDbIndex

_FULL_KEYS = (
    "doc_id",
    "capture_id",
    "parent_doc_or_dup_group",
    "source_type",
    "source_name",
    "source_locator",
    "source_shard",
    "source_offset_or_record_id",
    "discovery_channel",
    "job_id",
    "batch_id",
    "ingest_version",
    "url",
    "canonical_url",
    "domain",
    "fetch_ts",
    "observed_ts",
    "published_ts",
    "last_modified",
    "content_type",
    "http_status",
    "etag",
    "title",
    "text",
    "language",
    "content_hash",
    "near_dup_hash",
    "robots_decision",
    "terms_note_if_relevant",
)


def _write(
    root: Path,
    *,
    idx: int,
    capture_id: str,
    content_hash: str | None,
    parent: str | None,
    fetch_ts: str,
    title: str,
    domain: str = "example.com",
    source_type: str = "rss",
    day: str = "2026/06/01",
) -> None:
    day_dir = root / "captures" / day
    day_dir.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}",
        capture_id=capture_id,
        parent_doc_or_dup_group=parent,
        source_type=source_type,
        domain=domain,
        url=f"https://{domain}/{capture_id}",
        canonical_url=f"https://{domain}/{capture_id}",
        fetch_ts=fetch_ts,
        title=title,
        text=f"body for {title}",
        content_hash=content_hash,
        language="en",
    )
    (day_dir / f"{capture_id}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _index(tmp_path: Path) -> DuckDbIndex:
    jsonl = tmp_path / "jsonl"
    _write(
        jsonl,
        idx=1,
        capture_id="c-old-a",
        content_hash="h-a",
        parent="g1",
        fetch_ts="2026-06-01T10:00:00+00:00",
        title="A old",
    )
    _write(
        jsonl,
        idx=2,
        capture_id="c-new-a",
        content_hash="h-a",
        parent="g1",
        fetch_ts="2026-06-01T12:00:00+00:00",
        title="A new",
    )
    _write(
        jsonl,
        idx=3,
        capture_id="c-near",
        content_hash="h-b",
        parent="g1",
        fetch_ts="2026-06-01T13:00:00+00:00",
        title="A near",
    )
    _write(
        jsonl,
        idx=4,
        capture_id="c-other",
        content_hash="h-c",
        parent=None,
        fetch_ts="2026-06-01T11:00:00+00:00",
        title="Other",
        domain="other.com",
        source_type="gdelt",
    )
    return DuckDbIndex(
        db_path=tmp_path / "export.duckdb",
        jsonl_dir=jsonl,
        iceberg_warehouse=None,
    )


def test_export_fold_key_sql_modes() -> None:
    assert export_fold_key_sql("none") is None
    assert "content_hash" in (export_fold_key_sql("content") or "")
    assert "parent_doc_or_dup_group" in (export_fold_key_sql("group") or "")
    with pytest.raises(ValueError):
        export_fold_key_sql("bogus")


def test_query_export_captures_limit(tmp_path: Path) -> None:
    idx = _index(tmp_path)
    try:
        rows = query_export_captures(idx, limit=2, unique="none")
        assert len(rows) == 2
        # Newest first
        assert rows[0]["capture_id"] == "c-near"
    finally:
        idx.close()


def test_query_export_unique_content(tmp_path: Path) -> None:
    idx = _index(tmp_path)
    try:
        rows = query_export_captures(idx, limit=100, unique="content")
        # h-a x2 → 1, h-b, h-c → 3
        assert len(rows) == 3
        ids = {r["capture_id"] for r in rows}
        assert "c-new-a" in ids  # newest for h-a
        assert "c-old-a" not in ids
        assert "c-near" in ids
        assert "c-other" in ids
    finally:
        idx.close()


def test_query_export_unique_group(tmp_path: Path) -> None:
    idx = _index(tmp_path)
    try:
        rows = query_export_captures(idx, limit=100, unique="group")
        # g1 (3 rows) → 1 newest (c-near), c-other alone → 2
        assert len(rows) == 2
        ids = {r["capture_id"] for r in rows}
        assert "c-near" in ids
        assert "c-other" in ids
    finally:
        idx.close()


def test_query_export_domain_filter(tmp_path: Path) -> None:
    idx = _index(tmp_path)
    try:
        rows = query_export_captures(idx, limit=100, domain="other.com")
        assert len(rows) == 1
        assert rows[0]["capture_id"] == "c-other"
    finally:
        idx.close()


def test_query_export_source_filter_case_insensitive(tmp_path: Path) -> None:
    """export --source matches regardless of RSS vs rss casing."""
    idx = _index(tmp_path)
    try:
        upper = query_export_captures(idx, limit=100, source="GDELT")
        lower = query_export_captures(idx, limit=100, source="gdelt")
        mixed = query_export_captures(idx, limit=100, source="Rss")
        assert len(upper) == len(lower) == 1
        assert upper[0]["capture_id"] == "c-other"
        assert str(upper[0]["source_type"]).lower() == "gdelt"
        assert len(mixed) == 3  # three rss rows in fixture
        assert all(str(r["source_type"]).lower() == "rss" for r in mixed)
    finally:
        idx.close()


def test_query_export_domain_filter_case_insensitive(tmp_path: Path) -> None:
    """export --domain matches Example.COM vs example.com."""
    idx = _index(tmp_path)
    try:
        rows = query_export_captures(idx, limit=100, domain="Other.COM")
        assert len(rows) == 1
        assert rows[0]["capture_id"] == "c-other"
    finally:
        idx.close()


def test_write_export_jsonl(tmp_path: Path) -> None:
    out = tmp_path / "out" / "caps.jsonl"
    rows = [
        {"capture_id": "a", "title": "t1", "fetch_ts": "2026-01-01"},
        {"capture_id": "b", "title": "t2", "fetch_ts": "2026-01-02"},
    ]
    n = write_export_jsonl(out, rows)
    assert n == 2
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["capture_id"] == "a"
    assert json.loads(lines[1])["title"] == "t2"
