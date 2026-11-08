"""Typed records for the markets layer — the contracts every module shares.

Rules
-----
* Every datetime is timezone-aware UTC (naive inputs are assumed UTC).
* Every record that came from outside carries :class:`ProviderRef` provenance.
* Nothing here invents values: optional fields stay ``None`` when unknown.
"""

from __future__ import annotations

from datetime import UTC, datetime
from datetime import date as Date
from datetime import time as Time
from typing import Any, Literal

import xxhash
from pydantic import BaseModel, ConfigDict, Field, field_validator

QualityLabel = Literal["premium", "standard", "low", "rejected"]
ContentType = Literal["news", "analysis", "opinion", "press_release", "official", "filing", "social"]
EntityType = Literal[
    "equity", "crypto", "fx", "commodity", "index", "country", "central_bank", "person"
]
SentimentLabel = Literal["positive", "negative", "neutral"]
PublisherKind = Literal[
    "wire",
    "financial_press",
    "general_press",
    "specialist",
    "official",
    "regulator",
    "press_release",
    "aggregator",
    "community",
]
ProvenanceBasis = Literal["native", "derived", "curated"]
HealthStatus = Literal["ok", "degraded", "down", "never"]

QUALITY_ORDER: dict[str, int] = {"rejected": 0, "low": 1, "standard": 2, "premium": 3}


def ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def stable_id(*parts: object) -> str:
    """Deterministic 128-bit hex id from the given parts (order matters)."""
    joined = "\x1f".join("" if p is None else str(p) for p in parts)
    return xxhash.xxh3_128_hexdigest(joined.encode("utf-8"))


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class _UtcModel(_Model):
    @field_validator("*", mode="after")
    @classmethod
    def _utc(cls, value: Any) -> Any:
        if isinstance(value, datetime):
            return ensure_utc(value)
        return value


class ProviderRef(_UtcModel):
    """Where a record (or field) came from."""

    provider: str
    url: str | None = None
    fetched_at: datetime
    basis: ProvenanceBasis = "native"


# ── publishers ────────────────────────────────────────────────────────────


class FeedSpec(_Model):
    id: str
    url: str
    categories: list[str] = Field(default_factory=list)
    poll_seconds: int = 180
    kind: Literal["rss", "atom", "gnews"] = "rss"
    language: str | None = None
    # "browser" for publishers that refuse non-browser user agents (verified per feed).
    profile: Literal["bot", "browser"] = "bot"


class Publisher(_Model):
    id: str
    name: str
    domain: str
    tier: int = Field(ge=1, le=5)
    kind: PublisherKind
    region: str = "GLOBAL"
    languages: list[str] = Field(default_factory=lambda: ["en"])
    paywalled: bool = False
    fetch_fulltext: bool = False
    aliases: list[str] = Field(default_factory=list)  # extra domains / brand names
    feeds: list[FeedSpec] = Field(default_factory=list)


# ── NLP outputs ───────────────────────────────────────────────────────────


class EntityRef(_Model):
    type: EntityType
    id: str
    name: str
    confidence: float = Field(ge=0.0, le=1.0)
    mentions: int = 1
    salience: float = Field(default=0.0, ge=0.0, le=1.0)


class SentimentScore(_Model):
    label: SentimentLabel
    score: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    model: str


# ── news ──────────────────────────────────────────────────────────────────


class RawNewsItem(_UtcModel):
    """What a news collector emits before enrichment."""

    url: str
    title: str
    summary: str | None = None
    published_at: datetime | None = None
    author: str | None = None
    feed_id: str
    publisher_id: str
    categories: list[str] = Field(default_factory=list)
    language: str | None = None
    body_text: str | None = None
    # For aggregator feeds (Google News, Yahoo): the publisher that wrote the story.
    origin_publisher_name: str | None = None
    origin_publisher_domain: str | None = None
    fetched_at: datetime | None = None


