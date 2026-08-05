"""Unit tests for the qualityx time-series engine (per-bucket history + current).

Builds a small in-memory corpus over three calendar days (same JSONL-chunk
pattern as the rest of the unit suite) with known in-bucket exact-duplicate
hashes, a **cross-day** shared hash that must NOT count as a per-bucket
duplicate, and a new domain on the second capture day, then drives
:class:`~awareness.qualityx.engine.QualityTimeEngine` against it.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from awareness.qualityx.engine import QualityTimeEngine
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

BASE = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

# (idx, domain, ts, text, content_hash, parent_doc_or_dup_group)
# Day 1 (06-01): docs 1+2 exact-dup pair (h1); docs 3+4 unique (doc 4 carries a
# parent group but has no sibling in its bucket -> not a near-dup). Domains
# first seen: news/blog/markets.
# Day 2 (06-02): empty (zero-filled bucket in between).
# Day 3 (06-03): 3 unique docs; defi.example is the only NEW domain. Doc 7
# reuses day-1's hash h1 — a CROSS-day pair that must not count as a duplicate
# in either bucket.
_CORPUS: tuple[tuple[int, str, datetime, str, str, str | None], ...] = (
    (1, "news.example", BASE, "alpha one", "h1", None),
    (2, "news.example", BASE + timedelta(hours=1), "alpha two", "h1", None),
    (3, "blog.example", BASE + timedelta(hours=2), "alpha three", "h3", None),
    (4, "markets.example", BASE + timedelta(hours=3), "alpha four", "h4", "grpA"),
    (5, "markets.example", BASE + timedelta(days=2), "beta one", "h5", None),
    (6, "news.example", BASE + timedelta(days=2, hours=1), "beta two", "h6", None),
    (7, "defi.example", BASE + timedelta(days=2, hours=2), "beta three", "h1", None),
)

_DAY1 = date(2026, 6, 1)
_DAY2 = date(2026, 6, 2)
_DAY3 = date(2026, 6, 3)


def _write_doc(
    root: Path,
    idx: int,
    *,
    ts: datetime,
    text: str,
    domain: str,
    content_hash: str | None,
    parent_doc_or_dup_group: str | None = None,
) -> None:
    day = root / "captures" / f"{ts:%Y}" / f"{ts:%m}" / f"{ts:%d}"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}",
        capture_id=f"cap-{idx:04d}",
        source_type="rss",
        domain=domain,
        url=f"https://{domain}/{idx}",
        fetch_ts=ts.isoformat(),
        observed_ts=ts.isoformat(),
        title=f"doc {idx}",
        text=text,
        language="en",
        content_hash=content_hash,
        parent_doc_or_dup_group=parent_doc_or_dup_group,
    )
    (day / f"chunk-{idx:04d}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _write_corpus(root: Path) -> None:
    for idx, domain, ts, text, content_hash, parent in _CORPUS:
        _write_doc(
            root, idx, ts=ts, text=text, domain=domain,
            content_hash=content_hash, parent_doc_or_dup_group=parent,
        )


def _engine(tmp_path: Path) -> QualityTimeEngine:
    return QualityTimeEngine(
        DuckDbIndex(
            db_path=tmp_path / "duckdb" / "metadata.duckdb",
            jsonl_dir=tmp_path / "jsonl",
            iceberg_warehouse=None,
        )
    )


def _by_day(points: list) -> dict[date, object]:
    return {p.ts: p for p in points}


# ── per-day history ─────────────────────────────────────────────────────────


def test_history_per_day_totals_ratios_and_new_domains(tmp_path: Path) -> None:
    _write_corpus(tmp_path / "jsonl")
    engine = _engine(tmp_path)

    points = engine.history(days=7)
    by_day = _by_day(points)

    # Trailing window ending at floor(max(fetch_ts)): 05-28 .. 06-03.
    assert len(points) == 7
    assert points[0].ts == date(2026, 5, 28)
    assert points[-1].ts == _DAY3

    d1 = by_day[_DAY1]
    assert d1.total == 4
    assert d1.duplicate_ratio == pytest.approx(0.5)  # docs 1+2 share h1
    assert d1.near_duplicate_ratio == pytest.approx(0.0)  # grpA has no sibling
    assert d1.avg_length == pytest.approx(39 / 4)
    assert d1.new_domains == 3  # news, blog, markets first seen day 1
    assert d1.capture_rate == pytest.approx(4.0)

    d2 = by_day[_DAY2]
    assert d2.total == 0
    assert d2.duplicate_ratio == 0.0
    assert d2.near_duplicate_ratio == 0.0
    assert d2.new_domains == 0
    assert d2.capture_rate == pytest.approx(0.0)

    d3 = by_day[_DAY3]
    assert d3.total == 3
    assert d3.duplicate_ratio == 0.0  # h1 pair lives on day 1 — cross-day not counted
    assert d3.near_duplicate_ratio == 0.0
    assert d3.avg_length == pytest.approx(26 / 3)
    assert d3.new_domains == 1  # only defi.example
    assert d3.capture_rate == pytest.approx(3.0)

    # Leading days before the corpus are zeroed, never omitted.
    assert by_day[date(2026, 5, 30)].total == 0
    assert by_day[date(2026, 5, 30)].duplicate_ratio == 0.0


def test_history_duplicates_are_bucket_scoped(tmp_path: Path) -> None:
    """A hash shared across days counts in neither bucket (doc 7 vs docs 1/2)."""
    _write_corpus(tmp_path / "jsonl")
    engine = _engine(tmp_path)

    points = engine.history(days=3)
    by_day = _by_day(points)
    assert by_day[_DAY1].duplicate_ratio == pytest.approx(0.5)
    assert by_day[_DAY3].duplicate_ratio == 0.0


def test_history_exact_window_covers_only_corpus_days(tmp_path: Path) -> None:
    _write_corpus(tmp_path / "jsonl")
    engine = _engine(tmp_path)

    points = engine.history(days=3)
    assert [p.ts for p in points] == [_DAY1, _DAY2, _DAY3]
    assert [p.total for p in points] == [4, 0, 3]


def test_history_empty_corpus_is_zeroed(tmp_path: Path) -> None:
    engine = _engine(tmp_path)

    points = engine.history(days=3)
    assert len(points) == 3
    assert all(p.total == 0 for p in points)
    assert all(p.duplicate_ratio == 0.0 for p in points)
    assert all(p.near_duplicate_ratio == 0.0 for p in points)
    assert all(p.avg_length == 0.0 for p in points)
    assert all(p.new_domains == 0 for p in points)
    assert all(p.capture_rate == 0.0 for p in points)

    assert len(engine.history()) == 30  # default window


def test_history_window_clamp(tmp_path: Path) -> None:
    _write_corpus(tmp_path / "jsonl")
    engine = _engine(tmp_path)

    assert len(engine.history(days=0)) == 1
    assert len(engine.history(days=-5)) == 1
    assert len(engine.history(days=500)) == 365


def test_history_granularity_week_and_month(tmp_path: Path) -> None:
    _write_corpus(tmp_path / "jsonl")
    engine = _engine(tmp_path)

    weeks = engine.history(days=30, granularity="week")
    assert [p.ts for p in weeks] == [date(2026, 5, 4), date(2026, 5, 11),
                                     date(2026, 5, 18), date(2026, 5, 25),
                                     date(2026, 6, 1)]
    last_week = weeks[-1]
    assert last_week.total == 7
    assert last_week.duplicate_ratio == pytest.approx(3 / 7)  # docs 1,2,7 share h1 in-week
    assert last_week.new_domains == 4
    assert last_week.capture_rate == pytest.approx(1.0)  # 7 docs / 7 bucket days

    months = engine.history(days=30, granularity="month")
    assert [p.ts for p in months] == [date(2026, 5, 1), date(2026, 6, 1)]
    assert months[-1].total == 7
    assert months[-1].new_domains == 4
    assert months[-1].capture_rate == pytest.approx(7 / 30)  # June has 30 days


def test_history_rejects_unknown_granularity(tmp_path: Path) -> None:
    _write_corpus(tmp_path / "jsonl")
    engine = _engine(tmp_path)

    with pytest.raises(ValueError):
        engine.history(days=7, granularity="hour")


# ── current snapshot (delegated) ────────────────────────────────────────────


def test_current_delegates_to_corpusx_snapshot(tmp_path: Path) -> None:
    _write_corpus(tmp_path / "jsonl")
    engine = _engine(tmp_path)

    snap = engine.current()
    assert snap["total_captures"] == 7
    assert snap["duplicate_ratio"] == pytest.approx(3 / 7)  # corpus-wide: docs 1,2,7
    assert snap["near_duplicate_ratio"] == 0.0
    assert snap["avg_length"] == pytest.approx(65 / 7)
    assert set(snap) == {
        "total_captures", "empty_text", "duplicate_ratio", "near_duplicate_ratio",
        "avg_length", "languages", "top_domains", "dedup_group_count",
        "capture_rate_per_day",
    }


def test_current_empty_corpus_is_zeroed(tmp_path: Path) -> None:
    engine = _engine(tmp_path)

    snap = engine.current()
    assert snap["total_captures"] == 0
    assert snap["duplicate_ratio"] == 0.0
    assert snap["languages"] == {}
    assert snap["top_domains"] == []
