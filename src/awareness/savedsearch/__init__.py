"""Saved searches: user bookmarks that re-run a corpus query.

Exposes the :class:`~awareness.savedsearch.store.SavedSearchStore` (SQLite
persistence under ``<data_dir>/saved_searches.db``), the pydantic models in
:mod:`awareness.savedsearch.models`, and the FastAPI router factory
:func:`~awareness.savedsearch.router.create_savedsearch_router`.
"""

from awareness.savedsearch.models import SavedSearch, SavedSearchCreate
from awareness.savedsearch.router import create_savedsearch_router
from awareness.savedsearch.store import SavedSearchStore

__all__ = [
    "SavedSearch",
    "SavedSearchCreate",
    "SavedSearchStore",
    "create_savedsearch_router",
]
