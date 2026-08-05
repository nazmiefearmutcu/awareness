"""W38: FTS delta-append fast path.

Pure-addition batches INSERT the delta into ``captures_idx_current`` and
rebuild ONLY that shard's inverted index — the archive index
(``captures_idx``) is left untouched, so maintenance cost tracks the batch
volume, not the corpus (~7.5s full rebuild @100k → ms for a small delta).
Edits (content_hash OR fetch_ts change) and deletions still fall back to the
rare full rebuild. Search semantics (ranking, windows, phrase) across both
shards are the pre-existing search tests' contract; these tests pin the
*path selection* and the delta's insert/idempotence/persistence behavior.
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


@pytest.fixture(autouse=True)
def _disable_fts_coalescing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin immediate-rebuild behavior (the W25 window would defer the delta
    rebuild past the assertion points)."""
    monkeypatch.setattr("awareness.storage.duckdb_index._FTS_COALESCE_WINDOW_SECONDS", 0.0)


def _write_chunk(
    jsonl_dir: Path,
    name: str,
    capture_id: str,
    text: str,
    *,
    content_hash: str | None = None,
    fetch_ts: str = "2026-06-08T00:00:00+00:00",
) -> None:
    day = jsonl_dir / "captures" / "2026" / "06" / "08"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{capture_id}",
        capture_id=capture_id,
        url=f"http://example.test/{capture_id}",
        canonical_url=f"http://example.test/{capture_id}",
        domain="example.test",
        source_type="rss",
        title=text,
        text=text,
        language="en",
        content_hash=content_hash or f"hash-{text}",
        fetch_ts=fetch_ts,
    )
    (day / name).write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _index(tmp_path: Path) -> DuckDbIndex:
    return DuckDbIndex(
        db_path=tmp_path / "idx.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )


def _shard_counts(idx: DuckDbIndex) -> tuple[int, int]:
    archive = idx.execute("SELECT COUNT(*) AS n FROM captures_idx")[0]["n"]
    current = idx.execute("SELECT COUNT(*) AS n FROM captures_idx_current")[0]["n"]
    return int(archive), int(current)


def _archive_fts_num_docs(idx: DuckDbIndex) -> int:
    """num_docs in the archive's inverted index (LIMIT 1, like the legacy
    BM25 code). Changing after an append would prove the archive index was
    rebuilt — exactly what the delta path must NOT do."""
    row = idx.execute(
        "SELECT CAST(num_docs AS INTEGER) AS n FROM fts_main_captures_idx.stats LIMIT 1"
    )
    return int(row[0]["n"]) if row else 0


# ── pure addition ⇒ delta INSERT, archive index untouched ──────────────────


def test_pure_addition_delta_insert_leaves_archive_index_intact(tmp_path: Path) -> None:
    """A new capture goes into the CURRENT shard; the archive TABLE and its
    inverted INDEX are both left untouched (num_docs identical)."""
    jsonl_dir = tmp_path / "jsonl"
    _write_chunk(jsonl_dir, "a.jsonl", "d1", "xylophone rambunctious")

    idx = _index(tmp_path)
    assert idx.search("xylophone", limit=10)["total"] >= 1
    assert idx._fts_full_rebuilds == 1
    assert idx._fts_incremental_appends == 0
    assert _shard_counts(idx) == (1, 0)
    archive_docs_before = _archive_fts_num_docs(idx)

    _write_chunk(jsonl_dir, "b.jsonl", "d2", "pluviophile zymurgy")
    assert idx.search("pluviophile", limit=10)["total"] >= 1

    assert _shard_counts(idx) == (1, 1)
    assert idx._fts_incremental_appends == 1
    assert idx._fts_full_rebuilds == 1, "append must not trigger a corpus rebuild"
    assert _archive_fts_num_docs(idx) == archive_docs_before, "archive index must not be rebuilt"
    assert idx._fts_built_for_count == 2
    assert idx.search("xylophone", limit=10)["total"] >= 1  # archive still searchable
    assert idx.search("pluviophile", mode="fts", limit=10)["total"] == 1  # delta searchable
    idx.close()


def test_delta_append_idempotent_across_batches(tmp_path: Path) -> None:
    """Each batch INSERTs only its own new rows: already-appended ids are
    skipped (NOT EXISTS guard), so the delta never holds duplicates."""
    jsonl_dir = tmp_path / "jsonl"
    _write_chunk(jsonl_dir, "a.jsonl", "d1", "xylophone rambunctious")
    idx = _index(tmp_path)
    idx.search("xylophone", limit=10)

    _write_chunk(jsonl_dir, "b.jsonl", "d2", "pluviophile zymurgy")
    idx.search("pluviophile", limit=10)
    assert idx._fts_incremental_appends == 1

    _write_chunk(jsonl_dir, "c.jsonl", "d3", "quagmire quintessential")
    idx.search("quagmire", limit=10)
    assert idx._fts_incremental_appends == 2

    archive, current = _shard_counts(idx)
    assert (archive, current) == (1, 2), "delta must hold d2+d3 exactly once each"
    distinct = idx.execute(
        "SELECT COUNT(*) AS n FROM (SELECT DISTINCT capture_id FROM captures_idx_current)"
    )[0]["n"]
    assert int(distinct) == current, "no duplicate capture_id in the delta"
    assert idx.search("pluviophile", mode="fts", limit=10)["total"] == 1
    assert idx.search("xylophone", mode="fts", limit=10)["total"] == 1
    idx.close()


def test_unchanged_corpus_never_reappends(tmp_path: Path) -> None:
    """A search with an unchanged corpus is a pure fast-path hit: no append,
    no rebuild, shard counts stable."""
    jsonl_dir = tmp_path / "jsonl"
    _write_chunk(jsonl_dir, "a.jsonl", "d1", "xylophone rambunctious")
    idx = _index(tmp_path)
    idx.search("xylophone", limit=10)

    for _ in range(3):
        idx.search("xylophone", mode="fts", limit=10)
    assert idx._fts_incremental_appends == 0
    assert idx._fts_full_rebuilds == 1
    assert _shard_counts(idx) == (1, 0)
    idx.close()


# ── edits / deletions fall back to the rare full rebuild ───────────────────


def test_content_hash_edit_falls_back_to_full_rebuild(tmp_path: Path) -> None:
    """H-10: an edited capture (same id, new content_hash) must NOT take the
    delta path — the delta INSERT cannot correct archive rows."""
    jsonl_dir = tmp_path / "jsonl"
    _write_chunk(jsonl_dir, "a.jsonl", "d1", "xylophone rambunctious")
    idx = _index(tmp_path)
    assert idx.search("xylophone", limit=10)["total"] >= 1
    assert idx._fts_full_rebuilds == 1

    (jsonl_dir / "captures" / "2026" / "06" / "08" / "a.jsonl").unlink()
    _write_chunk(jsonl_dir, "a.jsonl", "d1", "pluviophile zymurgy")
    _write_chunk(jsonl_dir, "b.jsonl", "d2", "quagmire quintessential")

    assert idx.search("pluviophile", limit=10)["total"] >= 1
    assert idx.search("xylophone", limit=10)["total"] == 0, "stale text must be gone"
    assert idx._fts_full_rebuilds == 2
    assert idx._fts_incremental_appends == 0
    assert _shard_counts(idx) == (2, 0), "full rebuild → everything back in the archive"
    idx.close()


def test_fetch_ts_change_falls_back_to_full_rebuild(tmp_path: Path) -> None:
    """W28: same content_hash but a NEW fetch_ts is still an edit — the FTS
    index must track the new timestamp or windowed ranked search serves the
    old row (silent window misses)."""
    jsonl_dir = tmp_path / "jsonl"
    _write_chunk(
        jsonl_dir, "a.jsonl", "d1", "bitcoin rally",
        fetch_ts="2026-06-01T00:00:00+00:00",
    )
    idx = _index(tmp_path)
    assert idx.search("bitcoin", limit=10)["total"] >= 1
    assert idx._fts_full_rebuilds == 1

    (jsonl_dir / "captures" / "2026" / "06" / "08" / "a.jsonl").unlink()
    _write_chunk(
        jsonl_dir, "a.jsonl", "d1", "bitcoin rally",
        fetch_ts="2026-06-05T00:00:00+00:00",
    )

    assert idx.search("bitcoin", limit=10)["total"] >= 1
    assert idx._fts_full_rebuilds == 2, "fetch_ts edit must full-rebuild"
    assert idx._fts_incremental_appends == 0
    # Windowed ranked search honors the NEW timestamp.
    new_ts = idx.search("bitcoin", mode="fts", start="2026-06-04", end="2026-06-06")
    assert [r["capture_id"] for r in new_ts["rows"]] == ["d1"]
    old_ts = idx.search("bitcoin", mode="fts", start="2026-05-31", end="2026-06-02")
    assert old_ts["rows"] == []
    idx.close()


def test_deleted_capture_falls_back_to_full_rebuild(tmp_path: Path) -> None:
    """A capture removed from the source leaves shard rows with no matching
    captures row → the missing-id gate rejects the delta path → full rebuild."""
    jsonl_dir = tmp_path / "jsonl"
    _write_chunk(jsonl_dir, "a.jsonl", "d1", "xylophone rambunctious")
    _write_chunk(jsonl_dir, "b.jsonl", "d2", "pluviophile zymurgy")
    idx = _index(tmp_path)
    assert idx.search("xylophone", limit=10)["total"] >= 1
    assert idx._fts_full_rebuilds == 1

    (jsonl_dir / "captures" / "2026" / "06" / "08" / "b.jsonl").unlink()
    assert idx.search("pluviophile", limit=10)["total"] == 0
    assert idx._fts_full_rebuilds == 2
    assert idx._fts_incremental_appends == 0
    assert _shard_counts(idx) == (1, 0)
    idx.close()


# ── search semantics hold for delta rows ───────────────────────────────────


def test_phrase_and_window_semantics_hold_for_delta_rows(tmp_path: Path) -> None:
    """Quoted-phrase substring matching and the fetch_ts window both work for
    rows that live ONLY in the delta shard."""
    jsonl_dir = tmp_path / "jsonl"
    _write_chunk(
        jsonl_dir, "a.jsonl", "d1", "market report bitcoin rally",
        fetch_ts="2026-06-01T00:00:00+00:00",
    )
    idx = _index(tmp_path)
    idx.search("bitcoin", limit=10)  # full build → archive

    _write_chunk(
        jsonl_dir, "b.jsonl", "d2", "bitcoin rally fresh heights",
        fetch_ts="2026-06-08T00:00:00+00:00",
    )
    idx.search("bitcoin", limit=10)  # delta append → current shard

    phrase = idx.search('"fresh heights"', limit=10)
    assert [r["capture_id"] for r in phrase["rows"]] == ["d2"]
    phrase_archive = idx.search('"market report"', limit=10)
    assert [r["capture_id"] for r in phrase_archive["rows"]] == ["d1"]

    delta_only = idx.search("bitcoin", mode="fts", start="2026-06-07", end="2026-06-09")
    assert [r["capture_id"] for r in delta_only["rows"]] == ["d2"]
    idx.close()


# ── the delta persists across reopen ───────────────────────────────────────


def test_reopen_restores_both_shards_after_delta_append(tmp_path: Path) -> None:
    """After a delta append the persisted fingerprint covers BOTH shards: a
    reopen with an unchanged corpus restores (no rebuild) and the delta keeps
    appending — no forced full rebuild."""
    jsonl_dir = tmp_path / "jsonl"
    db_path = tmp_path / "idx.duckdb"
    _write_chunk(jsonl_dir, "a.jsonl", "d1", "xylophone rambunctious")
    idx = DuckDbIndex(db_path=db_path, jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    idx.search("xylophone", limit=10)
    _write_chunk(jsonl_dir, "b.jsonl", "d2", "pluviophile zymurgy")
    idx.search("pluviophile", limit=10)
    assert idx._fts_incremental_appends == 1
    idx.close()

    idx2 = DuckDbIndex(db_path=db_path, jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    assert idx2.search("xylophone", limit=10)["total"] >= 1
    assert idx2.search("pluviophile", limit=10)["total"] >= 1
    assert idx2._fts_restores == 1
    assert idx2._fts_full_rebuilds == 0, "unchanged corpus must reuse both shards"
    assert _shard_counts(idx2) == (1, 1)

    _write_chunk(jsonl_dir, "c.jsonl", "d3", "quagmire quintessential")
    assert idx2.search("quagmire", limit=10)["total"] >= 1
    assert idx2._fts_incremental_appends == 1
    assert idx2._fts_full_rebuilds == 0
    assert _shard_counts(idx2) == (1, 2)
    idx2.close()
