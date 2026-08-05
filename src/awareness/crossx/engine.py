"""Cross-view engine: topic lifecycle + news sentiment aligned with X sentiment.

:class:`CrossXEngine` combines two existing subsystems into one chartable
view:

* News side — :class:`~awareness.topicx.engine.TopicEngine.lifecycle` for the
  phase + daily counts, and
  :class:`~awareness.sentiment.engine.SentimentEngine.term_sentiment_over_time`
  for the per-day average score of the term's news docs.
* X side — :func:`~awareness.xscraper.analyze.session_timeline` for the
  per-day average score of a scraper session's tweets, read through a
  :class:`~awareness.xscraper.store.SessionStore` opened at the configured
  ``{data_dir}/xscraper.sqlite`` path.

Both series are aligned by calendar day (zero-filled over the news window),
so the correlation and the convergence verdict compare apples to apples.
The X side is optional: an unknown session (or no store path) yields
``x_sentiment=None`` and a news-only view with an explanatory note, never an
exception.

Design notes:

* The store is aiosqlite-backed, so the X-side methods are async and the
  store connection is opened/closed per call — no process-wide singleton to
  leak worker threads.
* ``correlation_r`` is the numpy Pearson correlation of the zero-filled
  daily series; zero-variance on either side yields ``0.0`` (never NaN).
* Convergence rules on the sign of both window means: same positive sign →
  ``aligned bullish``, same negative sign → ``aligned bearish``, opposite
  signs → ``divergence``, anything involving a zero (or a missing X side) →
  ``neutral``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from awareness.crossx.models import CombinedView, SentimentPoint
from awareness.obs.logging import get_logger
from awareness.sentiment.engine import SentimentEngine
from awareness.topicx.engine import TopicEngine
from awareness.xscraper.analyze import session_timeline
from awareness.xscraper.store import SessionStore

logger = get_logger("crossx.engine")

_MAX_TERM_LEN = 200
_MAX_SESSION_LEN = 200


def _validate_term(term: str) -> str:
    """Strip + validate a term; raise :class:`ValueError` when unusable."""
    cleaned = (term or "").strip()
    if not cleaned:
        raise ValueError("term must not be empty")
    if len(cleaned) > _MAX_TERM_LEN:
        raise ValueError(f"term must be at most {_MAX_TERM_LEN} characters")
    return cleaned


def _validate_session_id(session_id: str) -> str:
    """Strip + validate a session id; raise :class:`ValueError` when unusable."""
    cleaned = (session_id or "").strip()
    if not cleaned:
        raise ValueError("session_id must not be empty")
    if len(cleaned) > _MAX_SESSION_LEN:
        raise ValueError(f"session_id must be at most {_MAX_SESSION_LEN} characters")
    return cleaned


def _mean(values: list[float]) -> float:
    """Mean of *values* (0.0 for an empty list)."""
    return float(np.mean(values)) if values else 0.0


# Minimum days where AT LEAST ONE series has data before a nonzero
# correlation is surfaced (W22-F2: zero-filled sparse series otherwise
# inflate r to ±1.0 from a single shared day).
_MIN_OVERLAP_DAYS = 3


def _correlation_r(news: list[float], x: list[float]) -> float:
    """Pearson correlation over days where BOTH series have data.

    Zero-filled days would inflate r to ±1.0 from a single shared day; days
    where only ONE side has data must also be excluded (padding them with
    zeros fabricates correlation between series that never co-occurred —
    W26-F1). :data:`_MIN_OVERLAP_DAYS` counts shared-data days only.
    """
    a = np.asarray(news, dtype=np.float64)
    b = np.asarray(x, dtype=np.float64)
    if a.size < 2 or b.size < 2:
        return 0.0
    mask = (a != 0.0) & (b != 0.0)
    if int(mask.sum()) < _MIN_OVERLAP_DAYS:
        return 0.0
    a_m, b_m = a[mask], b[mask]
    if a_m.size < 2 or b_m.size < 2:
        return 0.0
    if float(np.std(a_m)) == 0.0 or float(np.std(b_m)) == 0.0:
        return 0.0
    value = float(np.corrcoef(a_m, b_m)[0, 1])
    return 0.0 if np.isnan(value) else round(value, 4)


def _convergence(news_avg: float, x_avg: float) -> str:
    """Rule-based verdict on the sign of both window means."""
    if news_avg > 0.0 and x_avg > 0.0:
        return "aligned bullish"
    if news_avg < 0.0 and x_avg < 0.0:
        return "aligned bearish"
    if news_avg != 0.0 and x_avg != 0.0:
        return "divergence"
    return "neutral"


class CrossXEngine:
    """News-lifecycle x X-sentiment cross-view over an index + X store."""

    def __init__(self, index: Any, x_store_path: Path | None = None) -> None:
        self._index = index
        self._topic = TopicEngine(index)
        self._sentiment = SentimentEngine(index)
        self._x_store_path = x_store_path

    # ── X side ───────────────────────────────────────────────────────────

    async def x_session_sentiment(self, session_id: str) -> dict[str, float] | None:
        """Per-day ``{YYYY-MM-DD: avg_score}`` for a session; ``None`` unknown.

        Returns ``None`` when no store path is configured or the session is
        unknown; an existing session with no tweets yields an empty dict.
        The store connection is opened and closed per call.
        """
        if self._x_store_path is None:
            return None
        store = SessionStore(self._x_store_path)
        try:
            await store.open()
            await store.init()
        except Exception as exc:
            logger.warning("crossx_store_open_failed", err=str(exc))
            return None
        try:
            try:
                days = await session_timeline(store, session_id)
            except KeyError:
                return None
            return {
                date: round(_mean(day["scores"]), 4)
                for date, day in sorted(days.items())
            }
        finally:
            await store.close()

    # ── combined view ────────────────────────────────────────────────────

    async def combined_view(
        self,
        term: str,
        session_id: str,
        window_days: int = 14,
    ) -> CombinedView:
        """News lifecycle + news sentiment aligned with X session sentiment.

        The news side comes from :meth:`TopicEngine.lifecycle` and
        :meth:`SentimentEngine.term_sentiment_over_time` over the same
        window; the X side from :func:`session_timeline`. Both daily series
        are zero-filled onto the news window's calendar days. An unknown
        session yields ``x_sentiment=None``, zero correlation and a
        ``neutral`` convergence with a news-only note.
        """
        cleaned_term = _validate_term(term)
        cleaned_session = _validate_session_id(session_id)
        lifecycle = self._topic.lifecycle(cleaned_term, window_days=window_days)
        buckets = self._sentiment.term_sentiment_over_time(
            cleaned_term, window_days=window_days, granularity="day"
        )
        dates = [bucket.ts for bucket in buckets]
        news_daily = [float(bucket.avg_score) for bucket in buckets]

        x_sentiment: list[SentimentPoint] | None = None
        x_daily: list[float] = []
        x_avg = 0.0
        correlation = 0.0
        note = ""
        x_by_date = await self.x_session_sentiment(cleaned_session)
        if x_by_date is not None:
            window_dates = {date.strftime("%Y-%m-%d") for date in dates}
            in_window = any(d in window_dates for d in x_by_date)
            if x_by_date and not in_window:
                # W22-F3: tweets exist but all predate the window — surface
                # it instead of an all-zero "X is silent" series.
                note = "x session tweets predate the window — news side only"
            else:
                x_daily = [x_by_date.get(date.strftime("%Y-%m-%d"), 0.0) for date in dates]
                x_sentiment = [
                    SentimentPoint(ts=date, avg_score=score)
                    for date, score in zip(dates, x_daily, strict=True)
                ]
                x_avg = round(_mean(x_daily), 4)
                correlation = _correlation_r(news_daily, x_daily)
        else:
            note = "x session unknown — news side only"

        news_avg = round(_mean(news_daily), 4)
        # W22-F2: convergence requires data on BOTH sides — a one-sided
        # average with the other all-zero would mislead ("aligned bearish"
        # from silence).
        if x_sentiment is None or not any(v != 0.0 for v in x_daily):
            convergence = "neutral"
        else:
            convergence = _convergence(news_avg, x_avg)
        return CombinedView(
            term=cleaned_term,
            news_phase=lifecycle.phase,
            news_series=lifecycle.counts,
            news_sentiment=[
                SentimentPoint(ts=bucket.ts, avg_score=bucket.avg_score)
                for bucket in buckets
            ],
            x_sentiment=x_sentiment,
            news_avg_score=news_avg,
            x_avg_score=x_avg,
            correlation_r=correlation,
            convergence=convergence,
            note=note,
        )
