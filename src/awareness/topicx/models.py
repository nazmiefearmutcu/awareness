"""Pydantic models for the topic-lifecycle / source-impact subsystem.

Response models mirror :class:`~awareness.topicx.engine.TopicEngine` outputs
1:1 so the router can expose them as typed FastAPI responses. Request models
carry input validation (term length, window bounds, clamped limits) so the
router can translate validation failures into deterministic HTTP 400s.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from awareness.analytics.models import TimeBucket

# ── response models ──────────────────────────────────────────────────────────

# Lifecycle phases, ordered by precedence in TopicEngine.lifecycle:
# PEAKING → EXPANDING → EMERGING → DECLINING → DORMANT → STABLE.
TopicPhase = Literal[
    "EMERGING", "EXPANDING", "PEAKING", "DECLINING", "DORMANT", "STABLE"
]


class TopicLifecycle(BaseModel):
    """Lifecycle of a single term over the capture window.

    ``counts`` is the zero-filled daily series (reuses the analytics
    :class:`~awareness.analytics.models.TimeBucket` shape); ``slope_7d`` is
    the numpy polyfit slope over the trailing 7 buckets (0.0 for a
    zero-variance tail); ``first_seen``/``last_seen`` are the first/last day
    with a nonzero count (``None`` when the term never appears in the
    window).
    """

    term: str
    phase: TopicPhase
    counts: list[TimeBucket]
    slope_7d: float
    peak_count: int
    peak_date: datetime | None
    first_seen: datetime | None
    last_seen: datetime | None


class EmergingTopic(BaseModel):
    """A corpus-wide term first seen in the trailing days, ranked by volume.

    ``first_seen`` is the day of its first mention; ``domains_covered`` is
    the number of distinct domains that published it.
    """

    term: str
    count: int
    first_seen: datetime
    domains_covered: int


class SourceImpact(BaseModel):
    """Origin-domain impact: how much a domain's output gets copied.

    ``impact_score`` blends the volume of replicated copies with the
    domain's own capture footprint (``captures``); ``replica_edges`` is the
    number of distinct (origin → replica) relationships; ``avg_lead_minutes``
    is the average head start of the origin over its replicas.
    """

    domain: str
    impact_score: float
    captures: int
    replica_edges: int
    avg_lead_minutes: float


class TopicDominance(BaseModel):
    """Per-domain share of the docs that mention a term.

    ``doc_fraction`` is ``doc_count`` divided by the total matching docs in
    the window (fractions of a complete result set sum to 1.0);
    ``avg_sentiment`` is the mean lexicon score over the docs' first 200
    characters.
    """

    domain: str
    doc_count: int
    doc_fraction: float
    avg_sentiment: float


# ── request models ───────────────────────────────────────────────────────────

_MAX_LIMIT = 500


class LifecycleRequest(BaseModel):
    """GET /topicx/lifecycle query parameters."""

    term: str = Field(min_length=1, max_length=200)
    window_days: int = Field(30, ge=1, le=365)


class EmergingRequest(BaseModel):
    """GET /topicx/emerging query parameters."""

    window_days: int = Field(7, ge=1, le=365)
    limit: int = Field(20)

    @field_validator("limit")
    @classmethod
    def _clamp_limit(cls, value: int) -> int:
        return min(max(int(value), 1), _MAX_LIMIT)


class ImpactRequest(BaseModel):
    """GET /topicx/impact query parameters."""

    window_days: int = Field(30, ge=1, le=365)
    limit: int = Field(20)

    @field_validator("limit")
    @classmethod
    def _clamp_limit(cls, value: int) -> int:
        return min(max(int(value), 1), _MAX_LIMIT)


class DominanceRequest(BaseModel):
    """GET /topicx/dominance query parameters."""

    term: str = Field(min_length=1, max_length=200)
    window_days: int = Field(14, ge=1, le=365)
