"""Search-quality surface: IDF term-drop diagnostics, recency-boost wiring,
and tokenizer LRU effectiveness.

Covers the two benchmark-report follow-ups (docs/benchmarks/
benchmark_report_2026-08-04.md):

1. ``search_with_diagnostics`` exposes low-IDF term pruning that
   ``search()`` previously only logged (silent "coastal sediment" →
   "sediment" narrowing).
2. ``search_recency_boost`` must actually re-rank fresh docs above
   identical-content old docs (and be a no-op at the default 0.0).
3. The ``876dbc6`` tokenizer LRU caches must be hit on repeat searches.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from awareness.config import get_settings
from awareness.storage.duckdb_index import (
    DuckDbIndex,
    _domain_labels,
    _lead_window_tokens,
    _title_tokens,
    _url_slug_tokens,
    _url_token_blob,
)

_FULL_KEYS = (
    "doc_id", "capture_id", "parent_doc_or_dup_group", "source_type",
    "source_name", "source_locator", "source_shard",
    "source_offset_or_record_id", "discovery_channel", "job_id", "batch_id",
    "ingest_version", "url", "canonical_url", "domain", "fetch_ts",
    "observed_ts", "published_ts", "last_modified", "content_type",
    "http_status", "etag", "title", "text", "language", "content_hash",
    "near_dup_hash", "robots_decision", "terms_note_if_relevant",
)


def _write_doc(
    root: Path,
    idx: int,
    *,
    title: str,
    text: str,
    domain: str = "example.com",
    published_ts: str | None = None,
    parent_group: str | None = None,
) -> None:
    day = root / "captures" / "2026" / "06" / "01"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}", capture_id=f"cap-{idx}", source_type="rss",
        domain=domain, url=f"https://{domain}/{idx}",
        fetch_ts="2026-06-01T12:00:00+00:00", title=title, text=text,
    )
    if published_ts is not None:
        rec["published_ts"] = published_ts
    if parent_group is not None:
        rec["parent_doc_or_dup_group"] = parent_group
    (day / f"chunk-{idx}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _index(tmp_path: Path) -> DuckDbIndex:
    return DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )


# ── 1) IDF term-drop surface ────────────────────────────────────────────


def test_low_idf_term_drop_reported_in_diagnostics(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """"coastal sediment" on a corpus where coastal is ubiquitous → "coastal"
    is pruned, reported in diagnostics, logged as a warning, and the search
    still works on the surviving term."""
    jsonl = tmp_path / "jsonl"
    # N >= 20 enables IDF pruning. "coastal" in every doc → IDF ~0; "sediment"
    # in one doc → high IDF. Default threshold 1.0 drops only "coastal".
    for i in range(1, 24):
        _write_doc(jsonl, i, title="Coastal digest", text="We love coastal cities.")
    _write_doc(jsonl, 24, title="Geology note", text="Sediment cores reveal river depth.")

    idx = _index(tmp_path)
    try:
        with caplog.at_level(logging.WARNING):
            res, diag = idx.search_with_diagnostics("coastal sediment", mode="fts")

        # Diagnostics surface the narrowed query explicitly.
        assert diag["dropped_terms"] == ["coastal"]
        assert diag["kept_terms"] == ["sediment"]
        assert diag["mode"] == "fts"
        assert diag["idf_threshold"] == 1.0

        # The search itself still works (and still uses the surviving term).
        assert res["total"] >= 1
        assert res["rows"][0]["capture_id"] == "cap-24"

        # Operators see a warning, not an info-only hint.
        assert any(
            rec.levelno == logging.WARNING and "bm25f_low_idf_terms_dropped" in rec.message
            for rec in caplog.records
        )
    finally:
        idx.close()


def test_search_payload_unchanged_when_terms_dropped(tmp_path: Path) -> None:
    """search() keeps its public payload shape; diagnostics stay off-payload."""
    jsonl = tmp_path / "jsonl"
    for i in range(1, 24):
        _write_doc(jsonl, i, title="Coastal digest", text="We love coastal cities.")
    _write_doc(jsonl, 24, title="Geology note", text="Sediment cores reveal river depth.")

    idx = _index(tmp_path)
    try:
        res = idx.search("coastal sediment", mode="fts")
        assert "dropped_terms" not in res
        assert "kept_terms" not in res
        assert "idf_threshold" not in res
        assert res["rows"][0]["capture_id"] == "cap-24"
    finally:
        idx.close()


def test_no_drop_when_threshold_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """threshold <= 0 disables pruning entirely → empty drop diagnostics."""
    monkeypatch.setattr(get_settings(), "search_idf_threshold", 0.0)
    jsonl = tmp_path / "jsonl"
    for i in range(1, 24):
        _write_doc(jsonl, i, title="Coastal digest", text="We love coastal cities.")
    _write_doc(jsonl, 24, title="Geology note", text="Sediment cores reveal river depth.")

    idx = _index(tmp_path)
    try:
        res, diag = idx.search_with_diagnostics("coastal sediment", mode="fts")
        assert diag["dropped_terms"] == []
        assert set(diag["kept_terms"]) == {"coastal", "sediment"}
        assert diag["mode"] == "fts"
        assert diag["idf_threshold"] is None
        # Both terms kept → every doc matches; the 23 same-title "Coastal
        # digest" rows collapse to one unique content, so total == 2.
        assert res["total"] == 2
        assert "cap-24" in [r["capture_id"] for r in res["rows"]]
    finally:
        idx.close()


def test_diagnostics_empty_on_non_fts_paths(tmp_path: Path) -> None:
    """Prefix/substring paths never prune → dropped empty, threshold None."""
    jsonl = tmp_path / "jsonl"
    _write_doc(jsonl, 1, title="Bitcoin rally", text="bitcoin is a cryptocurrency")

    idx = _index(tmp_path)
    try:
        res, diag = idx.search_with_diagnostics("bitcoin", mode="prefix")
        assert res["mode"] == "prefix"
        assert diag["dropped_terms"] == []
        assert diag["kept_terms"] == []
        assert diag["mode"] == "prefix"
        assert diag["idf_threshold"] is None
    finally:
        idx.close()


def test_search_with_diagnostics_empty_query(tmp_path: Path) -> None:
    """Empty query: payload equals search() and diagnostics are empty."""
    idx = _index(tmp_path)
    try:
        res, diag = idx.search_with_diagnostics("")
        assert res["rows"] == [] and res["total"] == 0
        assert diag["dropped_terms"] == []
        assert diag["kept_terms"] == []
        assert diag["idf_threshold"] is None
        assert diag["mode"] == res["mode"]
        assert idx.search("") == res
    finally:
        idx.close()


# ── 2) recency-boost wiring (fresh above identical old; no-op at 0) ─────


def _write_recency_pair(jsonl: Path) -> None:
    """Two docs with byte-identical title+text (identical BM25) but distinct
    dup groups and published times. cap-0 is old, cap-1 is fresh."""
    _write_doc(
        jsonl, 0, title="Bitcoin rally", text="bitcoin is a cryptocurrency",
        published_ts="2026-01-01T00:00:00+00:00", parent_group="g-old",
    )
    _write_doc(
        jsonl, 1, title="Bitcoin rally", text="bitcoin is a cryptocurrency",
        published_ts="2026-06-01T12:00:00+00:00", parent_group="g-new",
    )


def test_recency_boost_ranks_fresh_doc_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With search_recency_boost > 0 the fresh doc outranks its byte-identical
    older twin (recency prior from published_ts)."""
    monkeypatch.setattr(get_settings(), "search_recency_boost", 0.35)
    jsonl = tmp_path / "jsonl"
    _write_recency_pair(jsonl)

    idx = _index(tmp_path)
    try:
        res, diag = idx.search_with_diagnostics("bitcoin", mode="fts")
        assert [r["capture_id"] for r in res["rows"]] == ["cap-1", "cap-0"]
        assert res.get("recency_boost") == 0.35
        # Tiny corpus → no IDF pruning interfered with the tie-break.
        assert diag["dropped_terms"] == []
    finally:
        idx.close()


