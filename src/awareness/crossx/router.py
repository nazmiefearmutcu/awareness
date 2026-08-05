"""FastAPI router exposing the topic-lifecycle x X-sentiment view under ``/crossx``.

The router is a factory: the integrator binds it to the process-wide index
and (optionally) an X store path getter::

    app.include_router(create_crossx_router(_get_index))

The default X store getter resolves ``{data_dir}/xscraper.sqlite`` lazily,
mirroring :mod:`awareness.consume.xrouter` (the same file the /x/sessions
surface writes). Pass an explicit ``x_store_getter`` to point elsewhere
(e.g. tests).

Error contract:

* ``400`` — bad input: empty/too-long ``term`` or ``session_id``, or an
  out-of-range ``window_days`` (1..365).
* ``503`` — crossx index not ready (mirrors the ``/healthz`` ``index_ready``
  contract) or the DuckDB query failed.
* Unknown session — ``200`` with ``x_sentiment=None`` and a news-only note:
  the X side is optional by design.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError

from awareness.config import get_settings
from awareness.crossx.engine import CrossXEngine
from awareness.crossx.models import CombinedView
from awareness.obs.logging import get_logger

logger = get_logger("crossx.router")


def _default_x_store_path() -> Path:
    """Resolve ``{data_dir}/xscraper.sqlite`` (the /x/sessions store)."""
    settings = get_settings()
    assert settings.data_dir is not None
    return settings.data_dir / "xscraper.sqlite"


def _index_ready(index: Any) -> bool:
    """Cheap readiness probe mirroring the ``/healthz`` ``index_ready`` contract.

    Prefers ``health_snapshot()`` (the DuckDbIndex surface); accepts a
    duck-typed ``index_ready`` attribute for stub/shim indexes.
    """
    probe = getattr(index, "health_snapshot", None)
    if callable(probe):
        try:
            snap = probe()
            return bool((snap or {}).get("ready"))
        except Exception as exc:
            logger.warning("crossx_index_probe_failed", err=str(exc))
            return False
    return bool(getattr(index, "index_ready", False))


class _CrossXViewRequest(BaseModel):
    """GET /crossx/view query parameters."""

    term: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    window_days: int = Field(14, ge=1, le=365)


def create_crossx_router(
    index_getter: Any,
    x_store_getter: Callable[[], Path] | None = None,
) -> APIRouter:
    """Build the ``/crossx`` APIRouter bound to *index_getter*.

    *index_getter* is a :class:`~awareness.storage.duckdb_index.DuckDbIndex`
    (or a duck-typed shim exposing ``execute`` + ``health_snapshot``), or a
    zero-arg callable returning one (resolved lazily per request). *x_store_getter*
    is a zero-arg callable returning the X store path; defaults to
    :func:`_default_x_store_path`.
    """
    store_getter = x_store_getter or _default_x_store_path

    def _resolve() -> Any:
        return index_getter() if inspect.isfunction(index_getter) else index_getter

    def _resolve_store_path() -> Path | None:
        value = store_getter() if inspect.isfunction(store_getter) else store_getter
        return Path(value) if value is not None else None

    router = APIRouter(prefix="/crossx", tags=["crossx"])

    @router.get("/view", response_model=CombinedView)
    async def view(
        term: str = "",
        session_id: str = "",
        window_days: int | None = None,
    ) -> CombinedView:
        """News lifecycle + sentiment aligned with an X session's sentiment.

        ``term`` — word-boundary, case-insensitive; ``session_id`` — an X
        scraper session id (unknown sessions yield a news-only view);
        ``window_days`` — window length in days (1..365, default 14).
        """
        kwargs = {
            key: value for key, value in {
                "term": term, "session_id": session_id, "window_days": window_days,
            }.items() if value is not None
        }
        try:
            req = _CrossXViewRequest(**kwargs)
        except ValidationError as exc:
            details = "; ".join(
                ".".join(str(part) for part in err["loc"]) + ": " + err["msg"]
                for err in exc.errors()
            )
            raise HTTPException(status_code=400, detail=f"bad request: {details}") from exc

        idx = _resolve()
        if not _index_ready(idx):
            raise HTTPException(status_code=503, detail="crossx index not ready")
        engine = CrossXEngine(idx, x_store_path=_resolve_store_path())
        try:
            return await engine.combined_view(
                req.term,
                req.session_id,
                window_days=req.window_days,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.warning("crossx_query_failed", err=str(exc))
            raise HTTPException(
                status_code=503, detail=f"crossx query failed: {exc}"
            ) from exc

    return router
