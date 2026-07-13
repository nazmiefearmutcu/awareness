"""GET /captures unique=content|group folding (DuckDB DISTINCT ON)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from awareness.api.server import query_captures_list, unique_fold_key_sql
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
    day: str = "2026/06/01",
) -> None:
    day_dir = root / "captures" / day
    day_dir.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}",
        capture_id=capture_id,
        parent_doc_or_dup_group=parent,
        source_type="rss",
        domain="example.com",
        url=f"https://example.com/{capture_id}",
        canonical_url=f"https://example.com/{capture_id}",
        fetch_ts=fetch_ts,
        title=title,
        text=f"body for {title}",
        content_hash=content_hash,
        language="en",
    )
    (day_dir / f"{capture_id}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _index(tmp_path: Path) -> DuckDbIndex:
    jsonl = tmp_path / "jsonl"
    # Two exact content hashes (h-a) at different times; near-dups share group g1
    # with different hashes; one singleton.
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
    )
    _write(
        jsonl,
        idx=5,
        capture_id="c-null-hash",
        content_hash=None,
        parent=None,
        fetch_ts="2026-06-01T14:00:00+00:00",
        title="Null hash",
    )
    return DuckDbIndex(
        db_path=tmp_path / "i.duckdb",
        jsonl_dir=jsonl,
        iceberg_warehouse=None,
    )


def test_unique_fold_key_sql_modes() -> None:
    assert unique_fold_key_sql("none") is None
    assert "content_hash" in (unique_fold_key_sql("content") or "")
    assert "parent_doc_or_dup_group" in (unique_fold_key_sql("group") or "")
    with pytest.raises(ValueError):
        unique_fold_key_sql("bogus")


def test_unique_none_returns_all(tmp_path: Path) -> None:
    idx = _index(tmp_path)
    try:
        out = query_captures_list(idx, limit=50, offset=0, unique="none")
        assert out["total"] == 5
        assert out["unique"] == "none"
        assert len(out["rows"]) == 5
        ids = {r["capture_id"] for r in out["rows"]}
        assert ids == {"c-old-a", "c-new-a", "c-near", "c-other", "c-null-hash"}
    finally:
        idx.close()


def test_unique_content_collapses_hash_keeps_newest(tmp_path: Path) -> None:
    idx = _index(tmp_path)
    try:
        out = query_captures_list(idx, limit=50, offset=0, unique="content")
        # h-a x2 → 1, h-b, h-c, null → 4 unique
        assert out["total"] == 4
        assert out["unique"] == "content"
        by_hash = {r["content_hash"]: r for r in out["rows"]}
        assert by_hash["h-a"]["capture_id"] == "c-new-a"  # newer of the two
        assert by_hash["h-b"]["capture_id"] == "c-near"
        assert by_hash["h-c"]["capture_id"] == "c-other"
        assert by_hash[None]["capture_id"] == "c-null-hash"
        # Ordered by fetch_ts DESC
        assert [r["capture_id"] for r in out["rows"]] == [
            "c-null-hash",
            "c-near",
            "c-new-a",
            "c-other",
        ]
    finally:
        idx.close()


def test_unique_group_collapses_parent_then_hash(tmp_path: Path) -> None:
    idx = _index(tmp_path)
    try:
        out = query_captures_list(idx, limit=50, offset=0, unique="group")
        # g1 covers c-old-a, c-new-a, c-near → keep newest (c-near @13:00)
        # h-c alone, null-hash alone → 3 groups
        assert out["total"] == 3
        assert out["unique"] == "group"
        ids = [r["capture_id"] for r in out["rows"]]
        assert ids == ["c-null-hash", "c-near", "c-other"]
        assert out["rows"][1]["parent_doc_or_dup_group"] == "g1"
    finally:
        idx.close()


def test_unique_content_pagination_and_total(tmp_path: Path) -> None:
    idx = _index(tmp_path)
    try:
        page1 = query_captures_list(idx, limit=2, offset=0, unique="content")
        page2 = query_captures_list(idx, limit=2, offset=2, unique="content")
        assert page1["total"] == page2["total"] == 4
        assert len(page1["rows"]) == 2
        assert len(page2["rows"]) == 2
        all_ids = [r["capture_id"] for r in page1["rows"] + page2["rows"]]
        assert all_ids == ["c-null-hash", "c-near", "c-new-a", "c-other"]
        assert len(set(all_ids)) == 4
    finally:
        idx.close()


def test_unique_respects_domain_filter(tmp_path: Path) -> None:
    idx = _index(tmp_path)
    try:
        # All rows are example.com; filter to empty domain yields zero
        out = query_captures_list(
            idx,
            limit=50,
            offset=0,
            where=["domain = $dom"],
            params={"dom": "no.such"},
            unique="group",
        )
        assert out["total"] == 0
        assert out["rows"] == []

        out2 = query_captures_list(
            idx,
            limit=50,
            offset=0,
            where=["domain = $dom"],
            params={"dom": "example.com"},
            unique="content",
        )
        assert out2["total"] == 4
    finally:
        idx.close()


def test_browse_language_filter_case_insensitive(tmp_path: Path) -> None:
    """GET /captures language clause (lower) keeps only matching BCP-47 tags."""
    idx = _index(tmp_path)
    try:
        en = query_captures_list(
            idx,
            limit=50,
            offset=0,
            where=["lower(language) = $lang"],
            params={"lang": "EN".strip().lower()},
            unique="none",
        )
        assert en["total"] == 5
        for row in en["rows"]:
            assert str(row.get("language") or "").lower() == "en"

        none = query_captures_list(
            idx,
            limit=50,
            offset=0,
            where=["lower(language) = $lang"],
            params={"lang": "tr"},
            unique="none",
        )
        assert none["total"] == 0
    finally:
        idx.close()


def test_browse_domain_filter_case_insensitive(tmp_path: Path) -> None:
    """GET /captures domain clause uses lower() so SPA casing still matches."""
    idx = _index(tmp_path)
    try:
        out = query_captures_list(
            idx,
            limit=50,
            offset=0,
            where=["lower(domain) = $dom"],
            params={"dom": "Example.COM".strip().lower()},
            unique="none",
        )
        assert out["total"] == 5
        miss = query_captures_list(
            idx,
            limit=50,
            offset=0,
            where=["lower(domain) = $dom"],
            params={"dom": "other.example"},
            unique="none",
        )
        assert miss["total"] == 0
    finally:
        idx.close()


def test_list_captures_endpoint_uses_case_insensitive_domain() -> None:
    """Server list_captures SQL uses lower(domain) + shared language filter helper."""
    import inspect

    import awareness.api.server as server

    src = inspect.getsource(server.create_app)
    assert "lower(domain) = $dom" in src
    # Language filter via shared helper (primary subtag match + underscore normalize).
    assert "append_language_filter" in src
