"""Pydantic models for the saved-briefings API.

``SavedBriefing`` mirrors what :func:`~awareness.cli.main._save_briefing`
persists under ``{data_dir}/briefings/YYYY-MM-DD[-name].json``. Nullable
fields stay ``None`` for corrupt or legacy files — the list endpoint still
reports those files (with a size), it just cannot derive their metadata.
"""

from __future__ import annotations

from pydantic import BaseModel


class SavedBriefing(BaseModel):
    """One saved briefing file (``YYYY-MM-DD[-name].json``)."""

    date: str
    name: str | None = None
    path: str
    size_bytes: int | None = None  # None when the file vanished mid-listing
    generated_at: str | None = None
    movers_count: int | None = None
    top_terms: list[str] | None = None
