"""Unit tests for FTS Singleton, BM25F ranking, IDF threshold filtering, and FineWeb fallbacks."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from awareness.config import get_settings
from awareness.config.settings import Settings
from awareness.storage.duckdb_index import DuckDbIndex
from awareness.sources.fineweb import FineWebAdapter
from awareness.schemas.jobs import BackfillRequest
from datetime import datetime, UTC

_FULL_KEYS = (
    "doc_id", "capture_id", "parent_doc_or_dup_group", "source_type",
    "source_name", "source_locator", "source_shard",
    "source_offset_or_record_id", "discovery_channel", "job_id", "batch_id",
    "ingest_version", "url", "canonical_url", "domain", "fetch_ts",
    "observed_ts", "published_ts", "last_modified", "content_type",
    "http_status", "etag", "title", "text", "language", "content_hash",
    "near_dup_hash", "robots_decision", "terms_note_if_relevant",
)


def _write_doc(root: Path, idx: int, *, title: str, text: str, domain: str = "example.com") -> None:
    day = root / "captures" / "2026" / "06" / "01"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}",
        capture_id=f"cap-{idx}",
        source_type="rss",
        domain=domain,
        url=f"https://{domain}/{idx}",
        fetch_ts="2026-06-01T12:00:00+00:00",
        title=title,
        text=text,
    )
    (day / f"chunk-{idx}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def test_fts_singleton_behavior(tmp_path: Path) -> None:
    """Verify that DuckDbIndex instantiates as a singleton per unique db_path."""
    db_path1 = tmp_path / "db1.duckdb"
    db_path2 = tmp_path / "db2.duckdb"
    jsonl_dir = tmp_path / "jsonl"

    # Same path -> same instance
    idx1 = DuckDbIndex(db_path1, jsonl_dir, None)
    idx2 = DuckDbIndex(db_path1, jsonl_dir, None)
    assert idx1 is idx2

    # Different path -> different instance
    idx3 = DuckDbIndex(db_path2, jsonl_dir, None)
    assert idx1 is not idx3

    # Closing removes from cache
    idx1.close()
    idx4 = DuckDbIndex(db_path1, jsonl_dir, None)
    assert idx1 is not idx4
    idx4.close()
    idx3.close()


def test_bm25f_ranking_title_vs_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify BM25F weights matches in title higher than matches in text/body."""
    # Temporarily set IDF threshold to 0 to prevent filtering
    monkeypatch.setattr(get_settings(), "search_idf_threshold", 0.0)

    jsonl_dir = tmp_path / "jsonl"
    db_path = tmp_path / "duckdb" / "metadata.duckdb"

    # Doc 1 has "apple" in the title
    _write_doc(jsonl_dir, 1, title="Fresh apples are delicious", text="Eating fruit is healthy.")
    # Doc 2 has "apple" in the text/body only
    _write_doc(jsonl_dir, 2, title="Healthy fruit eating", text="Apples are delicious fresh fruit.")

    idx = DuckDbIndex(db_path, jsonl_dir, None)
    try:
        res = idx.search("apples", mode="fts")
        assert res["total"] == 2
        # Doc 1 (match in title) must be ranked first because of higher weight
        assert res["rows"][0]["doc_id"] == "doc-1"
        assert res["rows"][1]["doc_id"] == "doc-2"
        # Scores should reflect the ranking difference
        assert res["rows"][0]["score"] > res["rows"][1]["score"]
    finally:
        idx.close()