def test_recency_boost_zero_keeps_bm25_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default boost 0.0: recency is a no-op — ties keep BM25 order
    (identical scores → capture_id ASC), i.e. behavior is unchanged."""
    monkeypatch.setattr(get_settings(), "search_recency_boost", 0.0)
    jsonl = tmp_path / "jsonl"
    _write_recency_pair(jsonl)

    idx = _index(tmp_path)
    try:
        res = idx.search("bitcoin", mode="fts")
        assert [r["capture_id"] for r in res["rows"]] == ["cap-0", "cap-1"]
        assert "recency_boost" not in res
    finally:
        idx.close()


# ── 3) tokenizer LRU effectiveness (876dbc6) ────────────────────────────


@pytest.mark.parametrize(
    "fn,args",
    [
        (_title_tokens, ("Bitcoin rally pumps higher",)),
        (_lead_window_tokens, ("bitcoin rally pumps higher",)),
        (_url_token_blob, ("https://example.com/bitcoin-price", "example.com")),
        (_url_slug_tokens, ("https://example.com/bitcoin-price",)),
        (_domain_labels, ("news.bbc.co.uk",)),
    ],
)
def test_rerank_tokenizers_lru_cached(fn: Any, args: tuple[Any, ...]) -> None:
    """Repeat tokenization of the same string is served from the LRU cache."""
    fn.cache_clear()
    try:
        first = fn(*args)
        assert fn.cache_info().misses == 1
        second = fn(*args)
        assert second == first
        assert fn.cache_info().hits == 1
        assert fn.cache_info().misses == 1  # no re-tokenization on repeat
    finally:
        fn.cache_clear()


def test_search_reuses_tokenizer_cache_across_calls(tmp_path: Path) -> None:
    """Two consecutive searches over the same corpus must hit the memoized
    tokenizers instead of re-regexing every candidate."""
    jsonl = tmp_path / "jsonl"
    _write_doc(jsonl, 1, title="Bitcoin rally", text="bitcoin is a cryptocurrency")
    _write_doc(jsonl, 2, title="Market roundup", text="the market moved on bitcoin news")

    _title_tokens.cache_clear()
    idx = _index(tmp_path)
    try:
        idx.search("bitcoin", mode="fts")
        hits_after_first = _title_tokens.cache_info().hits
        misses_after_first = _title_tokens.cache_info().misses
        assert misses_after_first > 0  # first pass actually tokenized titles

        idx.search("bitcoin", mode="fts")
        info = _title_tokens.cache_info()
        assert info.hits > hits_after_first  # second pass reused cached tokens
        assert info.misses == misses_after_first  # …and re-tokenized nothing new
    finally:
        idx.close()
        _title_tokens.cache_clear()
