"""FastAPI router exposing saved briefing files under ``/briefings``.

The CLI persists briefing JSON under ``{data_dir}/briefings/`` (see
:func:`~awareness.cli.main._save_briefing`); these read-only endpoints make
that history available to the dashboard. The directory is resolved lazily
per request (via *briefings_dir_getter*), so files the CLI writes at any
time are picked up. No index is needed — the endpoints are purely
filesystem-backed.

Trust model: ``{data_dir}`` must be operator-controlled and non-writable by
untrusted users — the endpoints follow symlinks inside the briefings dir
(an attacker with write access there can already read the machine directly,
so this adds no capability; a ``resolve()`` containment check could be added
as defense-in-depth if the deployment model ever changes).

Error contract (all endpoints):

* ``400`` — malformed date (must match ``YYYY-MM-DD`` or
  ``YYYY-MM-DD-<name>``, the same rule the CLI applies to ``--save [NAME]``).
* ``404`` — no saved file for that date.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from awareness.briefings.models import SavedBriefing

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(-[A-Za-z0-9_-]+)?$")
_MAX_LIST = 100


class BriefingDetail(BaseModel):
    """A single saved briefing: metadata plus the full parsed JSON content."""

    briefing: SavedBriefing
    content: dict[str, Any]


def _briefing_from_path(path: Path) -> SavedBriefing:
    """Metadata for one saved briefing file (nulls on corrupt/legacy JSON).

    ``date`` comes from the filename stem (``YYYY-MM-DD[-name]``); payload
    fields are best-effort: unparseable files still yield a ``SavedBriefing``
    carrying the path and size so they stay visible in the list.
    """
    stem = path.stem
    date = stem[:10]
    name = stem[11:] if len(stem) > 10 else None
    generated_at: str | None = None
    movers_count: int | None = None
    top_terms: list[str] | None = None
    size_bytes: int | None = None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        size_bytes = path.stat().st_size
    except (OSError, ValueError):
        # W18-F2: a file that vanishes between glob() and stat() must not
        # 500 — the entry still lists (size null) instead of crashing.
        data = None
    if isinstance(data, dict):
        if isinstance(data.get("generated_at"), str):
            generated_at = data["generated_at"]
        movers = data.get("movers")
        if isinstance(movers, list):
            movers_count = len(movers)
        terms = data.get("top_terms")
        if isinstance(terms, list):
            parsed: list[str] = []
            for item in terms:
                if isinstance(item, str) and item:
                    parsed.append(item)
                elif isinstance(item, dict) and isinstance(item.get("term"), str) and item["term"]:
                    parsed.append(item["term"])
            top_terms = parsed
    return SavedBriefing(
        date=date,
        name=name,
        path=str(path),
        size_bytes=size_bytes,
        generated_at=generated_at,
        movers_count=movers_count,
        top_terms=top_terms,
    )


def create_briefings_router(briefings_dir_getter: Callable[[], Path]) -> APIRouter:
    """Build the ``/briefings`` APIRouter bound to *briefings_dir_getter*.

    The getter is a zero-arg callable returning the briefings directory
    (e.g. ``settings.data_dir / "briefings"``); it is invoked per request so
    the list always reflects files the CLI wrote after app startup.
    """
    router = APIRouter(prefix="/briefings", tags=["briefings"])

    def _resolve_dir() -> Path:
        return briefings_dir_getter()

    @router.get("", response_model=list[SavedBriefing])
    def list_briefings() -> list[SavedBriefing]:
        """Saved briefing files, newest first by filename (clamped to 100)."""
        briefings_dir = _resolve_dir()
        if not briefings_dir.exists():
            return []
        paths = sorted(briefings_dir.glob("*.json"), reverse=True)[:_MAX_LIST]
        # W18-L6: skip stray non-conforming *.json files (e.g. "notes.json")
        # so the list never advertises a chip whose click would 400.
        return [
            _briefing_from_path(p)
            for p in paths
            if _DATE_RE.fullmatch(p.stem)
        ]

    @router.get("/{date}", response_model=BriefingDetail)
    def get_briefing(date: str) -> BriefingDetail:
        """One saved briefing: metadata plus the full parsed JSON content."""
        if not _DATE_RE.fullmatch(date):
            raise HTTPException(
                status_code=400,
                detail="date must match YYYY-MM-DD or YYYY-MM-DD-<name> "
                "(name: letters, digits, _ and -)",
            )
        path = _resolve_dir() / f"{date}.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"no briefing saved for {date}")
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            content = {}
        if not isinstance(content, dict):
            content = {}
        return BriefingDetail(
            briefing=_briefing_from_path(path),
            content=content,
        )

    return router