class NewsItem(_UtcModel):
    """An enriched, quality-scored news item."""

    id: str
    url: str
    title: str
    summary: str | None = None
    body_text: str | None = None
    excerpt: str | None = None
    publisher_id: str
    publisher_name: str
    publisher_domain: str
    publisher_tier: int = Field(ge=1, le=5)
    author: str | None = None
    language: str = "en"
    region: str | None = None
    published_at: datetime
    first_seen_at: datetime
    feed_id: str
    categories: list[str] = Field(default_factory=list)
    content_type: ContentType = "news"
    entities: list[EntityRef] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    asset_classes: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    finance_relevance: float = 0.0
    sentiment: SentimentScore | None = None
    entity_sentiment: dict[str, SentimentScore] = Field(default_factory=dict)
    quality_score: float = 0.0
    quality_label: QualityLabel = "low"
    quality_reasons: list[str] = Field(default_factory=list)
    importance: float = 0.0
    story_id: str | None = None
    is_paywalled: bool = False
    word_count: int = 0
    sources: list[ProviderRef] = Field(default_factory=list)

    @staticmethod
    def id_for_url(canonical_url: str) -> str:
        return stable_id("news", canonical_url)

    def public_dict(self) -> dict[str, Any]:
        """Wire shape: never ships the full body text."""
        data = self.model_dump(mode="json", exclude={"body_text"})
        return data


class Story(_UtcModel):
    id: str
    title: str
    lead_item_id: str
    item_ids: list[str] = Field(default_factory=list)
    publishers: list[str] = Field(default_factory=list)
    corroboration: int = 1
    first_seen_at: datetime
    last_updated_at: datetime
    symbols: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    sentiment: SentimentScore | None = None
    importance: float = 0.0


# ── calendars ─────────────────────────────────────────────────────────────


class EconEvent(_UtcModel):
    id: str
    country: str  # ISO-2; "EU" = euro area
    currency: str | None = None
    title: str
    category: str | None = None
    scheduled_at: datetime
    all_day: bool = False
    importance: int = Field(default=1, ge=1, le=3)
    actual: str | None = None
    forecast: str | None = None
    previous: str | None = None
    revised: str | None = None
    actual_value: float | None = None
    forecast_value: float | None = None
    previous_value: float | None = None
    unit: str | None = None
    surprise: float | None = None
    surprise_direction: Literal["above", "below", "inline"] | None = None
    status: Literal["scheduled", "released", "revised", "cancelled"] = "scheduled"
    description: str | None = None
    sources: list[ProviderRef] = Field(default_factory=list)
    updated_at: datetime | None = None


class CorpEvent(_UtcModel):
    id: str
    type: Literal["earnings", "dividend", "split", "ipo", "filing"]
    symbol: str | None = None
    company: str
    exchange: str | None = None
    date: Date
    time_of_day: Literal["bmo", "amc", "dmh", "unknown"] | None = None
    datetime_utc: datetime | None = None
    status: Literal[
        "scheduled", "confirmed", "estimated", "priced", "filed", "withdrawn", "completed"
    ] = "scheduled"
    fields: dict[str, Any] = Field(default_factory=dict)
    sources: list[ProviderRef] = Field(default_factory=list)
    updated_at: datetime | None = None


class Holiday(_UtcModel):
    id: str
    date: Date
    country: str | None = None
    exchange: str | None = None
    name: str
    local_name: str | None = None
    kind: Literal["public", "market_closed", "early_close"] = "public"
    early_close_time: Time | None = None  # exchange-local wall time
    source: ProviderRef


# ── market sentiment indicators ───────────────────────────────────────────


class IndicatorPoint(_UtcModel):
    indicator: str
    ts: datetime
    value: float
    label: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    source: ProviderRef


# ── operations ────────────────────────────────────────────────────────────


class ProviderHealth(_UtcModel):
    provider: str
    last_attempt_at: datetime | None = None
    last_ok_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    items_last_ok: int = 0
    expected_interval_s: int = 3600
    status: HealthStatus = "never"

    def derive_status(self, now: datetime) -> HealthStatus:
        if self.last_ok_at is None:
            return "down" if self.last_error_at is not None else "never"
        age = (ensure_utc(now) - self.last_ok_at).total_seconds()  # type: ignore[operator]
        interval = max(self.expected_interval_s, 60)
        if age <= 3 * interval and self.consecutive_failures < 3:
            return "ok"
        if age <= 12 * interval:
            return "degraded"
        return "down"
