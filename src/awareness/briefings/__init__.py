"""Saved-briefings subsystem: read-only API over the CLI's briefing history.

The CLI persists briefing JSON under ``{data_dir}/briefings/`` (see
:func:`~awareness.cli.main._save_briefing`); this package exposes that
history to the dashboard via the FastAPI router factory
:func:`~awareness.briefings.router.create_briefings_router`. Pure
filesystem-backed reads — no engine, no DuckDB index.
"""

from awareness.briefings.models import SavedBriefing
from awareness.briefings.router import create_briefings_router

__all__ = ["SavedBriefing", "create_briefings_router"]
