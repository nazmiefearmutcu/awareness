"""FastAPI router exposing the origin engine under ``/origin``.

The router is a factory: the integrator binds it to the process-wide index::

    app.include_router(create_origin_router(_get_index))

*index* may be a :class:`~awareness.storage.duckdb_index.DuckDbIndex`
instance or a zero-arg callable returning one (resolved lazily per request,
mirroring the analytics router).

Error contract (all endpoints):

* ``400`` — bad input. Query parameters are validated through the pydantic
  request models; engine-raised ``ValueError``s (e.g. empty term after
  stripping) map to 400 as well.
* ``503`` — origin index not ready (mirrors the ``/healthz``
  ``index_ready`` contract) or the DuckDB query failed.
"""

from __future__ import annotations

import inspect
from typing import Any, TypeVar

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError, field_validator

from awareness.obs.logging import get_logger
from awareness.origin.engine import OriginEngine
from awareness.origin.models import PublisherFirst, StoryOrigin

logger = get_logger("origin.router")

_RequestT = TypeVar("_RequestT", bound=BaseModel)

_MAX_LIMIT = 500


class _OriginRequest(BaseModel):
    """Shared query parameters for the /origin endpoints."""

    term: str = Field(min_length=1, max_length=200)
    window_days: int = Field(30, ge=1, le=365)
    limit: int = Field(20)

    @field_validator("limit")
    @classmethod
    def _clamp_limit(cls, value: int) -> int:
        return min(max(int(value), 1), _MAX_LIMIT)


class OriginStoriesRequest(_OriginRequest):
    """GET /origin/stories query parameters."""


class OriginPublishersRequest(_OriginRequest):
    """GET /origin/publishers query parameters."""


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
            logger.warning("origin_index_probe_failed", err=str(exc))
            return False
    return bool(getattr(index, "index_ready", False))


def _parse_or_400(model_cls: type[_RequestT], **values: Any) -> _RequestT:
    """Validate *values* against *model_cls*; map ValidationError to HTTP 400.

    ``None`` values are dropped so model defaults apply — callers forward raw
    optional query params without pre-filling defaults.
    """
    kwargs = {key: value for key, value in values.items() if value is not None}
    try:
        return model_cls(**kwargs)
    except ValidationError as exc:
        details = "; ".join(
            ".".join(str(part) for part in err["loc"]) + ": " + err["msg"]
            for err in exc.errors()
        )
        raise HTTPException(status_code=400, detail=f"bad request: {details}") from exc


def create_origin_router(index: Any) -> APIRouter:
    """Build the ``/origin`` APIRouter bound to *index*."""
    def _resolve() -> Any:
        return index() if inspect.isfunction(index) else index

    router = APIRouter(prefix="/origin", tags=["origin"])

    def _query(method_name: str, **kwargs: Any) -> Any:
        """Shared guard: 503 when the index is not ready; 400/503 on errors."""
        idx = _resolve()
        if not _index_ready(idx):
            raise HTTPException(status_code=503, detail="origin index not ready")
        engine = OriginEngine(idx)
        try:
            return getattr(engine, method_name)(**kwargs)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.warning("origin_query_failed", err=str(exc))
            raise HTTPException(
                status_code=503, detail=f"origin query failed: {exc}"
            ) from exc

    @router.get("/stories", response_model=list[StoryOrigin])
    def stories(
        term: str = "",
        window_days: int | None = None,
        limit: int | None = None,
    ) -> list[StoryOrigin]:
        """Origins of breaking-news clusters containing *term*.

        ``term`` — word-boundary, case-insensitive; ``window_days`` — window
        length in days (1..365); ``limit`` — max clusters (clamped 1..500).
        """
        req = _parse_or_400(
            OriginStoriesRequest,
            term=term,
            window_days=window_days,
            limit=limit,
        )
        return _query(
            "story_origins",
            term=req.term,
            window_days=req.window_days,
            limit=req.limit,
        )

    @router.get("/publishers", response_model=list[PublisherFirst])
    def publishers(
        term: str = "",
        window_days: int | None = None,
        limit: int | None = None,
    ) -> list[PublisherFirst]:
        """Publishers ranked by how often they broke tracked stories first."""
        req = _parse_or_400(
            OriginPublishersRequest,
            term=term,
            window_days=window_days,
            limit=limit,
        )
        return _query(
            "publisher_firsts",
            term=req.term,
            window_days=req.window_days,
            limit=req.limit,
        )

    return router
