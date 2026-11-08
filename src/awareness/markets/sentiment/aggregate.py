"""Per-entity sentiment aggregation over ``news_entity_sentiment`` rows.

Only rows inside ``[now - window, now]`` count. Duplicate ``news_id`` rows for the same
entity count once. Buckets are UTC hour or day floors and include only buckets that have
rows, so no points are invented. Statistics with too few observations are ``None``.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from awareness.markets.models import ensure_utc
from awareness.markets.store import EntitySentimentRow

MIN_BASELINE_ROWS = 10
MOMENTUM_WINDOW = timedelta(hours=24)
_EPSILON = 1e-12  # population stdev of identical scores is float noise, not zero


@dataclass
class SentimentBucket:
    """Sentiment summary for one hour or day bucket."""

    ts: datetime
    mean: float
    weighted_mean: float
    count: int
    positive: int
    negative: int
    neutral: int


@dataclass
class EntitySentimentReport:
    """Aggregated sentiment for one entity over a window."""

    entity_id: str
    window_start: datetime
    window_end: datetime
    bucket: str
    series: list[SentimentBucket] = field(default_factory=list)
    count: int = 0
    mean: float | None = None
    weighted_mean: float | None = None
    positive_share: float | None = None
    negative_share: float | None = None
    momentum_z: float | None = None
    dispersion: float | None = None
    latest_at: datetime | None = None
    publishers: int = 0

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict with ISO-8601 timestamps."""
        data = asdict(self)
        data["window_start"] = self.window_start.isoformat()
        data["window_end"] = self.window_end.isoformat()
        data["latest_at"] = self.latest_at.isoformat() if self.latest_at else None
        data["series"] = [
            {**asdict(bucket), "ts": bucket.ts.isoformat()} for bucket in self.series
        ]
        return data


def _dedupe(rows: list[EntitySentimentRow]) -> list[EntitySentimentRow]:
    """Keep the first row per ``news_id``."""
    seen: set[str] = set()
    unique: list[EntitySentimentRow] = []
    for row in rows:
        if row.news_id in seen:
            continue
        seen.add(row.news_id)
        unique.append(row)
    return unique


def _weight(row: EntitySentimentRow) -> float:
    return row.confidence * max(row.quality_score, 1.0)


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _pstdev(values: list[float]) -> float | None:
    if not values:
        return None
    mu = sum(values) / len(values)
    return math.sqrt(sum((v - mu) ** 2 for v in values) / len(values))


def _weighted_mean(rows: list[EntitySentimentRow]) -> float | None:
    """Confidence x quality weighted mean. Falls back to the plain mean when all weights are zero."""
    if not rows:
        return None
    total = sum(_weight(r) for r in rows)
    if total <= 0:
        return _mean([r.score for r in rows])
    return sum(r.score * _weight(r) for r in rows) / total


def _floor(ts: datetime, bucket: Literal["1h", "1d"]) -> datetime:
    if bucket == "1d":
        return ts.replace(hour=0, minute=0, second=0, microsecond=0)
    return ts.replace(minute=0, second=0, microsecond=0)


def _as_utc(value: datetime) -> datetime:
    """Naive datetimes are assumed UTC, matching the store and models."""
    return ensure_utc(value) if value.tzinfo is None else value.astimezone(UTC)


def _bucket_summary(ts: datetime, rows: list[EntitySentimentRow]) -> SentimentBucket:
    weighted = _weighted_mean(rows)
    return SentimentBucket(
        ts=ts,
        mean=sum(r.score for r in rows) / len(rows),
        weighted_mean=weighted if weighted is not None else 0.0,
        count=len(rows),
        positive=sum(1 for r in rows if r.label == "positive"),
        negative=sum(1 for r in rows if r.label == "negative"),
        neutral=sum(1 for r in rows if r.label == "neutral"),
    )


def _momentum_z(
    in_window: list[EntitySentimentRow],
    baseline_rows: list[EntitySentimentRow] | None,
    now: datetime,
) -> float | None:
    """(mean of last 24 h - baseline mean) / baseline population stdev."""
    recent_start = now - MOMENTUM_WINDOW
    recent = [r for r in in_window if r.published_at >= recent_start]
    if not recent or not baseline_rows:
        return None
    recent_ids = {r.news_id for r in recent}
    baseline = [r.score for r in baseline_rows if r.news_id not in recent_ids]
    if len(baseline) < MIN_BASELINE_ROWS:
        return None
    base_mean = sum(baseline) / len(baseline)
    base_std = _pstdev(baseline)
    if base_std is None or base_std <= _EPSILON:
        return None
    recent_mean = sum(r.score for r in recent) / len(recent)
    return (recent_mean - base_mean) / base_std


def summarize_entity_sentiment(
    rows: list[EntitySentimentRow],
    *,
    entity_id: str,
    now: datetime,
    window: timedelta,
    bucket: Literal["1h", "1d"],
    baseline_rows: list[EntitySentimentRow] | None = None,
) -> EntitySentimentReport:
    """Summarise one entity's sentiment rows over ``[now - window, now]``.

    ``baseline_rows`` (typically the preceding 30 days for the same entity) feed the
    momentum z-score. Rows for other entities are ignored.
    """
    now_utc = _as_utc(now)
    window_start = now_utc - window
    wanted = entity_id.upper()

    scoped = [r for r in rows if r.entity_id.upper() == wanted]
    in_window = _dedupe(
        sorted(
            (r for r in scoped if window_start <= _as_utc(r.published_at) <= now_utc),
            key=lambda r: r.published_at,
        )
    )
    report = EntitySentimentReport(
        entity_id=wanted,
        window_start=window_start,
        window_end=now_utc,
        bucket=bucket,
        count=len(in_window),
    )
    if not in_window:
        return report

    groups: dict[datetime, list[EntitySentimentRow]] = {}
    for row in in_window:
        groups.setdefault(_floor(_as_utc(row.published_at), bucket), []).append(row)
    report.series = [_bucket_summary(ts, groups[ts]) for ts in sorted(groups)]

    scores = [r.score for r in in_window]
    total = len(in_window)
    report.mean = _mean(scores)
    report.weighted_mean = _weighted_mean(in_window)
    report.positive_share = sum(1 for r in in_window if r.label == "positive") / total
    report.negative_share = sum(1 for r in in_window if r.label == "negative") / total
    report.dispersion = _pstdev(scores) if total >= 2 else None
    report.latest_at = _as_utc(in_window[-1].published_at)
    report.publishers = len({r.publisher_id for r in in_window})
    report.momentum_z = _momentum_z(in_window, baseline_rows, now_utc)
    return report
