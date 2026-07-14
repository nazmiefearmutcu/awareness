"""Empty-result search diagnostics for DuckDbIndex.search."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from awareness.storage.duckdb_index import DuckDbIndex, build_search_diagnostics

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


def test_empty_corpus_hint_about_no_documents(tmp_path: Path) -> None:
    """Zero captures → diagnostics with a backfill/tail hint."""
    jsonl_dir = tmp_path / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    idx = DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=jsonl_dir,
        iceberg_warehouse=None,
    )
    try:
        res = idx.search("anything", mode="auto")
        assert res["total"] == 0
        assert res["rows"] == []
        diag = res["diagnostics"]
        assert diag["corpus_size"] == 0
        assert diag["mode_used"]
        assert isinstance(diag["fts_available"], bool)
        assert diag["query_terms"] == ["anything"]
        assert any("No documents in index yet" in h for h in diag["hints"])
    finally:
        idx.close()


def test_nonempty_no_match_still_returns_diagnostics_with_mode(tmp_path: Path) -> None:
    """Corpus has docs but query misses → diagnostics still present with mode."""
    jsonl_dir = tmp_path / "jsonl"
    _write_doc(jsonl_dir, 1, title="Sports roundup", text="A football match ended in a draw.")
    idx = DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=jsonl_dir,
        iceberg_warehouse=None,
    )
    try:
        res = idx.search("quantum chromodynamics", mode="auto")
        assert res["total"] == 0
        diag = res["diagnostics"]
        assert diag["corpus_size"] >= 1
        assert diag["mode_used"] in ("auto", "fts", "prefix", "substring")
        assert isinstance(diag["fts_available"], bool)
        assert "quantum" in diag["query_terms"]
        # Non-empty corpus: offer a recall tip, not the empty-index tip.
        assert not any("No documents in index yet" in h for h in diag["hints"])
        assert any("substring" in h.lower() or "fewer terms" in h.lower() for h in diag["hints"])
    finally:
        idx.close()


def test_successful_search_omits_heavy_diagnostics(tmp_path: Path) -> None:
    """Hits path stays lean — no diagnostics/corpus_size when total > 0."""
    jsonl_dir = tmp_path / "jsonl"
    _write_doc(jsonl_dir, 1, title="Global financial markets", text="Markets rallied today.")
    idx = DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=jsonl_dir,
        iceberg_warehouse=None,
    )
    try:
        res = idx.search("financial", mode="auto")
        assert res["total"] >= 1
        assert "diagnostics" not in res
    finally:
        idx.close()


def test_build_search_diagnostics_date_window_hint() -> None:
    diag = build_search_diagnostics(
        mode_used="prefix",
        fts_available=True,
        query_terms=["alpha"],
        corpus_size=10,
        start="2026-01-01",
        end="2026-06-01",
    )
    assert diag["window"]["start"] == "2026-01-01"
    assert any("Date window" in h for h in diag["hints"])


def test_build_search_diagnostics_fts_unavailable() -> None:
    diag = build_search_diagnostics(
        mode_used="prefix",
        fts_available=False,
        query_terms=["alpha"],
        corpus_size=5,
        requested_mode="auto",
    )
    assert diag["fts_available"] is False
    assert any("FTS unavailable" in h for h in diag["hints"])

def test_build_search_diagnostics_substring_single_term_has_hint() -> None:
    """Zero-hit substring + single term must still yield a non-empty hint list."""
    diag = build_search_diagnostics(
        mode_used="substring",
        fts_available=True,
        query_terms=["quantum"],
        corpus_size=3,
    )
    assert diag["hints"], "empty hints break CLI/API empty-state UX"
    assert any("substring" in h.lower() or "terms" in h.lower() for h in diag["hints"])


def test_build_search_diagnostics_phrase_mode() -> None:
    """Quoted phrase misses should not suggest substring (phrase already uses it)."""
    diag = build_search_diagnostics(
        mode_used="phrase",
        fts_available=True,
        query_terms=["machine", "learning"],
        corpus_size=10,
        requested_mode="substring",
    )
    assert diag["mode_used"] == "phrase"
    assert any("exact phrase" in h.lower() or "without quotes" in h.lower() for h in diag["hints"])
    assert not any("substring mode" in h.lower() for h in diag["hints"])


def test_phrase_search_empty_diagnostics_mode(tmp_path: Path) -> None:
    """Zero-hit quoted query surfaces mode=phrase + phrase-specific hints."""
    jsonl_dir = tmp_path / "jsonl"
    _write_doc(jsonl_dir, 1, title="Sports roundup", text="A football match ended in a draw.")
    idx = DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=jsonl_dir,
        iceberg_warehouse=None,
    )
    try:
        res = idx.search('"quantum chromodynamics"', mode="auto")
        assert res["total"] == 0
        assert res["mode"] == "phrase"
        diag = res["diagnostics"]
        assert diag["mode_used"] == "phrase"
        assert any("phrase" in h.lower() or "quotes" in h.lower() for h in diag["hints"])
    finally:
        idx.close()


def test_build_search_diagnostics_includes_domain_filter() -> None:
    """Active domain/source filters are first-class fields, not only hint text."""
    diag = build_search_diagnostics(
        mode_used="prefix",
        fts_available=True,
        query_terms=["alpha"],
        corpus_size=10,
        domain="news.example",
        source="rss",
    )
    assert diag["filters"] == {"domain": "news.example", "source": "rss"}
    assert any("domain" in h.lower() for h in diag["hints"])


def test_build_search_diagnostics_includes_language_filter() -> None:
    """Language filter is first-class in diagnostics.filters and filter hints."""
    diag = build_search_diagnostics(
        mode_used="prefix",
        fts_available=True,
        query_terms=["alpha"],
        corpus_size=10,
        language="tr",
    )
    assert diag["filters"] == {"language": "tr"}
    assert any("language" in h.lower() for h in diag["hints"])


def test_empty_search_with_domain_surfaces_filters(tmp_path: Path) -> None:
    """Zero-hit search with --domain carries filters.domain in diagnostics."""
    jsonl_dir = tmp_path / "jsonl"
    _write_doc(jsonl_dir, 1, title="Sports roundup", text="A football match ended in a draw.", domain="other.example")
    idx = DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=jsonl_dir,
        iceberg_warehouse=None,
    )
    try:
        res = idx.search("football", mode="auto", domain="news.example")
        assert res["total"] == 0
        diag = res["diagnostics"]
        assert diag["filters"]["domain"] == "news.example"
        assert any("domain" in h.lower() for h in diag["hints"])
    finally:
        idx.close()
