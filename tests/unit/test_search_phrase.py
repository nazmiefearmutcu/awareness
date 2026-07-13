"""Quoted whole-query phrase search for :class:`DuckDbIndex.search`.

Wrapping the query in double quotes (e.g. ``"machine learning"``) forces
exact-phrase substring matching (``ILIKE %phrase%``) instead of tokenizing
for FTS/prefix. Unquoted multi-word queries keep their existing behavior.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from awareness.storage.duckdb_index import DuckDbIndex, _phrase_query

# The unified ``captures`` view SELECTs the full record shape; a partial
# JSONL row makes view setup fail. Mirror the production 29-field schema.
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
) -> None:
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


def test_phrase_query_helper() -> None:
    assert _phrase_query('"machine learning"') == "machine learning"
    assert _phrase_query('"  spaced  "') == "spaced"
    assert _phrase_query('""') == ""
    assert _phrase_query("machine learning") is None
    assert _phrase_query('"unbalanced') is None
    assert _phrase_query('unbalanced"') is None
    assert _phrase_query('say "hi" now') is None
    assert _phrase_query('"') is None


@pytest.fixture()
def phrase_index(tmp_path: Path) -> DuckDbIndex:
    """Corpus: contiguous phrase vs same tokens non-adjacent / reordered."""
    jsonl_dir = tmp_path / "jsonl"
    # Contiguous exact phrase in title
    _write_doc(
        jsonl_dir,
        1,
        title="Intro to machine learning systems",
        text="An overview of supervised models.",
    )
    # Same tokens present but not as the contiguous phrase
    _write_doc(
        jsonl_dir,
        2,
        title="Learning about machines in the factory",
        text="A machine needs careful learning of safety rules over time.",
    )
    # Unrelated
    _write_doc(
        jsonl_dir,
        3,
        title="Sports roundup",
        text="A football match ended in a draw.",
    )
    return DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=jsonl_dir,
        iceberg_warehouse=None,
    )


def test_quoted_phrase_requires_exact_substring(phrase_index: DuckDbIndex) -> None:
    res = phrase_index.search('"machine learning"', mode="auto")
    assert res["mode"] == "substring"
    assert res["ranked"] is False
    assert res["query"] == "machine learning"
    ids = {r["doc_id"] for r in res["rows"]}
    assert "doc-1" in ids
    assert "doc-2" not in ids
    assert res["total"] == 1


@pytest.mark.parametrize("mode", ["auto", "fts", "prefix", "substring"])
def test_quoted_phrase_early_branch_for_all_modes(
    phrase_index: DuckDbIndex, mode: str
) -> None:
    """Phrase detection is an early branch for auto/fts/prefix/substring."""
    res = phrase_index.search('"machine learning"', mode=mode)
    assert res["mode"] == "substring"
    ids = {r["doc_id"] for r in res["rows"]}
    assert ids == {"doc-1"}


def test_unquoted_multiword_unchanged(phrase_index: DuckDbIndex) -> None:
    """Without quotes, tokenization may still surface both term-bearing docs."""
    res = phrase_index.search("machine learning", mode="auto")
    # Unquoted path is not forced to substring; mode is fts or prefix.
    assert res["mode"] in ("fts", "prefix", "substring")
    ids = {r["doc_id"] for r in res["rows"]}
    # At least the contiguous doc; typically both term-bearing docs match.
    assert "doc-1" in ids
    assert "doc-3" not in ids


def test_unbalanced_quotes_do_not_force_phrase(phrase_index: DuckDbIndex) -> None:
    res = phrase_index.search('"machine learning', mode="auto")
    assert res["mode"] != "substring" or res["query"] == '"machine learning'
    # Must not treat as exact phrase of machine learning alone.
    # Query still has leading quote; may return zero or token-ish results.
    # Primary assertion: not silently rewriting to unquoted phrase mode.
    assert res["query"] == '"machine learning'


def test_quoted_phrase_matches_body_text(tmp_path: Path) -> None:
    jsonl_dir = tmp_path / "jsonl"
    _write_doc(
        jsonl_dir,
        1,
        title="Research notes",
        text="We study deep neural networks and machine learning pipelines.",
    )
    _write_doc(
        jsonl_dir,
        2,
        title="Other",
        text="machine tools require learning curves separately.",
    )
    idx = DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=jsonl_dir,
        iceberg_warehouse=None,
    )
    try:
        res = idx.search('"machine learning"', mode="fts")
        assert res["mode"] == "substring"
        assert {r["doc_id"] for r in res["rows"]} == {"doc-1"}
    finally:
        idx.close()


def test_quoted_phrase_respects_fields(tmp_path: Path) -> None:
    jsonl_dir = tmp_path / "jsonl"
    _write_doc(
        jsonl_dir,
        1,
        title="Unrelated title",
        text="Contains machine learning only in the body.",
    )
    idx = DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=jsonl_dir,
        iceberg_warehouse=None,
    )
    try:
        in_text = idx.search('"machine learning"', mode="auto", fields=["text"])
        in_title = idx.search('"machine learning"', mode="auto", fields=["title"])
        assert in_text["total"] == 1
        assert in_title["total"] == 0
    finally:
        idx.close()
