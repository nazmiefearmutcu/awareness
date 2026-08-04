"""W25: incremental materialize of ``captures_materialized`` (delta INSERT).

Instead of a full ``CREATE OR REPLACE TABLE`` rebuild on every write batch,
a PURE-ADDITION batch (new capture_ids only) is delta-INSERTed by scanning
just the changed chunks. Any ambiguity — chunk removal, an existing
capture_id with changed content, NULL capture_ids — must fall back to the
full rebuild so the materialized row set always equals the deduped view.

Row-set oracle: after every refresh, ``captures`` must expose exactly the
rows the full ROW_NUMBER dedup would produce (one row per capture_id,
newest fetch_ts wins, NULL ids deduped).
"""

from __future__ import annotations

import json
from pathlib import Path

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
    jsonl_dir: Path,
    name: str,
    capture_id: str | None,
    title: str,
    fetch_ts: str,
    *,
    day: str = "2026/06/01",
    content_hash: str | None = None,
    text: str | None = None,
) -> None:
    day_dir = jsonl_dir / "captures" / day
    day_dir.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{capture_id or name}",
        capture_id=capture_id,
        url=f"https://example.test/{capture_id or name}",
        canonical_url=f"https://example.test/{capture_id or name}",
        domain="example.test",
        source_type="rss",
        fetch_ts=fetch_ts,
        title=title,
        text=text if text is not None else f"body {title}",
        content_hash=content_hash if content_hash is not None else f"h-{title}",
        language="en",
    )
    (day_dir / name).write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _index(tmp_path: Path) -> DuckDbIndex:
    return DuckDbIndex(
        db_path=tmp_path / "idx.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )


def _ids(idx: DuckDbIndex) -> list[str | None]:
    return [r["capture_id"] for r in idx.execute("SELECT capture_id FROM captures ORDER BY capture_id")]


def test_pure_append_uses_delta_path(tmp_path: Path) -> None:
    jsonl = tmp_path / "jsonl"
    _write(jsonl, "a.jsonl", "c1", "one", "2026-06-01T10:00:00+00:00")
    idx = _index(tmp_path)
    try:
        assert _ids(idx) == ["c1"]
        assert idx._materialize_full_builds == 1
        assert idx._materialize_delta_applies == 0

        # Append into the SAME day dir (new chunk) — pure addition.
        _write(jsonl, "b.jsonl", "c2", "two", "2026-06-01T11:00:00+00:00")
        assert _ids(idx) == ["c1", "c2"]
        assert idx._materialize_delta_applies == 1
        assert idx._materialize_full_builds == 1, "pure append must not full-rebuild"

        # Append into a NEW day dir — still pure addition.
        _write(jsonl, "c.jsonl", "c3", "three", "2026-06-02T10:00:00+00:00", day="2026/06/02")
        assert _ids(idx) == ["c1", "c2", "c3"]
        assert idx._materialize_delta_applies == 2
        assert idx._materialize_full_builds == 1

        # Row set must match the deduped view row-for-row.
        view_rows = idx.execute("SELECT capture_id, title FROM captures ORDER BY capture_id")
        mat_rows = idx.execute(
            "SELECT capture_id, title FROM captures_materialized ORDER BY capture_id"
        )
        assert [r["capture_id"] for r in view_rows] == [r["capture_id"] for r in mat_rows]
        assert len(mat_rows) == 3
    finally:
        idx.close()


def test_delta_keeps_unique_index(tmp_path: Path) -> None:
    jsonl = tmp_path / "jsonl"
    _write(jsonl, "a.jsonl", "c1", "one", "2026-06-01T10:00:00+00:00")
    idx = _index(tmp_path)
    try:
        _write(jsonl, "b.jsonl", "c2", "two", "2026-06-01T11:00:00+00:00")
        assert _ids(idx) == ["c1", "c2"]
        rows = idx.execute(
            "SELECT index_name, is_unique FROM duckdb_indexes() WHERE table_name = 'captures_materialized'"
        )
        assert any(
            r["index_name"] == "captures_materialized_capture_id" and r["is_unique"] for r in rows
        )
    finally:
        idx.close()


def test_updated_existing_capture_forces_full_rebuild(tmp_path: Path) -> None:
    """Same capture_id re-fetched with new content must replace, not duplicate."""
    jsonl = tmp_path / "jsonl"
    _write(jsonl, "a.jsonl", "c1", "old", "2026-06-01T10:00:00+00:00")
    idx = _index(tmp_path)
    try:
        assert _ids(idx) == ["c1"], "warm-up refresh"
        assert idx._materialize_full_builds == 1
        # Newer fetch_ts, same capture_id, different content_hash — update.
        _write(
            jsonl,
            "b.jsonl",
            "c1",
            "new",
            "2026-06-01T12:00:00+00:00",
            content_hash="h-new",
        )
        _write(jsonl, "c.jsonl", "c2", "solo", "2026-06-01T11:00:00+00:00")
        assert sorted(_ids(idx), key=lambda x: x or "") == ["c1", "c2"]
        assert idx._materialize_full_builds == 2, "content change must force a full rebuild"
        assert idx._materialize_delta_applies == 0

        by_id = {r["capture_id"]: r["title"] for r in idx.execute("SELECT capture_id, title FROM captures")}
        assert by_id == {"c1": "new", "c2": "solo"}
        assert idx.execute("SELECT COUNT(*) AS n FROM captures")[0]["n"] == 2
    finally:
        idx.close()


def test_chunk_removal_forces_full_rebuild(tmp_path: Path) -> None:
    jsonl = tmp_path / "jsonl"
    _write(jsonl, "a.jsonl", "c1", "one", "2026-06-01T10:00:00+00:00")
    _write(jsonl, "b.jsonl", "c2", "two", "2026-06-01T11:00:00+00:00")
    idx = _index(tmp_path)
    try:
        assert _ids(idx) == ["c1", "c2"]
        assert idx._materialize_full_builds == 1
        (jsonl / "captures" / "2026" / "06" / "01" / "a.jsonl").unlink()
        assert _ids(idx) == ["c2"], "removed chunk must vanish"
        assert idx._materialize_full_builds == 2, "removal cannot be delta'd — full rebuild"
        assert idx._materialize_delta_applies == 0
    finally:
        idx.close()


def test_reappended_identical_rows_are_noops(tmp_path: Path) -> None:
    """Re-writing a chunk byte-identically (same ids/content) changes nothing."""
    jsonl = tmp_path / "jsonl"
    _write(jsonl, "a.jsonl", "c1", "one", "2026-06-01T10:00:00+00:00")
    idx = _index(tmp_path)
    try:
        assert _ids(idx) == ["c1"]
        # Same rows re-written to a NEW chunk path — no new ids, no conflicts.
        _write(jsonl, "b.jsonl", "c1", "one", "2026-06-01T10:00:00+00:00")
        assert _ids(idx) == ["c1"]
        assert idx.execute("SELECT COUNT(*) AS n FROM captures")[0]["n"] == 1
        assert idx._materialize_delta_applies == 1, "identical re-append stays on the delta path"
    finally:
        idx.close()


def test_null_capture_id_batch_forces_full_rebuild(tmp_path: Path) -> None:
    jsonl = tmp_path / "jsonl"
    _write(jsonl, "a.jsonl", "c1", "one", "2026-06-01T10:00:00+00:00")
    idx = _index(tmp_path)
    try:
        # NULL-id rows dedup to ONE row via ROW_NUMBER; the delta path cannot
        # express that safely, so it must full-rebuild (and still dedup).
        assert _ids(idx) == ["c1"], "warm-up refresh"
        _write(jsonl, "b.jsonl", None, "no id A", "2026-06-01T11:00:00+00:00")
        _write(jsonl, "c.jsonl", None, "no id B", "2026-06-01T12:00:00+00:00")
        null_rows = idx.execute("SELECT title FROM captures WHERE capture_id IS NULL")
        assert idx._materialize_full_builds == 2, "NULL ids must force a full rebuild"
        assert idx._materialize_delta_applies == 0
        null_rows = idx.execute("SELECT title FROM captures WHERE capture_id IS NULL")
        assert len(null_rows) == 1, "NULL capture_ids must still dedup to one row"
        assert null_rows[0]["title"] == "no id B", "newest fetch_ts wins"
    finally:
        idx.close()


def test_delta_survives_null_rows_already_in_table(tmp_path: Path) -> None:
    """A later PURE-ADDITION batch after a NULL-id row exists must not dup it."""
    jsonl = tmp_path / "jsonl"
    _write(jsonl, "a.jsonl", None, "no id", "2026-06-01T10:00:00+00:00")
    idx = _index(tmp_path)
    try:
        assert _ids(idx) == [None]
        _write(jsonl, "b.jsonl", "c2", "two", "2026-06-01T11:00:00+00:00")
        assert sorted(_ids(idx), key=lambda x: x or "") == [None, "c2"]
        assert idx.execute("SELECT COUNT(*) AS n FROM captures")[0]["n"] == 2
    finally:
        idx.close()


def test_delta_preserves_newest_fetch_ts_within_batch(tmp_path: Path) -> None:
    """Two chunks in one batch carrying the same NEW capture_id dedup to the newest."""
    jsonl = tmp_path / "jsonl"
    _write(jsonl, "a.jsonl", "c1", "one", "2026-06-01T10:00:00+00:00")
    idx = _index(tmp_path)
    try:
        assert _ids(idx) == ["c1"], "warm-up refresh"
        _write(jsonl, "b.jsonl", "c2", "old dup", "2026-06-01T11:00:00+00:00")
        _write(jsonl, "c.jsonl", "c2", "new dup", "2026-06-01T12:00:00+00:00")
        assert _ids(idx) == ["c1", "c2"]
        assert idx._materialize_delta_applies == 1
        got = idx.execute("SELECT capture_id, title FROM captures WHERE capture_id = 'c2'")
        assert got[0]["title"] == "new dup"
        assert idx.execute("SELECT COUNT(*) AS n FROM captures")[0]["n"] == 2
    finally:
        idx.close()
