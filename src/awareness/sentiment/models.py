"""Pydantic models for the sentiment subsystem (requests and responses).

Response models mirror engine outputs 1:1. Request models carry input
validation (term length, window bounds) so the router can translate
validation failures into deterministic HTTP 400s.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# ── response models ──────────────────────────────────────────────────────────


class SentimentBucket(BaseModel):
    """Sentiment aggregate for one time bucket (UTC).

    ``doc_count`` is the number of docs containing the term in the bucket;
    ``pos_count`` / ``neg_count`` are docs whose score is > 0 / < 0;
    ``avg_score`` is the mean per-doc score ([-1, 1]).
    """

    ts: datetime
    doc_count: int = Field(ge=0)
    pos_count: int = Field(ge=0)
    neg_count: int = Field(ge=0)
    avg_score: float = Field(ge=-1.0, le=1.0)


class SentimentHeat(BaseModel):
    """Aggregate sentiment summary for a term over a window.

    ``sentiment_ratio`` is ``(pos_docs - neg_docs) / (pos_docs + neg_docs)``
    (0.0 when no scored docs); ``volatility`` is the std of per-day average
    scores; ``last_7d_trend`` is the linear slope of the last 7 daily
    average scores (0.0 when fewer than 2 days of data).
    """

    total_docs: int = Field(ge=0)
    pos_docs: int = Field(ge=0)
    neg_docs: int = Field(ge=0)
    sentiment_ratio: float = Field(ge=-1.0, le=1.0)
    volatility: float = Field(ge=0.0)
    last_7d_trend: float


class SentimentResult(BaseModel):
    """Full sentiment view for a term: bucketed series + heat summary.

    The heat fields (``total_docs`` … ``last_7d_trend``) are the
    :class:`SentimentHeat` aggregate computed over the same window as
    ``buckets``.
    """

    term: str
    buckets: list[SentimentBucket]
    total_docs: int = Field(ge=0)
    pos_docs: int = Field(ge=0)
    neg_docs: int = Field(ge=0)
    sentiment_ratio: float = Field(ge=-1.0, le=1.0)
    volatility: float = Field(ge=0.0)
    last_7d_trend: float


# ── request models ───────────────────────────────────────────────────────────

_GRANULARITY = Literal["day", "week", "month"]


class SentimentTermRequest(BaseModel):
    """GET /sentiment/term query parameters."""

    term: str = Field(min_length=1, max_length=200)
    window_days: int = Field(14, ge=1, le=365)
    granularity: _GRANULARITY = "day"


class SentimentHeatRequest(BaseModel):
    """GET /sentiment/heat query parameters."""

    term: str = Field(min_length=1, max_length=200)
    window_days: int = Field(30, ge=1, le=365)
