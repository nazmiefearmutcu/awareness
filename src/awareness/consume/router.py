"""HTTP surface for consumption features (LLM export + weekly digest).

Endpoints:
    POST /consume/export         — write an LLM-ready dataset into
                                   ``{data_dir}/exports/`` (threadpool-heavy)
    GET  /consume/digest         — JSON digest for the last N days
    GET  /consume/digest/markdown — markdown render of the digest

All heavy work (export streaming, digest aggregation) runs in the event loop
threadpool via ``asyncio.to_thread`` so requests never block the loop.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from awareness.config import get_settings
from awareness.consume.digest import (
    MAX_DIGEST_DAYS,
    Digest,
    generate_digest,
    render_digest_markdown,
)
from awareness.consume.llm_export import (
    EXPORT_FORMATS,
    HARD_MAX_LIMIT,
    export_llm_dataset,
)
from awareness.storage.duckdb_index import DuckDbIndex
from awareness.util.timeutil import to_utc

router = APIRouter(prefix="/consume", tags=["consume"])


class ExportRequest(BaseModel):
    """Body for POST /consume/export (bounds enforced manually for 400s)."""

    format: str = "jsonl"
    limit: int = Field(default=1000)
    start: datetime | None = None
    end: datetime | None = None
    domains: list[str] = Field(default_factory=list)
    dedup: bool = True


def _get_index() -> DuckDbIndex | None:
    """Process-wide index from the API server, or None when unavailable.

    Lazy import keeps this router importable without booting the full API
    app (and lets tests monkeypatch this function directly).
    """
    try:
        from awareness.api.server import _get_index as _server_get_index  # noqa: PLC0415

        return _server_get_index()
    except Exception:
        return None


def _require_index() -> DuckDbIndex:
    """Return a ready index or raise 503 (service unavailable)."""
    index = _get_index()
    if index is None:
        raise HTTPException(503, "search index not ready")
    try:
        snapshot = index.health_snapshot()
    except Exception as exc:
        raise HTTPException(503, f"search index not ready: {exc}") from exc
    if not bool(snapshot.get("ready")):
        raise HTTPException(503, f"search index not ready: {snapshot.get('error')}")
    return index


def _export_dir() -> Path:
    settings = get_settings()
    assert settings.data_dir is not None
    out_dir = settings.data_dir / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


@router.post("/export")
async def export_dataset(body: ExportRequest) -> dict[str, Any]:
    """Write an LLM-ready dataset export into ``{data_dir}/exports/``.

    Bounded: ``limit`` is clamped to ``[1, {HARD_MAX_LIMIT}]`` and the write
    runs in the threadpool. Returns the written file path + row count.
    """
    fmt = str(body.format or "").strip().lower()
    if fmt not in EXPORT_FORMATS:
        raise HTTPException(
            400,
            f"unsupported export format {body.format!r}; expected one of {EXPORT_FORMATS}",
        )
    limit = body.limit
    if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= HARD_MAX_LIMIT):
        raise HTTPException(400, f"limit must be an integer in [1, {HARD_MAX_LIMIT}]")
    start = to_utc(body.start)
    end = to_utc(body.end)
    if start is not None and end is not None and end < start:
        raise HTTPException(400, "end must be greater than or equal to start")

    index = _require_index()
    result = await asyncio.to_thread(
        export_llm_dataset,
        index,
        _export_dir(),
        format=fmt,
        limit=limit,
        start=start,
        end=end,
        domains=body.domains or None,
        dedupe=body.dedup,
    )
    return result.model_dump(mode="json")


@router.get("/digest")
async def digest_json(days: int = Query(default=7)) -> Digest:
    """JSON digest for the last *days* of captures (bounded to 1..365)."""
    if not (1 <= days <= MAX_DIGEST_DAYS):
        raise HTTPException(400, f"days must be in [1, {MAX_DIGEST_DAYS}]")
    index = _require_index()
    return await asyncio.to_thread(generate_digest, index, days=days)


@router.get("/digest/markdown")
async def digest_markdown(days: int = Query(default=7)) -> PlainTextResponse:
    """Markdown render of the digest (text/markdown)."""
    if not (1 <= days <= MAX_DIGEST_DAYS):
        raise HTTPException(400, f"days must be in [1, {MAX_DIGEST_DAYS}]")
    index = _require_index()
    digest = await asyncio.to_thread(generate_digest, index, days=days)
    return PlainTextResponse(render_digest_markdown(digest), media_type="text/markdown")


def wire(app: Any) -> None:
    """Include this router plus the X scraper router into a FastAPI app.

    Convenience for the API integrator::

        from awareness.consume.router import wire
        wire(app)

    Also registers an app-shutdown hook that closes the X scraper's SQLite
    store (aiosqlite keeps a background thread per open connection).
    """
    from awareness.consume import xrouter  # noqa: PLC0415

    app.include_router(router)
    app.include_router(xrouter.router)

    async def _close_x_store_on_shutdown() -> None:
        await xrouter.close_store()

    # Starlette router-level shutdown hook (runs when lifespan is active).
    app.router.add_event_handler("shutdown", _close_x_store_on_shutdown)
