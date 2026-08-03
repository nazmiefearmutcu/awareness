"""The captures corpus is materialized into a real DuckDB table.

The deduped ``captures`` view reads ``captures_materialized`` (a BASE TABLE
with a unique index on ``capture_id``) instead of re-parsing JSONL chunks per
query. These tests pin the row-set semantics: after any refresh, ``captures``
must expose exactly the rows the old view-based implementation exposed
(one row per capture_id, newest fetch_ts wins, NULL capture_ids kept), across
empty corpora, chunk removal, and the M-01 corrupt-chunk fallback.
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
        text=f"body {title}",
        content_hash=f"h-{title}",
        language="en",
    )
    (day_dir / name).write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _index(tmp_path: Path) -> DuckDbIndex:
    return DuckDbIndex(
        db_path=tmp_path / "idx.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )


def _table_type(conn_owner: DuckDbIndex, name: str) -> str:
    rows = conn_owner.execute(
        "SELECT table_type FROM information_schema.tables WHERE table_schema = 'main' AND table_name = ?",
        [name],
    )
    return str(rows[0]["table_type"]) if rows else ""


def test_materialized_is_base_table_and_captures_is_view(tmp_path: Path) -> None:
    _write(tmp_path / "jsonl", "a.jsonl", "c1", "one", "2026-06-01T10:00:00+00:00")
    idx = _index(tmp_path)
    try:
        assert _table_type(idx, "captures_materialized") == "BASE TABLE"
        assert _table_type(idx, "captures") == "VIEW"
        assert _table_type(idx, "staging_captures") == "VIEW"
        # Exactly the canonical projection (no stray rn / json columns).
        cols = idx.execute(
            "SELECT COUNT(*) AS n FROM information_schema.columns WHERE table_name = 'captures_materialized'"
        )
        assert cols[0]["n"] == len(_FULL_KEYS)
    finally:
        idx.close()


def test_materialized_row_set_matches_view_dedup_semantics(tmp_path: Path) -> None:
    jsonl = tmp_path / "jsonl"
    _write(jsonl, "a.jsonl", "c1", "old", "2026-06-01T10:00:00+00:00")
    _write(jsonl, "b.jsonl", "c1", "new", "2026-06-01T12:00:00+00:00")
    _write(jsonl, "c.jsonl", "c2", "solo", "2026-06-01T11:00:00+00:00")
    idx = _index(tmp_path)
    try:
        # The view and the materialized table must agree on the deduped set.
        view_rows = idx.execute("SELECT capture_id, title FROM captures ORDER BY capture_id")
        mat_rows = idx.execute("SELECT capture_id, title FROM captures_materialized ORDER BY capture_id")
        assert len(mat_rows) == 2, "duplicate capture_id must dedup to one row"
        assert [r["capture_id"] for r in mat_rows] == [r["capture_id"] for r in view_rows]
        by_id = {r["capture_id"]: r["title"] for r in mat_rows}
        assert by_id["c1"] == "new", "newest fetch_ts must win"
        assert by_id["c2"] == "solo"
        assert [r["title"] for r in view_rows] == [r["title"] for r in mat_rows]
    finally:
        idx.close()


def test_materialized_unique_index_on_capture_id(tmp_path: Path) -> None:
    _write(tmp_path / "jsonl", "a.jsonl", "c1", "one", "2026-06-01T10:00:00+00:00")
    idx = _index(tmp_path)
    try:
        rows = idx.execute(
            "SELECT index_name, is_unique FROM duckdb_indexes() WHERE table_name = 'captures_materialized'"
        )
        assert any(r["index_name"] == "captures_materialized_capture_id" and r["is_unique"] for r in rows), (
            "captures_materialized must carry a unique index on capture_id"
        )
    finally:
        idx.close()


def test_refresh_adds_new_chunk_and_drops_removed(tmp_path: Path) -> None:
    jsonl = tmp_path / "jsonl"
    _write(jsonl, "a.jsonl", "c1", "one", "2026-06-01T10:00:00+00:00")
    idx = _index(tmp_path)
    try:
        assert idx.execute("SELECT COUNT(*) AS n FROM captures")[0]["n"] == 1
        sig_after_first = idx._views_signature

        _write(jsonl, "b.jsonl", "c2", "two", "2026-06-01T11:00:00+00:00")
        assert idx.execute("SELECT COUNT(*) AS n FROM captures")[0]["n"] == 2
        assert idx._views_signature != sig_after_first, "new chunk must bump the signature"

        (jsonl / "captures" / "2026" / "06" / "01" / "a.jsonl").unlink()
        assert idx.execute("SELECT COUNT(*) AS n FROM captures")[0]["n"] == 1
        got = idx.execute("SELECT capture_id FROM captures")
        assert got[0]["capture_id"] == "c2", "removed chunk must vanish from the materialized table"

        # health_snapshot counts must match the view.
        assert idx.health_snapshot()["captures"] == 1
    finally:
        idx.close()


def test_empty_corpus_materializes_to_zero_rows(tmp_path: Path) -> None:
    idx = _index(tmp_path)
    try:
        snap = idx.health_snapshot()
        assert snap["ready"] is True
        assert snap["captures"] == 0
        assert idx.execute("SELECT COUNT(*) AS n FROM captures_materialized")[0]["n"] == 0
        assert idx.search("bitcoin", limit=10)["total"] == 0
    finally:
        idx.close()


def test_null_capture_id_rows_survive_materialization(tmp_path: Path) -> None:
    jsonl = tmp_path / "jsonl"
    _write(jsonl, "a.jsonl", None, "no id", "2026-06-01T10:00:00+00:00")
    idx = _index(tmp_path)
    try:
        assert idx.execute("SELECT COUNT(*) AS n FROM captures")[0]["n"] == 1
        rows = idx.execute("SELECT capture_id FROM captures WHERE title = 'no id'")
        assert rows[0]["capture_id"] is None
    finally:
        idx.close()


def test_corrupt_chunk_fallback_still_materializes(tmp_path: Path) -> None:
    """M-01: one bad chunk must not brick the corpus; the materialized table
    reflects whatever the fallback staging view exposes. The per-file fallback
    operates per partition glob, so the corrupt chunk lives in its own day dir."""
    jsonl = tmp_path / "jsonl"
    _write(jsonl, "good.jsonl", "c1", "ok", "2026-06-01T10:00:00+00:00")
    # Not readable as NDJSON at all: format detection fails for the strict and
    # the ignore_errors read alike, so the per-file fallback excludes this glob
    # and the corpus comes from the good chunk only.
    bad_day = jsonl / "captures" / "2026" / "06" / "02"
    bad_day.mkdir(parents=True, exist_ok=True)
    (bad_day / "corrupt.jsonl").write_text('{"capture_id": "c1", "broken\n', encoding="utf-8")

    idx = _index(tmp_path)
    try:
        snap = idx.health_snapshot()
        assert snap["staging_view_state"] in ("fallback_ignore_errors", "fallback_per_file")
        # The materialized row set must equal whatever the fallback view exposes.
        raw_n = idx.execute("SELECT COUNT(*) AS n FROM staging_captures_raw")[0]["n"]
        assert raw_n == 1
        assert idx.execute("SELECT COUNT(*) AS n FROM captures_materialized")[0]["n"] == raw_n
        assert idx.execute("SELECT COUNT(*) AS n FROM captures")[0]["n"] == raw_n
        assert idx.execute("SELECT capture_id FROM captures")[0]["capture_id"] == "c1"
        assert idx.search("ok", limit=10)["total"] >= 1
    finally:
        idx.close()
