"""W28: time-sharded FTS — pure-addition batches rebuild ONLY the current
shard; the archive shard is rebuilt only at promotion (cap) or on content
edits. Search merges both shards with corpus-global BM25 statistics, so
ranking is identical to the old single-index behavior (the pre-existing
search tests are the oracle for that).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import awareness.storage.duckdb_index as duckdb_index
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
    """Pin immediate-rebuild behavior for the shard-path assertions (the W25
    window would defer the rebuild past the assertion points)."""
    monkeypatch.setattr("awareness.storage.duckdb_index._FTS_COALESCE_WINDOW_SECONDS", 0.0)


@pytest.fixture(autouse=True)
def _default_shard_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests poke at the caps; ensure a known baseline for every test."""
    monkeypatch.setattr("awareness.storage.duckdb_index._FTS_SHARD_MAX_ROWS", 50_000)
    monkeypatch.setattr("awareness.storage.duckdb_index._FTS_SHARD_MAX_DAYS", 7.0)


def _write_chunk(
    jsonl_dir: Path,
    name: str,
    capture_id: str,
    text: str,
    *,
    day: str = "2026/06/08",
    fetch_ts: str = "2026-06-08T00:00:00+00:00",
) -> None:
    day_dir = jsonl_dir / "captures" / day
    day_dir.mkdir(parents=True, exist_ok=True)
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
        content_hash=f"hash-{text}",
        fetch_ts=fetch_ts,
    )
    (day_dir / name).write_text(json.dumps(rec) + "\n", encoding="utf-8")


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


# ── delta append rebuilds only the current shard ───────────────────────────


def test_pure_append_rebuilds_only_current_shard(tmp_path: Path) -> None:
    jsonl_dir = tmp_path / "jsonl"
    _write_chunk(jsonl_dir, "a.jsonl", "d1", "xylophone rambunctious")

    idx = _index(tmp_path)
    assert idx.search("xylophone", limit=10)["total"] >= 1
    assert idx._fts_full_rebuilds == 1
    assert _shard_counts(idx) == (1, 0)

    _write_chunk(jsonl_dir, "b.jsonl", "d2", "pluviophile zymurgy")
    assert idx.search("pluviophile", limit=10)["total"] >= 1

    # The delta went to the CURRENT shard; the archive was NOT rebuilt.
    assert _shard_counts(idx) == (1, 1)
    assert idx._fts_incremental_appends == 1
    assert idx._fts_full_rebuilds == 1, "append must not trigger a corpus rebuild"
    assert idx._fts_shard_promotions == 0
    assert idx.search("xylophone", limit=10)["total"] >= 1  # archive still searchable
    idx.close()


def test_shard_append_keeps_health_count_total(tmp_path: Path) -> None:
    jsonl_dir = tmp_path / "jsonl"
    _write_chunk(jsonl_dir, "a.jsonl", "d1", "xylophone rambunctious")
    idx = _index(tmp_path)
    idx.search("xylophone", limit=10)
    _write_chunk(jsonl_dir, "b.jsonl", "d2", "pluviophile zymurgy")
    idx.search("pluviophile", limit=10)
    assert idx.health_snapshot()["fts_built_for_count"] == 2  # archive + current
    idx.close()


# ── promotion at the caps ──────────────────────────────────────────────────


