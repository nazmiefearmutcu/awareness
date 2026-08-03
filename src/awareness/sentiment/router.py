"""FastAPI router exposing the sentiment engine under ``/sentiment``.

The router is a factory: the integrator binds it to the process-wide index::

    app.include_router(create_sentiment_router(_get_index))

*index* may be a :class:`~awareness.storage.duckdb_index.DuckDbIndex`
instance or a zero-arg callable returning one (resolved lazily per request,
mirroring the analytics router).

Error contract (all endpoints):

* ``400`` — bad input. Query parameters are validated through the pydantic
  request models in :mod:`awareness.sentiment.models`; engine-raised
  ``ValueError``s (e.g. empty term after stripping) map to 400 as well.
* ``503`` — sentiment index not ready (mirrors the ``/healthz``
  ``index_ready`` contract) or the DuckDB query failed.
"""

from __future__ import annotations

import inspect
from typing import Any, TypeVar

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError

from awareness.obs.logging import get_logger
from awareness.sentiment.engine import SentimentEngine
from awareness.sentiment.models import (
    SentimentHeat,
    SentimentHeatRequest,
    SentimentResult,
    SentimentTermRequest,
)

logger = get_logger("sentiment.router")

_RequestT = TypeVar("_RequestT", bound=BaseModel)


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
            logger.warning("sentiment_index_probe_failed", err=str(exc))
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


def create_sentiment_router(index: Any) -> APIRouter:
    """Build the ``/sentiment`` APIRouter bound to *index*."""
    def _resolve() -> Any:
        return index() if inspect.isfunction(index) else index

    router = APIRouter(prefix="/sentiment", tags=["sentiment"])

    def _query(method_name: str, **kwargs: Any) -> Any:
        """Shared guard: 503 when the index is not ready; 400/503 on errors."""
        idx = _resolve()
        if not _index_ready(idx):
            raise HTTPException(status_code=503, detail="sentiment index not ready")
        engine = SentimentEngine(idx)
        try:
            return getattr(engine, method_name)(**kwargs)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.warning("sentiment_query_failed", err=str(exc))
            raise HTTPException(
                status_code=503, detail=f"sentiment query failed: {exc}"
            ) from exc

    @router.get("/term", response_model=SentimentResult)
    def term(
        term: str = "",
        window_days: int | None = None,
        granularity: str = "day",
    ) -> SentimentResult:
        """Sentiment time series (buckets + heat summary) for docs with *term*.

        ``term`` — word-boundary, case-insensitive; ``window_days`` — window
        length in days (1..365); ``granularity`` — day | week | month.
        """
        req = _parse_or_400(
            SentimentTermRequest,
            term=term,
            window_days=window_days,
            granularity=granularity,
        )
        buckets = _query(
            "term_sentiment_over_time",
            term=req.term,
            window_days=req.window_days,
            granularity=req.granularity,
        )
        heat = _query("market_heat", term=req.term, window_days=req.window_days)
        return SentimentResult(term=req.term, buckets=buckets, **heat)

    @router.get("/heat", response_model=SentimentHeat)
    def heat(
        term: str = "",
        window_days: int | None = None,
    ) -> SentimentHeat:
        """Aggregate sentiment summary for docs with *term* over the window."""
        req = _parse_or_400(SentimentHeatRequest, term=term, window_days=window_days)
        return SentimentHeat(
            **_query("market_heat", term=req.term, window_days=req.window_days)
        )

    return router
