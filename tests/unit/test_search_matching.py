"""Search matching modes + bounds for :class:`DuckDbIndex.search`.

Regression coverage for the "finance returns nothing" bug: the Snowball
english stemmer reduces ``finance`` -> ``financ`` but ``financial`` ->
``financi``. A corpus that only contains "financial" therefore yields
zero FTS hits for the query "finance". The ``auto``/``prefix`` matching
modes close that gap by falling back to stem-root prefix matching.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from awareness.storage.duckdb_index import DuckDbIndex

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


@pytest.fixture()
def index(tmp_path: Path) -> DuckDbIndex:
    jsonl_dir = tmp_path / "jsonl"
    # The corpus contains "financial" but never the bare word "finance".
    _write_doc(jsonl_dir, 1, title="Global financial markets rally", text="The financial sector surged today.")
    _write_doc(jsonl_dir, 2, title="Sports roundup", text="A football match ended in a draw.")
    return DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=jsonl_dir,
        iceberg_warehouse=None,
    )


def test_fts_stemming_gap_is_real(index: DuckDbIndex) -> None:
    """Pure FTS misses 'finance' against a 'financial'-only corpus."""
    res = index.search("finance", mode="fts")
    assert res["total"] == 0


def test_auto_mode_recovers_stemmed_variant(index: DuckDbIndex) -> None:
    """auto = FTS first, then stem-prefix fallback -> finds 'financial'."""
    res = index.search("finance", mode="auto")
    assert res["total"] >= 1
    assert res["mode"] == "prefix"  # records that the fallback fired
    assert res["rows"][0]["domain"] == "example.com"


def test_prefix_mode_matches_word_family(index: DuckDbIndex) -> None:
    res = index.search("finance", mode="prefix")
    assert res["total"] >= 1


def test_fts_still_ranks_when_term_present(index: DuckDbIndex) -> None:
    """A query that IS in the corpus stays on the ranked FTS path."""
    res = index.search("financial", mode="auto")
    assert res["total"] >= 1
    assert res["mode"] == "fts"
    assert res["ranked"] is True


def test_fields_restriction(index: DuckDbIndex) -> None:
    """Restricting to title only must not match a body-only term."""
    # 'sector' lives only in doc 1's text, not any title.
    only_text = index.search("sector", mode="prefix", fields=["text"])
    only_title = index.search("sector", mode="prefix", fields=["title"])
    assert only_text["total"] >= 1
    assert only_title["total"] == 0


def test_auto_mode_honors_narrowed_fields(index: DuckDbIndex) -> None:
    """A field-restricted query must not silently ride the field-agnostic
    FTS path. 'sector' is in doc 1's body only — restricting to title=0."""
    body = index.search("sector", mode="auto", fields=["text"])
    title_only = index.search("sector", mode="auto", fields=["title"])
    assert body["total"] >= 1
    assert title_only["total"] == 0
    assert title_only["mode"] == "prefix"  # routed off FTS to honor fields


def test_max_results_caps_returned_rows(tmp_path: Path) -> None:
    jsonl_dir = tmp_path / "jsonl"
    for i in range(8):
        _write_doc(jsonl_dir, i, title=f"Financial report {i}", text="financial financial")
    idx = DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=jsonl_dir,
        iceberg_warehouse=None,
    )
    res = idx.search("financial", mode="auto", limit=100, max_results=3)
    assert len(res["rows"]) <= 3
