"""Integration tests: the FTS path retrieves top-K by BM25, re-ranks, then
slices the page. These assert wiring + invariants (not DuckDB's exact BM25
magnitudes — exact scoring is covered by tests/unit/test_search_rerank.py)."""

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


def _write_doc(root: Path, idx: int, *, title: str, text: str, domain: str = "example.com") -> None:
    day = root / "captures" / "2026" / "06" / "01"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}", capture_id=f"cap-{idx}", source_type="rss",
        domain=domain, url=f"https://{domain}/{idx}",
        fetch_ts="2026-06-01T12:00:00+00:00", title=title, text=text,
    )
    (day / f"chunk-{idx}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _index(tmp_path: Path) -> DuckDbIndex:
    return DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )


def test_title_match_ranks_first_on_fts_path(tmp_path: Path) -> None:
    jsonl = tmp_path / "jsonl"
    # cap-0: "bitcoin" in BOTH title and body. cap-1: body-only mention.
    # cap-2: no match. The title doc must lead and the no-match doc be absent.
    _write_doc(jsonl, 0, title="Bitcoin rally", text="bitcoin is a cryptocurrency")
    _write_doc(jsonl, 1, title="Market roundup", text="the market moved on bitcoin news")
    _write_doc(jsonl, 2, title="Sports", text="a football match ended in a draw")
    res = _index(tmp_path).search("bitcoin", mode="auto")
    assert res["mode"] == "fts" and res["ranked"] is True
    assert res["total"] == 2
    ids = [r["capture_id"] for r in res["rows"]]
    assert ids[0] == "cap-0"          # title hit surfaces first
    assert set(ids) == {"cap-0", "cap-1"}  # the non-matching doc is excluded


def test_rerank_pagination_slices_after_reorder(tmp_path: Path) -> None:
    jsonl = tmp_path / "jsonl"
    # cap-1 has NO title hit but mentions "bitcoin" many times, so raw BM25
    # would order it first; only the title-aware re-rank puts cap-0 ahead.
    _write_doc(jsonl, 0, title="Bitcoin guide", text="bitcoin explained simply")
    _write_doc(
        jsonl, 1, title="News",
        text="bitcoin bitcoin bitcoin bitcoin bitcoin mentioned many times here",
    )
    idx = _index(tmp_path)
    page0 = idx.search("bitcoin", mode="auto", limit=1, offset=0)
    page1 = idx.search("bitcoin", mode="auto", limit=1, offset=1)
    assert page0["total"] == 2 and page1["total"] == 2
    assert len(page0["rows"]) == 1 and len(page1["rows"]) == 1
    assert page0["rows"][0]["capture_id"] == "cap-0"          # title doc first
    assert page0["rows"][0]["capture_id"] != page1["rows"][0]["capture_id"]


def test_rerank_still_respects_max_results_cap(tmp_path: Path) -> None:
    jsonl = tmp_path / "jsonl"
    for i in range(8):
        _write_doc(jsonl, i, title=f"Financial report {i}", text="financial financial")
    res = _index(tmp_path).search("financial", mode="auto", limit=100, max_results=3)
    assert len(res["rows"]) <= 3
