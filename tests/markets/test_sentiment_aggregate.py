"""Per-entity sentiment aggregation: buckets, weights, momentum, dispersion."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from awareness.markets.sentiment.aggregate import summarize_entity_sentiment
from awareness.markets.store import EntitySentimentRow

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def _row(
    news_id: str,
    published: datetime,
    *,
    score: float,
    label: str | None = None,
    confidence: float = 0.8,
    quality: float = 1.0,
    publisher: str = "reuters",
    entity: str = "NVDA",
) -> EntitySentimentRow:
    if label is None:
        label = "positive" if score > 0.15 else "negative" if score < -0.15 else "neutral"
    return EntitySentimentRow(
        news_id=news_id,
        entity_id=entity,
        score=score,
        label=label,
        confidence=confidence,
        published_at=published,
        quality_score=quality,
        publisher_id=publisher,
    )


def test_empty_window_returns_null_statistics() -> None:
    report = summarize_entity_sentiment(
        [], entity_id="NVDA", now=NOW, window=timedelta(days=1), bucket="1h"
    )
    assert report.count == 0
    assert report.series == []
    assert report.mean is None
    assert report.weighted_mean is None
    assert report.positive_share is None
    assert report.negative_share is None
    assert report.momentum_z is None
    assert report.dispersion is None
    assert report.latest_at is None
    assert report.publishers == 0


def test_hourly_buckets_floor_to_hour_and_skip_empty_hours() -> None:
    rows = [
        _row("a", datetime(2026, 11, 8, 10, 5, tzinfo=UTC), score=0.5),
        _row("b", datetime(2026, 11, 8, 10, 40, tzinfo=UTC), score=-0.5),
        _row("c", datetime(2026, 11, 8, 11, 15, tzinfo=UTC), score=0.2),
    ]
    report = summarize_entity_sentiment(rows, entity_id="NVDA", now=NOW, window=timedelta(days=1), bucket="1h")
    assert [b.ts for b in report.series] == [
        datetime(2026, 11, 8, 10, tzinfo=UTC),
        datetime(2026, 11, 8, 11, tzinfo=UTC),
    ]
    first = report.series[0]
    assert first.count == 2
    assert first.mean == pytest.approx(0.0)
    assert first.positive == 1 and first.negative == 1 and first.neutral == 0
    assert report.series[1].count == 1


def test_daily_buckets_floor_to_utc_midnight() -> None:
    rows = [
        _row("a", datetime(2026, 11, 7, 3, 0, tzinfo=UTC), score=0.4),
        _row("b", datetime(2026, 11, 7, 22, 0, tzinfo=UTC), score=0.6),
        _row("c", datetime(2026, 11, 8, 9, 0, tzinfo=UTC), score=-0.2),
    ]
    report = summarize_entity_sentiment(rows, entity_id="NVDA", now=NOW, window=timedelta(days=3), bucket="1d")
    assert [b.ts for b in report.series] == [
        datetime(2026, 11, 7, tzinfo=UTC),
        datetime(2026, 11, 8, tzinfo=UTC),
    ]
    assert report.series[0].count == 2
    assert report.series[0].mean == pytest.approx(0.5)


def test_weighted_mean_uses_confidence_times_quality() -> None:
    t = datetime(2026, 11, 8, 9, 0, tzinfo=UTC)
    rows = [
        _row("a", t, score=1.0, confidence=0.9, quality=3.0, publisher="reuters"),
        _row("b", t, score=-1.0, confidence=0.9, quality=1.0, publisher="blog"),
    ]
    report = summarize_entity_sentiment(rows, entity_id="NVDA", now=NOW, window=timedelta(days=1), bucket="1h")
    # weights 2.7 and 0.9 -> (2.7 - 0.9) / 3.6 = 0.5
    assert report.weighted_mean == pytest.approx(0.5)
    assert report.mean == pytest.approx(0.0)
    assert report.series[0].weighted_mean == pytest.approx(0.5)


def test_quality_below_one_counts_as_one() -> None:
    t = datetime(2026, 11, 8, 9, 0, tzinfo=UTC)
    rows = [
        _row("a", t, score=1.0, confidence=0.5, quality=0.0),
        _row("b", t, score=0.0, confidence=0.5, quality=0.5),
    ]
    report = summarize_entity_sentiment(rows, entity_id="NVDA", now=NOW, window=timedelta(days=1), bucket="1h")
    assert report.weighted_mean == pytest.approx(0.5)


def test_rows_outside_window_are_excluded() -> None:
    rows = [
        _row("old", NOW - timedelta(days=5), score=-1.0),
        _row("new", NOW - timedelta(hours=2), score=0.6),
        _row("future", NOW + timedelta(hours=1), score=-1.0),
    ]
    report = summarize_entity_sentiment(rows, entity_id="NVDA", now=NOW, window=timedelta(days=1), bucket="1h")
    assert report.count == 1
    assert report.mean == pytest.approx(0.6)


def test_duplicate_news_id_counts_once() -> None:
    t = NOW - timedelta(hours=1)
    rows = [_row("same", t, score=0.8), _row("same", t, score=0.8)]
    report = summarize_entity_sentiment(rows, entity_id="NVDA", now=NOW, window=timedelta(days=1), bucket="1h")
    assert report.count == 1


def test_shares_dispersion_and_publishers() -> None:
    t = NOW - timedelta(hours=1)
    rows = [
        _row("a", t, score=0.0, publisher="reuters"),
        _row("b", t, score=1.0, publisher="cnbc"),
        _row("c", t, score=-1.0, publisher="reuters"),
        _row("d", t, score=0.0, publisher="ft"),
    ]
    report = summarize_entity_sentiment(rows, entity_id="NVDA", now=NOW, window=timedelta(days=1), bucket="1h")
    assert report.count == 4
    assert report.positive_share == pytest.approx(0.25)
    assert report.negative_share == pytest.approx(0.25)
    assert report.publishers == 3
    assert report.latest_at == t
    # population stdev of [0, 1, -1, 0] = sqrt(0.5)
    assert report.dispersion == pytest.approx(0.5**0.5)


def test_dispersion_none_with_single_row() -> None:
    rows = [_row("a", NOW - timedelta(hours=1), score=0.3)]
    report = summarize_entity_sentiment(rows, entity_id="NVDA", now=NOW, window=timedelta(days=1), bucket="1h")
    assert report.dispersion is None


def test_momentum_z_against_baseline() -> None:
    baseline_start = NOW - timedelta(days=30)
    baseline = [
        _row(f"base{i}", baseline_start + timedelta(hours=i * 6), score=(0.1 if i % 2 else -0.1))
        for i in range(60)
    ]
    # baseline mean 0, population stdev 0.1
    recent = [
        _row("r1", NOW - timedelta(hours=3), score=0.3),
        _row("r2", NOW - timedelta(hours=5), score=0.3),
    ]
    rows = recent + baseline
    report = summarize_entity_sentiment(
        rows,
        entity_id="NVDA",
        now=NOW,
        window=timedelta(days=30),
        bucket="1d",
        baseline_rows=baseline,
    )
    assert report.momentum_z == pytest.approx(3.0)


def test_momentum_none_with_too_few_baseline_rows() -> None:
    baseline = [_row(f"b{i}", NOW - timedelta(days=2, hours=i), score=0.1 * (i % 3)) for i in range(9)]
    rows = [_row("r", NOW - timedelta(hours=1), score=0.5)]
    report = summarize_entity_sentiment(
        rows, entity_id="NVDA", now=NOW, window=timedelta(days=3), bucket="1h", baseline_rows=baseline
    )
    assert report.momentum_z is None


def test_momentum_none_with_zero_baseline_std() -> None:
    baseline = [_row(f"b{i}", NOW - timedelta(days=2, hours=i), score=0.2) for i in range(12)]
    rows = [_row("r", NOW - timedelta(hours=1), score=0.5)]
    report = summarize_entity_sentiment(
        rows, entity_id="NVDA", now=NOW, window=timedelta(days=3), bucket="1h", baseline_rows=baseline
    )
    assert report.momentum_z is None


def test_momentum_none_without_recent_rows() -> None:
    baseline = [_row(f"b{i}", NOW - timedelta(days=2, hours=i), score=0.1 * (i % 2)) for i in range(12)]
    rows = [_row("r", NOW - timedelta(days=2), score=0.5)]
    report = summarize_entity_sentiment(
        rows, entity_id="NVDA", now=NOW, window=timedelta(days=3), bucket="1h", baseline_rows=baseline
    )
    assert report.momentum_z is None


def test_to_dict_is_json_ready() -> None:
    rows = [_row("a", NOW - timedelta(hours=1), score=0.4)]
    report = summarize_entity_sentiment(rows, entity_id="NVDA", now=NOW, window=timedelta(days=1), bucket="1h")
    data = report.to_dict()
    encoded = json.loads(json.dumps(data))
    assert encoded["entity_id"] == "NVDA"
    assert encoded["window_end"] == NOW.isoformat()
    assert encoded["series"][0]["ts"].endswith("+00:00")
    assert encoded["count"] == 1
    assert encoded["bucket"] == "1h"
