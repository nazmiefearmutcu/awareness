"""W25: the source-signature walk is guarded by a cheap dir-mtime summary.

``_source_signature`` returns ``(signature, guard)`` where *guard* is a
(root, year, month, day) dir-mtime fingerprint. When the guard is unchanged
since the last committed signature, the per-file walk (~92ms @100k) is
skipped and the cached signature is reused. The guard detects chunk
adds/removes (atomic renames bump the day-dir mtime) without stat-ing the
JSONL files themselves, and is committed together with the signature so a
failed refresh never short-circuits the retry.
"""

from __future__ import annotations

import json
from pathlib import Path

import awareness.storage.duckdb_index as mod
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


def _write_chunk(
    jsonl_dir: Path,
    name: str,
    capture_id: str,
    text: str,
    *,
    day: str = "2026/06/08",
) -> None:
    day_dir = jsonl_dir / "captures" / day
    day_dir.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=capture_id,
        capture_id=capture_id,
        url=f"http://example.test/{capture_id}",
        canonical_url=f"http://example.test/{capture_id}",
        domain="example.test",
        source_type="rss",
        title=text,
        text=text,
        language="en",
        fetch_ts="2026-06-08T00:00:00+00:00",
    )
    (day_dir / name).write_text(json.dumps(rec) + "\n", encoding="utf-8")


def test_signature_skips_walk_when_unchanged(tmp_path: Path, monkeypatch) -> None:
    jsonl_dir = tmp_path / "jsonl"
    _write_chunk(jsonl_dir, "a.jsonl", "c1", "hello world")
    idx = DuckDbIndex(db_path=tmp_path / "i.duckdb", jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    try:
        idx.execute("SELECT COUNT(*) FROM captures")
        assert idx._signature_guard is not None
        assert idx._views_signature is not None

        walks = {"n": 0}
        orig = mod.DuckDbIndex._walk_source_signature

        def spy(self):
            walks["n"] += 1
            return orig(self)

        monkeypatch.setattr(mod.DuckDbIndex, "_walk_source_signature", spy)

        # Steady-state signature checks must not walk files.
        sig1, guard1 = idx._source_signature()
        sig2, guard2 = idx._source_signature()
        assert walks["n"] == 0, "unchanged corpus must reuse the cached signature"
        assert sig1 == sig2 == idx._views_signature
        assert guard1 == guard2 == idx._signature_guard

        # And no view refresh either.
        idx.execute("SELECT COUNT(*) FROM captures")
        assert walks["n"] == 0
    finally:
        idx.close()


def test_new_chunk_in_existing_day_dir_invalidates_guard(tmp_path: Path) -> None:
    jsonl_dir = tmp_path / "jsonl"
    _write_chunk(jsonl_dir, "a.jsonl", "c1", "one")
    idx = DuckDbIndex(db_path=tmp_path / "i.duckdb", jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    try:
        assert idx.execute("SELECT COUNT(*) AS n FROM captures")[0]["n"] == 1
        sig_before = idx._views_signature
        # New chunk in the SAME day dir: only the day-dir mtime changes.
        _write_chunk(jsonl_dir, "b.jsonl", "c2", "two")
        sig, guard = idx._source_signature()
        assert guard != idx._signature_guard, "day-dir mtime change must bump the guard"
        assert sig != sig_before
        assert idx.execute("SELECT COUNT(*) AS n FROM captures")[0]["n"] == 2
    finally:
        idx.close()


def test_new_month_and_year_dirs_invalidate_guard(tmp_path: Path) -> None:
    jsonl_dir = tmp_path / "jsonl"
    _write_chunk(jsonl_dir, "a.jsonl", "c1", "one", day="2026/06/08")
    idx = DuckDbIndex(db_path=tmp_path / "i.duckdb", jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    try:
        assert idx.execute("SELECT COUNT(*) AS n FROM captures")[0]["n"] == 1
        _write_chunk(jsonl_dir, "b.jsonl", "c2", "two", day="2026/07/01")
        _write_chunk(jsonl_dir, "c.jsonl", "c3", "three", day="2027/01/15")
        _, guard = idx._source_signature()
        assert guard != idx._signature_guard
        assert idx.execute("SELECT COUNT(*) AS n FROM captures")[0]["n"] == 3
    finally:
        idx.close()


def test_chunk_removal_invalidates_guard(tmp_path: Path) -> None:
    jsonl_dir = tmp_path / "jsonl"
    _write_chunk(jsonl_dir, "a.jsonl", "c1", "one")
    _write_chunk(jsonl_dir, "b.jsonl", "c2", "two")
    idx = DuckDbIndex(db_path=tmp_path / "i.duckdb", jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    try:
        assert idx.execute("SELECT COUNT(*) AS n FROM captures")[0]["n"] == 2
        (jsonl_dir / "captures" / "2026" / "06" / "08" / "a.jsonl").unlink()
        _, guard = idx._source_signature()
        assert guard != idx._signature_guard
        assert idx.execute("SELECT COUNT(*) AS n FROM captures")[0]["n"] == 1
    finally:
        idx.close()


def test_guard_does_not_commit_on_failed_refresh(tmp_path: Path, monkeypatch) -> None:
    """M-02: a failed refresh keeps the old signature AND its guard, so the
    next call retries instead of short-circuiting on the guard cache."""
    jsonl_dir = tmp_path / "jsonl"
    _write_chunk(jsonl_dir, "a.jsonl", "c1", "one")
    idx = DuckDbIndex(db_path=tmp_path / "i.duckdb", jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    try:
        idx.execute("SELECT COUNT(*) FROM captures")
        guard_committed = idx._signature_guard
        sig_committed = idx._views_signature

        _write_chunk(jsonl_dir, "b.jsonl", "c2", "two")
        orig_refresh = mod.DuckDbIndex._refresh_views

        def boom(self, conn, *, new_sig=None):
            return False

        monkeypatch.setattr(mod.DuckDbIndex, "_refresh_views", boom)
        # The refresh fails → signature and guard must NOT be committed.
        idx._refresh_views_if_stale(idx._conn)
        assert idx._views_signature == sig_committed
        assert idx._signature_guard == guard_committed

        # Once the refresh works again, the next call retries and commits.
        monkeypatch.setattr(mod.DuckDbIndex, "_refresh_views", orig_refresh)
        assert idx.execute("SELECT COUNT(*) AS n FROM captures")[0]["n"] == 2
        assert idx._views_signature != sig_committed
    finally:
        idx.close()


def test_fresh_instance_walks_once(tmp_path: Path) -> None:
    jsonl_dir = tmp_path / "jsonl"
    _write_chunk(jsonl_dir, "a.jsonl", "c1", "hello world")
    idx = DuckDbIndex(db_path=tmp_path / "i.duckdb", jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    try:
        # No cached signature on a fresh instance → full walk, then guarded.
        sig, guard = idx._source_signature()
        assert idx._views_signature is None
        assert sig != ()
        idx.execute("SELECT COUNT(*) FROM captures")
        assert idx._views_signature is not None
        assert idx._signature_guard is not None
        sig2, guard2 = idx._source_signature()
        assert sig2 == idx._views_signature
        assert guard2 == guard
    finally:
        idx.close()
