"""Pydantic models for the analytics subsystem (requests and responses).

Response models mirror engine outputs 1:1. Request models carry input
validation (term length, window bounds, clamped limits) so the router can
translate validation failures into deterministic HTTP 400s.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# ── response models ──────────────────────────────────────────────────────────


class TimeBucket(BaseModel):
    """Document count for one time bucket (UTC)."""

    ts: datetime
    count: int


class TermCount(BaseModel):
    """A term and how often it appears in the scanned corpus."""

    term: str
    count: int


class Spike(BaseModel):
    """A single bucket flagged as an anomalous burst for a term.

    ``mean``/``std`` describe the window the z-score was computed against;
    ``vs_mean`` is ``count - mean`` (signed magnitude of the burst).
    """

    bucket: datetime
    count: int
    zscore: float
    mean: float
    std: float
    vs_mean: float


class DomainCount(BaseModel):
    """Capture count grouped by domain."""

    domain: str
    count: int


class LanguageCount(BaseModel):
    """Capture count grouped by (primary) language tag; ``None`` for undetected."""

    language: str | None
    count: int


# ── request models ───────────────────────────────────────────────────────────

_MAX_LIMIT = 500
_MAX_MIN_COUNT = 1000

_GRANULARITY = Literal["day", "week", "month"]


class TermFrequencyRequest(BaseModel):
    """GET /analytics/term-frequency query parameters."""

    term: str = Field(min_length=1, max_length=200)
    window_days: int = Field(7, ge=1, le=365)
    granularity: _GRANULARITY = "day"
    start: datetime | None = None
    end: datetime | None = None


class TopTermsRequest(BaseModel):
    """GET /analytics/top-terms query parameters."""

    limit: int = Field(50)
    min_count: int = Field(2, ge=1)

    @field_validator("limit")
    @classmethod
    def _clamp_limit(cls, value: int) -> int:
        return min(max(int(value), 1), _MAX_LIMIT)

    @field_validator("min_count")
    @classmethod
    def _clamp_min_count(cls, value: int) -> int:
        return min(max(int(value), 1), _MAX_MIN_COUNT)


class SpikesRequest(BaseModel):
    """GET /analytics/spikes query parameters."""

    term: str = Field(min_length=1, max_length=200)
    window_days: int = Field(14, ge=1, le=365)
    zscore_threshold: float = Field(2.5, gt=0.0)
    min_absolute: int = Field(3, ge=1)


class DomainBreakdownRequest(BaseModel):
    """GET /analytics/domains query parameters."""

    limit: int = Field(20)
    start: datetime | None = None
    end: datetime | None = None

    @field_validator("limit")
    @classmethod
    def _clamp_limit(cls, value: int) -> int:
        return min(max(int(value), 1), _MAX_LIMIT)


class LanguageBreakdownRequest(BaseModel):
    """GET /analytics/languages query parameters."""

    limit: int = Field(20)

    @field_validator("limit")
    @classmethod
    def _clamp_limit(cls, value: int) -> int:
        return min(max(int(value), 1), _MAX_LIMIT)


class CoOccurringRequest(BaseModel):
    """GET /analytics/co-occurring query parameters."""

    term: str = Field(min_length=1, max_length=200)
    limit: int = Field(50)

    @field_validator("limit")
    @classmethod
    def _clamp_limit(cls, value: int) -> int:
        return min(max(int(value), 1), _MAX_LIMIT)
