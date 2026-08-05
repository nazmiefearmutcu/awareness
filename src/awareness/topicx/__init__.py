"""Topic-lifecycle + source-impact subsystem.

Exposes the :class:`~awareness.topicx.engine.TopicEngine` over a
:class:`~awareness.storage.duckdb_index.DuckDbIndex` and the FastAPI router
factory :func:`~awareness.topicx.router.create_topicx_router`.
"""

from awareness.topicx.engine import TopicEngine
from awareness.topicx.router import create_topicx_router

__all__ = ["TopicEngine", "create_topicx_router"]
