"""Search pagination after collapse + re-rank.

Guarantees:
  * ``total`` is the unique collapsed count (not raw capture_id hits)
  * offset/limit are applied **after** collapse (and re-rank on FTS)
  * page 2 is stable: disjoint from page 1, order matches the full list,
    and repeated calls return the same rows
  * offset past ``max_results`` returns empty (no silent rewrite)
"""

from __future__ import annotations

import json
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


def _write_doc(
    root: Path,
    idx: int,
    *,
    title: str,
    text: str,
    domain: str = "example.com",
    content_hash_val: str | None = None,
    parent_doc_or_dup_group: str | None = None,
    fetch_ts: str | None = None,
) -> None:
    day = root / "captures" / "2026" / "06" / "01"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    minute = idx % 60
    hour = 12 + (idx // 60)
    rec.update(
        doc_id=f"doc-{idx}",
        capture_id=f"cap-{idx}",
        parent_doc_or_dup_group=parent_doc_or_dup_group,
        source_type="rss",
        domain=domain,
        url=f"https://{domain}/{idx}",
        fetch_ts=fetch_ts or f"2026-06-01T{hour:02d}:{minute:02d}:00+00:00",
        title=title,
        text=text,
        content_hash=content_hash_val,
    )
    (day / f"chunk-{idx}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _index(tmp_path: Path) -> DuckDbIndex:
    return DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )


def _corpus_with_dups(jsonl: Path, n_unique: int = 6) -> list[str]:
    """n_unique articles x 2 syndication copies (shared content_hash).

    Returns the ordered list of unique content_hash values (hash0000...).
    """
    hashes: list[str] = []
    for i in range(n_unique):
        body = (
            f"unique article number {i} about quantum computing breakthroughs "
            f"and research findings with enough tokens for fts {i}"
        )
        h = f"hash{i:04d}aaaaaaaa"
        hashes.append(h)
        _write_doc(
            jsonl, i * 2,
            title=f"Quantum article {i}",
            text=body,
            domain="news.example",
            content_hash_val=h,
        )
        _write_doc(
            jsonl, i * 2 + 1,
            title=f"Quantum article {i}",
            text=body,
            domain="wire.example",
            content_hash_val=h,
        )
    return hashes


@pytest.mark.parametrize("mode", ("prefix", "substring", "auto", "fts"))
def test_total_is_unique_collapsed_count(tmp_path: Path, mode: str) -> None:
    jsonl = tmp_path / "jsonl"
    n_unique = 5
    _corpus_with_dups(jsonl, n_unique=n_unique)
    idx = _index(tmp_path)
    try:
        res = idx.search("quantum", mode=mode, limit=50, offset=0)
        assert res["total"] == n_unique, (
            f"mode={mode}: total must count collapsed uniques, not raw rows "
            f"(got total={res['total']}, rows={len(res['rows'])})"
        )
        assert len(res["rows"]) == n_unique
        hashes = [r.get("content_hash") for r in res["rows"]]
        assert len(hashes) == len(set(hashes)), f"mode={mode}: dups leaked into rows"
    finally:
        idx.close()


@pytest.mark.parametrize("mode", ("prefix", "substring", "auto", "fts"))
def test_page2_stable_disjoint_and_matches_full_list(tmp_path: Path, mode: str) -> None:
    """Page 2 must be stable, disjoint from page 1, and concat == full ranking."""
    jsonl = tmp_path / "jsonl"
    n_unique = 6
    _corpus_with_dups(jsonl, n_unique=n_unique)
    idx = _index(tmp_path)
    try:
        full = idx.search("quantum", mode=mode, limit=50, offset=0)
        assert full["total"] == n_unique
        full_ids = [r["capture_id"] for r in full["rows"]]

        page_size = 2
        p0 = idx.search("quantum", mode=mode, limit=page_size, offset=0)
        p1 = idx.search("quantum", mode=mode, limit=page_size, offset=page_size)
        p2 = idx.search("quantum", mode=mode, limit=page_size, offset=page_size * 2)
        # Stability: second fetch of page 1 equals the first.
        p1_again = idx.search("quantum", mode=mode, limit=page_size, offset=page_size)

        assert p0["total"] == p1["total"] == p2["total"] == n_unique
        assert len(p0["rows"]) == page_size
        assert len(p1["rows"]) == page_size
        assert len(p2["rows"]) == page_size

        p0_ids = [r["capture_id"] for r in p0["rows"]]
        p1_ids = [r["capture_id"] for r in p1["rows"]]
        p2_ids = [r["capture_id"] for r in p2["rows"]]
        p1_again_ids = [r["capture_id"] for r in p1_again["rows"]]

        assert p1_ids == p1_again_ids, f"mode={mode}: page 2 not stable across calls"
        assert set(p0_ids).isdisjoint(p1_ids), f"mode={mode}: page1/page2 overlap"
        assert set(p1_ids).isdisjoint(p2_ids), f"mode={mode}: page2/page3 overlap"
        assert p0_ids + p1_ids + p2_ids == full_ids, (
            f"mode={mode}: paged concat must equal full post-collapse ranking\n"
            f"  full={full_ids}\n  paged={p0_ids + p1_ids + p2_ids}"
        )
        # Echoed offset/limit match the request (no silent rewrite on in-range pages).
        assert p1["offset"] == page_size and p1["limit"] == page_size
    finally:
        idx.close()


@pytest.mark.parametrize("mode", ("prefix", "auto"))
def test_offset_past_max_results_returns_empty_not_rewritten(
    tmp_path: Path, mode: str
) -> None:
    """Past the overload ceiling: empty page, original offset, total ≤ max_results."""
    jsonl = tmp_path / "jsonl"
    _corpus_with_dups(jsonl, n_unique=6)
    idx = _index(tmp_path)
    try:
        max_results = 4
        res = idx.search(
            "quantum", mode=mode, limit=2, offset=10, max_results=max_results,
        )
        assert res["offset"] == 10, "must not rewrite offset past max_results"
        assert res["rows"] == []
        assert res["total"] <= max_results
        # In-range last page still works and reports the same capped total.
        last = idx.search(
            "quantum", mode=mode, limit=2, offset=2, max_results=max_results,
        )
        assert last["offset"] == 2
        assert last["total"] == res["total"]
        assert len(last["rows"]) <= 2
        # total is unique collapsed count capped by max_results (6 unique → 4).
        assert last["total"] == max_results
    finally:
        idx.close()


def test_collapse_then_page_does_not_underfill_with_dups(tmp_path: Path) -> None:
    """limit=3 with 3 unique x 2 dups must still fill the page (collapse first)."""
    jsonl = tmp_path / "jsonl"
    _corpus_with_dups(jsonl, n_unique=3)
    idx = _index(tmp_path)
    try:
        res = idx.search("quantum", mode="prefix", limit=3, offset=0)
        assert res["total"] == 3
        assert len(res["rows"]) == 3
        page2 = idx.search("quantum", mode="prefix", limit=3, offset=3)
        assert page2["total"] == 3
        assert page2["rows"] == []
    finally:
        idx.close()


def test_fts_page_after_rerank_stable(tmp_path: Path) -> None:
    """FTS: re-rank then page; page 2 stays disjoint and total is unique."""
    jsonl = tmp_path / "jsonl"
    # Mix title hits so re-rank reorders relative to raw body TF.
    for i in range(4):
        body = f"bitcoin bitcoin bitcoin market note {i} with filler text for length"
        h = f"btc{i:04d}hashhhhh"
        title = "Bitcoin outlook" if i % 2 == 0 else f"Market note {i}"
        _write_doc(
            jsonl, i * 2,
            title=title, text=body, domain="a.example", content_hash_val=h,
        )
        _write_doc(
            jsonl, i * 2 + 1,
            title=title, text=body, domain="b.example", content_hash_val=h,
        )
    idx = _index(tmp_path)
    try:
        full = idx.search("bitcoin", mode="auto", limit=10, offset=0)
        assert full["mode"] == "fts" and full["ranked"] is True
        assert full["total"] == 4
        p0 = idx.search("bitcoin", mode="auto", limit=2, offset=0)
        p1 = idx.search("bitcoin", mode="auto", limit=2, offset=2)
        p1b = idx.search("bitcoin", mode="auto", limit=2, offset=2)
        assert p0["total"] == p1["total"] == 4
        ids0 = [r["capture_id"] for r in p0["rows"]]
        ids1 = [r["capture_id"] for r in p1["rows"]]
        assert ids1 == [r["capture_id"] for r in p1b["rows"]]
        assert set(ids0).isdisjoint(ids1)
        assert ids0 + ids1 == [r["capture_id"] for r in full["rows"]]
    finally:
        idx.close()
