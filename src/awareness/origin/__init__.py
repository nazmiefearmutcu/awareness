"""Breaking-news origin subsystem: first-publisher tracking.

Exposes the :class:`~awareness.origin.engine.OriginEngine` over a
:class:`~awareness.storage.duckdb_index.DuckDbIndex` and the FastAPI router
factory :func:`~awareness.origin.router.create_origin_router`.
"""

from awareness.origin.engine import OriginEngine
from awareness.origin.router import create_origin_router

__all__ = ["OriginEngine", "create_origin_router"]