def test_archive_promotion_at_row_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("awareness.storage.duckdb_index._FTS_SHARD_MAX_ROWS", 2)
    jsonl_dir = tmp_path / "jsonl"
    _write_chunk(jsonl_dir, "a.jsonl", "d1", "xylophone rambunctious")
    idx = _index(tmp_path)
    idx.search("xylophone", limit=10)

    _write_chunk(jsonl_dir, "b.jsonl", "d2", "pluviophile zymurgy")
    idx.search("pluviophile", limit=10)
    assert _shard_counts(idx) == (1, 1)  # current=1: under the cap

    _write_chunk(jsonl_dir, "c.jsonl", "d3", "quagmire quintessential")
    idx.search("quagmire", limit=10)
    assert _shard_counts(idx) == (1, 2)  # current=2: equals, not exceeds, the cap

    _write_chunk(jsonl_dir, "d.jsonl", "d4", "zephyr zymurgy")
    idx.search("zephyr", limit=10)
    # d4 pushed the current shard OVER the cap → promoted into the archive.
    assert _shard_counts(idx) == (4, 0)
    assert idx._fts_shard_promotions == 1
    assert idx._fts_full_rebuilds == 1, "promotion must not count as a full rebuild"

    # All docs still searchable after promotion.
    assert idx.search("xylophone", limit=10)["total"] >= 1
    assert idx.search("pluviophile", limit=10)["total"] >= 1
    assert idx.search("quagmire", limit=10)["total"] >= 1
    assert idx.search("zephyr", limit=10)["total"] >= 1
    idx.close()


def test_archive_promotion_at_time_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("awareness.storage.duckdb_index._FTS_SHARD_MAX_DAYS", 2.0)
    jsonl_dir = tmp_path / "jsonl"
    _write_chunk(jsonl_dir, "a.jsonl", "d1", "xylophone rambunctious", day="2026/06/01")
    idx = _index(tmp_path)
    idx.search("xylophone", limit=10)

    # New row on the same day as the archive content: no span in the shard.
    _write_chunk(
        jsonl_dir, "b.jsonl", "d2", "pluviophile zymurgy",
        day="2026/06/01", fetch_ts="2026-06-01T00:00:00+00:00",
    )
    idx.search("pluviophile", limit=10)
    assert _shard_counts(idx) == (1, 1)
    assert idx._fts_shard_promotions == 0

    # Delta now spans 06-01 → 06-04 = 3 days > 2-day cap → promotion.
    _write_chunk(
        jsonl_dir, "c.jsonl", "d3", "quagmire quintessential",
        day="2026/06/04", fetch_ts="2026-06-04T00:00:00+00:00",
    )
    idx.search("quagmire", limit=10)
    assert _shard_counts(idx) == (3, 0)
    assert idx._fts_shard_promotions == 1
    assert idx.search("pluviophile", limit=10)["total"] >= 1
    idx.close()


# ── merged search semantics ────────────────────────────────────────────────


def test_search_merges_both_shards_ranked(tmp_path: Path) -> None:
    jsonl_dir = tmp_path / "jsonl"
    # Archive doc: term only in the body.
    _write_chunk(
        jsonl_dir,
        "a.jsonl",
        "d1",
        "Market report: bitcoin moved sideways all week.",
    )
    idx = _index(tmp_path)
    idx.search("bitcoin", limit=10)  # full build → archive

    # Current-shard doc: term in the TITLE → should rank FIRST overall.
    _write_chunk(
        jsonl_dir,
        "b.jsonl",
        "d2",
        "Bitcoin price surges past local resistance.",
    )
    res = idx.search("bitcoin", mode="fts", limit=10)
    assert res["total"] == 2
    ids = [r["capture_id"] for r in res["rows"]]
    assert set(ids) == {"d1", "d2"}
    assert ids[0] == "d2", "title match from the current shard ranks first"
    assert res["rows"][0]["score"] > res["rows"][1]["score"]
    assert res["ranked"] is True
    idx.close()


def test_windowed_search_across_shards(tmp_path: Path) -> None:
    """fetch_ts filters apply per shard — both shards respect the window."""
    jsonl_dir = tmp_path / "jsonl"
    _write_chunk(
        jsonl_dir, "a.jsonl", "d1", "bitcoin rally",
        fetch_ts="2026-06-01T00:00:00+00:00",
    )
    idx = _index(tmp_path)
    idx.search("bitcoin", limit=10)  # archive: d1 @ 06-01

    _write_chunk(
        jsonl_dir, "b.jsonl", "d2", "bitcoin rally again",
        fetch_ts="2026-06-08T00:00:00+00:00",
    )
    idx.search("bitcoin", limit=10)  # current: d2 @ 06-08

    narrow = idx.search("bitcoin", mode="fts", start="2026-06-07", end="2026-06-09")
    assert [r["capture_id"] for r in narrow["rows"]] == ["d2"]

    wide = idx.search("bitcoin", mode="fts", start="2026-06-01", end="2026-06-09")
    assert {r["capture_id"] for r in wide["rows"]} == {"d1", "d2"}
    idx.close()


