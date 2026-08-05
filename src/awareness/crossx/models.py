"""Pydantic models for the topic-lifecycle x X-sentiment cross-view.

The :class:`CombinedView` response mirrors :class:`~awareness.crossx.engine.CrossXEngine.combined_view`
1:1 so the router can expose it as a typed FastAPI response. ``news_series``
reuses the analytics :class:`~awareness.analytics.models.TimeBucket` shape;
both sentiment series use the :class:`SentimentPoint` shape (day + average
score) so the two sides stay directly chartable side by side.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from awareness.analytics.models import TimeBucket


class SentimentPoint(BaseModel):
    """Average sentiment score for one calendar day (UTC)."""

    ts: datetime
    avg_score: float = Field(ge=-1.0, le=1.0)


class CombinedView(BaseModel):
    """News lifecycle + news sentiment aligned with X session sentiment.

    ``news_series`` is the zero-filled daily doc-count series of the term
    (the lifecycle counts); ``news_sentiment`` / ``x_sentiment`` are the
    per-day average scores, aligned to the same calendar days (zero-filled
    on days without data). ``x_sentiment`` is ``None`` when the X session
    is unknown or the X store is unavailable — the news side alone is still
    returned, with an explanatory ``note``.

    ``news_avg_score`` / ``x_avg_score`` are the window means of the
    zero-filled daily series; ``correlation_r`` is the Pearson correlation
    between the two daily series (0.0 when either side is missing or has
    zero variance); ``convergence`` is the rule-based verdict
    (``aligned bullish`` / ``aligned bearish`` / ``divergence`` /
    ``neutral``).
    """

    term: str
    news_phase: str
    news_series: list[TimeBucket]
    news_sentiment: list[SentimentPoint]
    x_sentiment: list[SentimentPoint] | None
    news_avg_score: float
    x_avg_score: float
    correlation_r: float
    convergence: str
    note: str
