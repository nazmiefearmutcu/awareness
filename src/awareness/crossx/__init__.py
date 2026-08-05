"""Cross-view of topic lifecycle + X sentiment (crossx).

Combines the news-side lifecycle (:class:`~awareness.topicx.engine.TopicEngine`)
and per-day news sentiment (:class:`~awareness.sentiment.engine.SentimentEngine`)
with the X-side per-day sentiment of a scraper session
(:func:`~awareness.xscraper.analyze.session_timeline`), aligned by calendar day
so the two series can be correlated and judged for convergence.

Exposes :class:`~awareness.crossx.engine.CrossXEngine` and the FastAPI router
factory :func:`~awareness.crossx.router.create_crossx_router`.
"""

from awareness.crossx.engine import CrossXEngine
from awareness.crossx.router import create_crossx_router

__all__ = ["CrossXEngine", "create_crossx_router"]