# ── coalescing still works with shards ─────────────────────────────────────


def test_coalescing_window_defers_only_current_shard_rebuild(tmp_path: Path) -> None:
    jsonl_dir = tmp_path / "jsonl"
    _write_chunk(jsonl_dir, "a.jsonl", "d1", "xylophone rambunctious")
    idx = _index(tmp_path)
    idx.search("xylophone", limit=10)  # full build, no coalescing active

    _write_chunk(jsonl_dir, "b.jsonl", "d2", "pluviophile zymurgy")
    idx._fts_coalesce_window = 3600.0
    res = idx.search("pluviophile", limit=10)
    assert res["total"] >= 1  # served via the prefix fallback
    assert idx._fts_coalesced_skips == 1
    assert idx._fts_incremental_appends == 0

    idx._fts_coalesce_window = 0.0
    res2 = idx.search("pluviophile", mode="fts", limit=10)
    assert res2["total"] >= 1
    assert idx._fts_incremental_appends == 1
    assert _shard_counts(idx) == (1, 1)
    idx.close()


# ── correctness gates: edits still full-rebuild ────────────────────────────


def test_content_edit_forces_full_rebuild_across_shards(tmp_path: Path) -> None:
    jsonl_dir = tmp_path / "jsonl"
    _write_chunk(jsonl_dir, "a.jsonl", "c1", "xylophone rambunctious")
    idx = _index(tmp_path)
    assert idx.search("xylophone", limit=10)["total"] >= 1
    assert idx._fts_full_rebuilds == 1

    # Same capture_id, NEW content + one genuinely new capture: the delta
    # path must reject the edit (H-10) and rebuild BOTH shards.
    (jsonl_dir / "captures" / "2026" / "06" / "08" / "a.jsonl").unlink()
    _write_chunk(jsonl_dir, "a.jsonl", "c1", "pluviophile zymurgy")
    _write_chunk(jsonl_dir, "b.jsonl", "c2", "quintessential quagmire")

    assert idx.search("pluviophile", limit=10)["total"] >= 1
    assert idx.search("xylophone", limit=10)["total"] == 0, "stale text must be gone"
    assert idx._fts_full_rebuilds == 2
    assert idx._fts_incremental_appends == 0
    assert _shard_counts(idx) == (2, 0)  # full rebuild → everything in archive
    idx.close()


# ── legacy persisted index becomes the archive ─────────────────────────────


def test_legacy_persisted_index_becomes_archive(tmp_path: Path) -> None:
    """A pre-W28 DB has only captures_idx + its FTS tables. On reopen the
    restore path reuses it AS the archive (no rebuild) and seeds an empty
    current shard."""
    jsonl_dir = tmp_path / "jsonl"
    db_path = tmp_path / "idx.duckdb"
    _write_chunk(jsonl_dir, "a.jsonl", "d1", "xylophone rambunctious")

    idx = DuckDbIndex(db_path=db_path, jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    idx.search("xylophone", limit=10)
    # Simulate a pre-W28 database: only the legacy table survives.
    idx.execute("DROP TABLE captures_idx_current")
    idx.close()

    idx2 = DuckDbIndex(db_path=db_path, jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    assert idx2.search("xylophone", limit=10)["total"] >= 1
    assert idx2._fts_restores == 1
    assert idx2._fts_full_rebuilds == 0, "legacy index reused, not rebuilt"
    assert _shard_counts(idx2) == (1, 0)
    idx2.close()
