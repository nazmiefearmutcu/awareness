"""GDELT truncation must never be dropped silently (gdeltx/{engine,models}.py).

1. ``_aggregate`` ORs member ``truncated`` flags into each aggregated bucket.
2. ``coverage_gap`` surfaces truncation on ``GapReport`` (``truncated`` flag
   + ``note`` with the cap message).
3. The disk cache round-trips the flag.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from awareness.gdeltx.engine import GdeltBridge
from awareness.gdeltx.models import GapReport, GdeltWindow
from awareness.storage.duckdb_index import DuckDbIndex
from awareness.util.timeutil import floor_to_day

_FIXED_NOW = datetime(2026, 6, 14, 10, 0, 0, tzinfo=UTC)

_FULL_KEYS = (
    "doc_id", "capture_id", "parent_doc_or_dup_group", "source_type",
    "source_name", "source_locator", "source_shard",
    "source_offset_or_record_id", "discovery_channel", "job_id", "batch_id",
    "ingest_version", "url", "canonical_url", "domain", "fetch_ts",
    "observed_ts", "published_ts", "last_modified", "content_type",
    "http_status", "etag", "title", "text", "language", "content_hash",
    "near_dup_hash", "robots_decision", "terms_note_if_relevant",
)


def _window(term: str, day: int, count: int, truncated: bool = False) -> GdeltWindow:
    return GdeltWindow(
        term=term,
        ts=datetime(2026, 6, day, tzinfo=UTC),
        count=count,
        truncated=truncated,
    )


def _index(tmp_path: Path) -> DuckDbIndex:
    return DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )


def _write_doc(root: Path, idx: int, *, title: str, text: str) -> None:
    day = root / "captures" / "2026" / "06" / "08"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}",
        capture_id=f"cap-{idx:04d}",
        source_type="rss",
        domain="example.com",
        url=f"https://example.com/{idx}",
        fetch_ts="2026-06-08T12:00:00+00:00",
        title=title,
        text=text,
    )
    (day / f"chunk-{idx:04d}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


# ── _aggregate OR-ing ────────────────────────────────────────────────────────


def test_aggregate_week_ors_truncated_flags() -> None:
    """A bucket is truncated when ANY member day was truncated."""
    windows = [
        _window("bitcoin", 1, 100, truncated=False),
        _window("bitcoin", 2, 250, truncated=True),  # cap hit
        _window("bitcoin", 3, 150, truncated=True),
        _window("bitcoin", 8, 50, truncated=False),  # next Monday → week 2
    ]
    agg = GdeltBridge._aggregate(windows, "week")
    assert len(agg) == 2
    assert agg[0].ts == datetime(2026, 6, 1, tzinfo=UTC)
    assert agg[0].count == 500
    assert agg[0].truncated is True
    assert agg[1].ts == datetime(2026, 6, 8, tzinfo=UTC)
    assert agg[1].count == 50
    assert agg[1].truncated is False


def test_aggregate_month_ors_truncated_flags() -> None:
    windows = [
        _window("bitcoin", 1, 10, truncated=True),
        _window("bitcoin", 20, 20, truncated=False),
    ]
    agg = GdeltBridge._aggregate(windows, "month")
    assert len(agg) == 1
    assert agg[0].count == 30
    assert agg[0].truncated is True


def test_aggregate_day_passes_through() -> None:
    windows = [_window("bitcoin", 1, 250, truncated=True)]
    assert GdeltBridge._aggregate(windows, "day") == windows


# ── coverage_gap surfaces truncation ─────────────────────────────────────────


def test_coverage_gap_reports_truncated_flag_and_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("awareness.gdeltx.engine.utcnow", lambda: _FIXED_NOW)
    first = datetime(2026, 6, 8, tzinfo=UTC)
    last = datetime(2026, 6, 14, tzinfo=UTC)
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]

    async def fake_counts(self: GdeltBridge, term: str, start: object, end: object) -> list[GdeltWindow]:
        return [
            GdeltWindow(
                term=term,
                ts=day,
                count=300 if i == 2 else 0,
                truncated=term == "bigstory" and i == 2,
            )
            for i, day in enumerate(days)
        ]

    monkeypatch.setattr(GdeltBridge, "_gdelt_counts", fake_counts)
    bridge = GdeltBridge(_index(tmp_path), cache_dir=tmp_path / "cache")

    [report] = bridge.coverage_gap(["bigstory"], window_days=7)
    assert isinstance(report, GapReport)
    assert report.truncated is True
    assert report.note == "gdelt day(s) hit the 250-record cap; counts are a floor"
    assert report.gap is True  # 300 external vs 0 local

    [quiet] = bridge.coverage_gap(["quiet"], window_days=7)
    assert quiet.truncated is False
    assert quiet.note is None


# ── cache round-trip ─────────────────────────────────────────────────────────


def test_cache_round_trip_preserves_truncated_flag(tmp_path: Path) -> None:
    bridge = GdeltBridge(_index(tmp_path), cache_dir=tmp_path / "cache")
    start = datetime(2026, 6, 1, tzinfo=UTC)
    end = datetime(2026, 6, 2, 23, 59, 59, tzinfo=UTC)
    bridge._cache_write(
        bridge._cache_path("bitcoin", start, end, "day"),
        [_window("bitcoin", 1, 250, truncated=True), _window("bitcoin", 2, 5)],
    )
    windows = bridge.gdelt_query("bitcoin", start, end)
    assert [w.count for w in windows] == [250, 5]
    assert [w.truncated for w in windows] == [True, False]
    # Also via the raw reader.
    cached = bridge._cache_read(bridge._cache_path("bitcoin", start, end, "day"))
    assert cached is not None
    assert cached[0].truncated is True
    assert cached[1].truncated is False


def test_floor_day_sanity() -> None:
    """Guards the Monday assumption the aggregate tests rely on."""
    assert floor_to_day(datetime(2026, 6, 1, tzinfo=UTC)).weekday() == 0
    assert floor_to_day(datetime(2026, 6, 8, tzinfo=UTC)).weekday() == 0
