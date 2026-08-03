"""Corpus-quality subsystem: term x domain matrix + corpus health metrics.

Exposes the :class:`~awareness.corpusx.engine.CorpusXEngine` over a
:class:`~awareness.storage.duckdb_index.DuckDbIndex` and the FastAPI router
factory :func:`~awareness.corpusx.router.create_corpusx_router`.
"""

from awareness.corpusx.engine import CorpusXEngine
from awareness.corpusx.router import create_corpusx_router

__all__ = ["CorpusXEngine", "create_corpusx_router"]
