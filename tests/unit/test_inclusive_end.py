"""Inclusive end-of-day for date-only / midnight UTC filter bounds."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from awareness.cli.main import _resolve_search_window
from awareness.storage.duckdb_index import DuckDbIndex
from awareness.util.timeutil import inclusive_end, to_utc

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


def test_date_only_string_expands_to_end_of_utc_day() -> None:
    dt = inclusive_end("2026-06-08")
    assert dt == datetime(2026, 6, 8, 23, 59, 59, 999999, tzinfo=UTC)


def test_naive_midnight_datetime_expands() -> None:
    # FastAPI/Pydantic path: bare date → naive midnight, then to_utc attaches UTC.
    dt = inclusive_end(datetime(2026, 6, 8, 0, 0, 0))
    assert dt == datetime(2026, 6, 8, 23, 59, 59, 999999, tzinfo=UTC)


def test_aware_midnight_expands() -> None:
    dt = inclusive_end(datetime(2026, 6, 8, 0, 0, 0, tzinfo=UTC))
    assert dt is not None
    assert (dt.hour, dt.minute, dt.second, dt.microsecond) == (23, 59, 59, 999999)


def test_non_midnight_timestamp_unchanged() -> None:
    raw = datetime(2026, 6, 8, 14, 30, 0, tzinfo=UTC)
    assert inclusive_end(raw) == raw


def test_none_passthrough() -> None:
    assert inclusive_end(None) is None


def test_resolve_search_window_date_only_end_is_inclusive() -> None:
    _, end_dt = _resolve_search_window("2026-06-08", "2026-06-08")
    assert end_dt is not None
    assert end_dt == datetime(2026, 6, 8, 23, 59, 59, 999999, tzinfo=UTC)


def test_index_date_only_end_includes_same_day_afternoon_capture(tmp_path: Path) -> None:
    """API/index path: end=midnight must still match captures later that day."""
    jsonl = tmp_path / "jsonl"
    day = jsonl / "captures" / "2026" / "06" / "08"
    day.mkdir(parents=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id="doc-1",
        capture_id="cap-afternoon",
        source_type="rss",
        domain="example.com",
        url="https://example.com/afternoon",
        fetch_ts="2026-06-08T14:00:00+00:00",
        title="Afternoon capture",
        text="body for same-day end filter",
        language="en",
        content_hash="h1",
    )
    (day / "cap-afternoon.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")

    idx = DuckDbIndex(tmp_path / "meta.duckdb", jsonl, None)
    try:
        # Simulate API: Pydantic date-only → naive midnight → to_utc → inclusive_end
        end = inclusive_end(to_utc(datetime(2026, 6, 8, 0, 0, 0)))
        start = to_utc(datetime(2026, 6, 8, 0, 0, 0))
        rows = idx.execute(
            "SELECT capture_id FROM captures WHERE fetch_ts >= $start AND fetch_ts <= $end",
            {"start": start, "end": end},
        )
        assert [r["capture_id"] for r in rows] == ["cap-afternoon"]

        # Without expansion the afternoon row would be excluded (the bug).
        midnight = to_utc(datetime(2026, 6, 8, 0, 0, 0))
        excluded = idx.execute(
            "SELECT capture_id FROM captures WHERE fetch_ts >= $start AND fetch_ts <= $end",
            {"start": start, "end": midnight},
        )
        assert excluded == []
    finally:
        idx.close()
