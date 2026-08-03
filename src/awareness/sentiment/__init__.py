"""Sentiment subsystem: lexicon scoring + time-series sentiment analytics.

Exposes the :class:`~awareness.sentiment.engine.SentimentEngine` over a
:class:`~awareness.storage.duckdb_index.DuckDbIndex` and the FastAPI router
factory :func:`~awareness.sentiment.router.create_sentiment_router`.
"""

from awareness.sentiment.engine import SentimentEngine
from awareness.sentiment.router import create_sentiment_router

__all__ = ["SentimentEngine", "create_sentiment_router"]
