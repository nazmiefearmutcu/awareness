"""IDF drop-all fallback must not lie (duckdb_index.py).

When EVERY query term would be pruned below ``search_idf_threshold``, the
search falls back to the full query — and that fallback must report NO drop:
``search_with_diagnostics`` returns ``dropped_terms=[]`` /
``idf_threshold=None`` and no ``bm25f_low_idf_terms_dropped`` warning fires.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

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


def _write_doc(root: Path, idx: int, *, title: str, text: str) -> None:
    day = root / "captures" / "2026" / "06" / "01"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}",
        capture_id=f"cap-{idx}",
        source_type="rss",
        domain="example.com",
        url=f"https://example.com/{idx}",
        fetch_ts="2026-06-01T12:00:00+00:00",
        title=title,
        text=text,
    )
    (day / f"chunk-{idx}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _index(tmp_path: Path) -> DuckDbIndex:
    return DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )


def test_drop_all_fallback_reports_no_drop_and_no_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Every query term ubiquitous (present in all N >= 20 docs, IDF ~0 below
    the default threshold 1.0) → fallback to the full query, diagnostics
    report NO drop, and no operator warning is logged."""
    jsonl = tmp_path / "jsonl"
    for i in range(1, 24):
        _write_doc(jsonl, i, title="Coastal digest", text="We love coastal cities.")

    idx = _index(tmp_path)
    try:
        with caplog.at_level(logging.WARNING):
            res, diag = idx.search_with_diagnostics("coastal cities", mode="fts")

        # The fallback keeps the full query and the search still matches.
        assert res["total"] >= 1
        assert set(diag["kept_terms"]) == {"coastal", "citi"}
        # No lie: nothing was actually dropped.
        assert diag["dropped_terms"] == []
        assert diag["idf_threshold"] is None
        assert diag["mode"] == "fts"
        # No warning either — the fallback is not a drop.
        assert not any(
            rec.levelno == logging.WARNING and "bm25f_low_idf_terms_dropped" in rec.message
            for rec in caplog.records
        )
    finally:
        idx.close()


def test_partial_drop_still_warns_and_reports(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Sanity guard: when only SOME terms drop, diagnostics + warning stay."""
    jsonl = tmp_path / "jsonl"
    for i in range(1, 24):
        _write_doc(jsonl, i, title="Coastal digest", text="We love coastal cities.")
    _write_doc(jsonl, 24, title="Geology note", text="Sediment cores reveal river depth.")

    idx = _index(tmp_path)
    try:
        with caplog.at_level(logging.WARNING):
            res, diag = idx.search_with_diagnostics("coastal sediment", mode="fts")

        assert diag["dropped_terms"] == ["coastal"]
        assert diag["kept_terms"] == ["sediment"]
        assert diag["idf_threshold"] == 1.0
        assert res["total"] >= 1
        assert any(
            rec.levelno == logging.WARNING and "bm25f_low_idf_terms_dropped" in rec.message
            for rec in caplog.records
        )
    finally:
        idx.close()
