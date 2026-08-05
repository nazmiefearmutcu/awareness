"""Granularity alignment tests for qualityx week/month history buckets.

Probes the engine's SQL bucket keys (``date_trunc('week'/'month')``) against
the Python calendar arithmetic (``analytics.engine._iter_buckets``): week
buckets must start on UTC Mondays, month buckets on the 1st, and
``new_domains`` must be bucketed at the chosen granularity. A crafted corpus
spans two ISO weeks (Tue + Sun + next Mon) and two months; cross-bucket dup
hashes must not count. The API surface is covered too (400 on bad
granularity).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from awareness.qualityx.engine import QualityTimeEngine
from awareness.qualityx.router import create_qualityx_router
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
    ts: datetime,
    text: str,
    domain: str,
    content_hash: str | None = None,
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
    )
    (day / f"chunk-{idx:04d}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


# Fixed dates (UTC): 2026-06-01 is a Monday; 2026-05-26 a Tuesday;
# 2026-05-31 a Sunday — all three share the ISO week starting Mon 2026-05-25
# except the Monday itself, which opens the next ISO week.
_TUE = datetime(2026, 5, 26, 12, 0, tzinfo=UTC)
_SUN = datetime(2026, 5, 31, 12, 0, tzinfo=UTC)
_MON = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
_MID_MAY = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)

# Week of Mon 05-25: Tue pair (same hash), Sun doc; week of Mon 06-01: Mon
# pair (same hash) + a cross-week hash reuse that must NOT count in-bucket.
# Month view: May gets the Tue/Sun/Mid docs, June the Mon docs.
_CORPUS: tuple[tuple[int, str, datetime, str, str | None], ...] = (
    (1, "tue.example", _TUE, "alpha one", "htue"),
    (2, "tue.example", _TUE.replace(hour=13), "alpha two", "htue"),
    (3, "sun.example", _SUN, "beta one", "hsun"),
    (4, "mon.example", _MON, "gamma one", "hmon"),
    (5, "mon.example", _MON.replace(hour=13), "gamma two", "hmon"),
    (6, "mid.example", _MID_MAY, "delta one", "hmid"),
    (7, "mon.example", _MON.replace(hour=14), "gamma three", "htue"),
)


def _write_corpus(root: Path) -> None:
    for idx, domain, ts, text, content_hash in _CORPUS:
        _write_doc(root, idx, ts=ts, text=text, domain=domain, content_hash=content_hash)


def _index(tmp_path: Path) -> DuckDbIndex:
    return DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )


def _by_bucket(points: list) -> dict[date, object]:
    return {p.ts: p for p in points}


# ── week: Monday-aligned buckets ────────────────────────────────────────────


def test_week_buckets_are_monday_aligned(tmp_path: Path) -> None:
    _write_corpus(tmp_path / "jsonl")

    points = QualityTimeEngine(_index(tmp_path)).history(days=14, granularity="week")
    by_bucket = _by_bucket(points)

    # Every bucket key is a Monday; the window end (Mon 06-01) is included.
    assert [p.ts for p in points] == [date(2026, 5, 18), date(2026, 5, 25), date(2026, 6, 1)]
    assert all(p.ts.weekday() == 0 for p in points)

    # Tue + Sun docs land in the week of their Monday (05-25), not their day.
    assert by_bucket[date(2026, 5, 25)].total == 3
    assert by_bucket[date(2026, 6, 1)].total == 3
    assert by_bucket[date(2026, 5, 18)].total == 0

    # capture_rate spans the full 7-day bucket, not one day.
    assert by_bucket[date(2026, 5, 25)].capture_rate == pytest.approx(3 / 7)
    assert by_bucket[date(2026, 5, 18)].capture_rate == 0.0


def test_week_duplicates_are_bucket_scoped(tmp_path: Path) -> None:
    """In-week pairs count; a hash reused across weeks counts in neither."""
    _write_corpus(tmp_path / "jsonl")

    points = QualityTimeEngine(_index(tmp_path)).history(days=14, granularity="week")
    by_bucket = _by_bucket(points)

    # Week 05-25: docs 1+2 share htue in-bucket → 2/3; doc 3 unique.
    assert by_bucket[date(2026, 5, 25)].duplicate_ratio == pytest.approx(2 / 3)
    # Week 06-01: docs 4+5 share hmon → 2/3; doc 7's htue pairs with docs 1+2
    # in the *other* week, so it is not a duplicate in this bucket.
    assert by_bucket[date(2026, 6, 1)].duplicate_ratio == pytest.approx(2 / 3)


def test_week_new_domains_bucketed_at_week_granularity(tmp_path: Path) -> None:
    """A domain counts as new in the week of its first-ever capture."""
    _write_corpus(tmp_path / "jsonl")

    points = QualityTimeEngine(_index(tmp_path)).history(days=14, granularity="week")
    by_bucket = _by_bucket(points)

    # tue.example + sun.example first seen in the 05-25 week; mon.example in
    # the 06-01 week; mid.example first seen on 05-15 → its Monday (05-11) is
    # outside this 14-day window and correctly absent.
    assert by_bucket[date(2026, 5, 25)].new_domains == 2
    assert by_bucket[date(2026, 6, 1)].new_domains == 1
    assert by_bucket[date(2026, 5, 18)].new_domains == 0


# ── month: 1st-aligned buckets ──────────────────────────────────────────────


def test_month_buckets_first_aligned(tmp_path: Path) -> None:
    _write_corpus(tmp_path / "jsonl")

    points = QualityTimeEngine(_index(tmp_path)).history(days=60, granularity="month")
    by_bucket = _by_bucket(points)

    # Every bucket key is the 1st of a month; May and June both covered.
    assert [p.ts for p in points] == [date(2026, 4, 1), date(2026, 5, 1), date(2026, 6, 1)]
    assert all(p.ts.day == 1 for p in points)

    # Mid-May, Tue and Sun docs all land in the May bucket.
    assert by_bucket[date(2026, 5, 1)].total == 4
    assert by_bucket[date(2026, 6, 1)].total == 3
    assert by_bucket[date(2026, 4, 1)].total == 0

    # capture_rate uses the bucket's own calendar span (May 31 days).
    assert by_bucket[date(2026, 5, 1)].capture_rate == pytest.approx(4 / 31)
    assert by_bucket[date(2026, 6, 1)].capture_rate == pytest.approx(3 / 30)


def test_month_duplicates_are_bucket_scoped(tmp_path: Path) -> None:
    """May htue pair counts; doc 7's June reuse of htue counts in neither month."""
    _write_corpus(tmp_path / "jsonl")

    points = QualityTimeEngine(_index(tmp_path)).history(days=60, granularity="month")
    by_bucket = _by_bucket(points)

    assert by_bucket[date(2026, 5, 1)].duplicate_ratio == pytest.approx(2 / 4)
    assert by_bucket[date(2026, 6, 1)].duplicate_ratio == pytest.approx(2 / 3)