def test_bm25_avg_lengths_memoized_per_signature(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Avg field lengths are computed once per views_signature, not per search."""
    monkeypatch.setattr(get_settings(), "search_idf_threshold", 0.0)

    jsonl_dir = tmp_path / "jsonl"
    db_path = tmp_path / "duckdb" / "metadata.duckdb"
    _write_doc(jsonl_dir, 1, title="Fresh apples are delicious", text="Eating fruit is healthy.")
    _write_doc(jsonl_dir, 2, title="Healthy fruit eating", text="Apples are delicious fresh fruit.")

    idx = DuckDbIndex(db_path, jsonl_dir, None)
    try:
        assert idx._bm25_avg_lengths is None
        res1 = idx.search("apples", mode="fts")
        assert res1["total"] == 2
        assert res1["rows"][0]["doc_id"] == "doc-1"
        assert res1["rows"][0]["score"] > res1["rows"][1]["score"]
        cached = idx._bm25_avg_lengths
        sig = idx._bm25_avg_lengths_signature
        assert cached is not None
        assert sig is not None
        assert sig == idx._views_signature
        assert cached[0] >= 1.0 and cached[1] >= 1.0

        # Second search with unchanged corpus must reuse the cache object.
        res2 = idx.search("apples", mode="fts")
        assert res2["rows"][0]["doc_id"] == "doc-1"
        assert idx._bm25_avg_lengths is cached
        assert idx._bm25_avg_lengths_signature is sig

        # Corpus change invalidates FTS + avg-length memoization.
        _write_doc(jsonl_dir, 3, title="Bananas forever", text="Yellow fruit only.")
        res3 = idx.search("apples", mode="fts")
        assert res3["total"] == 2
        assert res3["rows"][0]["doc_id"] == "doc-1"
        assert idx._bm25_avg_lengths is not None
        assert idx._bm25_avg_lengths_signature == idx._views_signature
        assert idx._bm25_avg_lengths_signature != sig
    finally:
        idx.close()

def test_idf_threshold_filtering(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Verify terms with low IDF are filtered, and logged."""
    jsonl_dir = tmp_path / "jsonl"
    db_path = tmp_path / "duckdb" / "metadata.duckdb"

    # Need N >= 20 so IDF pruning is enabled (tiny corpora skip the filter).
    # "apples" is common (low IDF); "mango" is rare (high IDF).
    for i in range(1, 25):
        _write_doc(jsonl_dir, i, title="Fruit post", text="We love eating apples.")
    _write_doc(jsonl_dir, 25, title="Special fruit post", text="We love eating mango.")

    idx = DuckDbIndex(db_path, jsonl_dir, None)
    try:
        # High IDF threshold: should drop "apples" but keep "mango"
        with patch("awareness.config.get_settings") as mock_settings:
            mock_s = MagicMock()
            mock_s.search_idf_threshold = 1.2  # high enough to drop apples
            mock_settings.return_value = mock_s

            with caplog.at_level(logging.INFO):
                res = idx.search("apples mango", mode="fts")
                # Assert warning / info log was emitted
                assert any("bm25f_low_idf_terms_dropped" in rec.message for rec in caplog.records)
                # Query still returns the rare-term document.
                assert res["total"] >= 1
    finally:
        idx.close()


def test_fineweb_crawl_id_fallback() -> None:
    """Verify that FineWeb planning validates configs and falls back gracefully."""
    pytest.importorskip("datasets")
    adapter = FineWebAdapter()
    
    # 2024 June range translates to CC-MAIN-2024-26 (from crawl_ids_for_range)
    req = BackfillRequest(
        start=datetime(2024, 6, 1, tzinfo=UTC),
        end=datetime(2024, 6, 14, tzinfo=UTC),
    )

    # 1. Mock get_dataset_config_names returning matching configs
    with patch("datasets.get_dataset_config_names", return_value=["CC-MAIN-2024-21", "CC-MAIN-2024-23", "sample-10BT"]) as mock_get:
        partitions = adapter.plan(req)
        assert len(partitions) == 2
        assert partitions[0].payload["dump"] == "CC-MAIN-2024-21"
        assert partitions[1].payload["dump"] == "CC-MAIN-2024-23"
        mock_get.assert_called_once_with("HuggingFaceFW/fineweb")

    # 2. Mock get_dataset_config_names where requested dumps are missing, triggering fallback to sample-10BT
    with patch("datasets.get_dataset_config_names", return_value=["CC-MAIN-2024-10", "sample-10BT"]):
        partitions = adapter.plan(req)
        assert len(partitions) == 1
        assert partitions[0].payload["dump"] == "sample-10BT"
        assert partitions[0].partition_key == "HuggingFaceFW/fineweb:sample-10BT"
