"""Analytics subsystem: term frequency, spikes, breakdowns, co-occurrence.

Exposes the :class:`~awareness.analytics.engine.TermFrequencyEngine` over a
:class:`~awareness.storage.duckdb_index.DuckDbIndex` and the FastAPI router
factory :func:`~awareness.analytics.router.create_analytics_router`.
"""

from awareness.analytics.engine import TermFrequencyEngine
from awareness.analytics.router import create_analytics_router

__all__ = ["TermFrequencyEngine", "create_analytics_router"]