def test_month_new_domains_bucketed_at_month_granularity(tmp_path: Path) -> None:
    _write_corpus(tmp_path / "jsonl")

    points = QualityTimeEngine(_index(tmp_path)).history(days=60, granularity="month")
    by_bucket = _by_bucket(points)

    # tue/sun/mid first seen in May → May bucket; mon.example → June bucket.
    assert by_bucket[date(2026, 5, 1)].new_domains == 3
    assert by_bucket[date(2026, 6, 1)].new_domains == 1
    assert by_bucket[date(2026, 4, 1)].new_domains == 0


# ── API surface ─────────────────────────────────────────────────────────────


def test_api_week_and_month_granularity_ok(tmp_path: Path) -> None:
    _write_corpus(tmp_path / "jsonl")
    index = _index(tmp_path)
    app = FastAPI()
    app.include_router(create_qualityx_router(index))
    with TestClient(app) as client:
        week = client.get("/qualityx/history", params={"days": 14, "granularity": "week"})
        month = client.get("/qualityx/history", params={"days": 60, "granularity": "month"})
    assert week.status_code == 200
    assert [p["ts"] for p in week.json()["points"]] == [
        "2026-05-18", "2026-05-25", "2026-06-01",
    ]
    assert month.status_code == 200
    assert [p["ts"] for p in month.json()["points"]] == [
        "2026-04-01", "2026-05-01", "2026-06-01",
    ]


def test_api_bad_granularity_is_400(tmp_path: Path) -> None:
    _write_corpus(tmp_path / "jsonl")
    index = _index(tmp_path)
    app = FastAPI()
    app.include_router(create_qualityx_router(index))
    with TestClient(app) as client:
        res = client.get("/qualityx/history", params={"granularity": "weekly"})
    assert res.status_code == 400
    assert "granularity" in res.json()["detail"]
