"""FastAPI router for the entities subsystem."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from awareness.entities.engine import EntityEngine
from awareness.entities.models import (
    CoOccurrence,
    CorrelationResult,
    ExtractedEntity,
    TimeBucket,
)
from awareness.obs.logging import get_logger
from awareness.util.timeutil import to_utc

logger = get_logger("entities.router")


def _validate_term(term: str | None) -> str:
    if not term or not term.strip():
        raise HTTPException(status_code=400, detail="'entity' (or 'a'/'b') is required")
    term = term.strip()
    if len(term) > 200:
        raise HTTPException(status_code=400, detail="entity too long (max 200 chars)")
    return term


def _parse_start(value: str | None) -> datetime | None:
    if value is None or not value.strip():
        return None
    parsed = to_utc(value)
    if parsed is None:
        raise HTTPException(status_code=400, detail=f"invalid start date: {value!r}")
    return parsed


def create_entities_router(
    index_getter: Callable[[], Any],
) -> APIRouter:
    """Build the /entities router around an index accessor."""
    router = APIRouter(prefix="/entities", tags=["entities"])

    def _engine() -> EntityEngine:
        index = index_getter()
        engine = EntityEngine(index)
        if not engine._ready():
            raise HTTPException(status_code=503, detail="index not ready")
        return engine

    @router.get("/top", response_model=list[ExtractedEntity])
    def entities_top(
        limit: int = Query(100, ge=1, le=500),
        limit_docs: int = Query(500, ge=1, le=2000),
        start: str | None = None,
        end: str | None = None,
    ) -> list[ExtractedEntity]:
        start_dt = _parse_start(start)
        end_dt = _parse_start(end)
        try:
            return _engine().extract_from_corpus(
                limit_docs=limit_docs, start=start_dt, end=end_dt
            )[:limit]
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("entities_top_failed", error=str(exc))
            raise HTTPException(status_code=503, detail="query failed") from exc

    @router.get("/co-occurring", response_model=list[CoOccurrence])
    def entities_co_occurring(
        entity: str | None = Query(default=None),
        kind: str | None = Query(default=None),
        limit: int = Query(50, ge=1, le=200),
        window_days: int = Query(30, ge=1, le=365),
    ) -> list[CoOccurrence]:
        term = _validate_term(entity)
        if kind is not None and kind not in ("ORG", "PERSON", "PLACE", "TICKER"):
            raise HTTPException(status_code=400, detail="invalid kind")
        try:
            return _engine().co_occurrence(
                term, kind=kind, window_days=window_days, limit=limit
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("entities_cooccur_failed", error=str(exc))
            raise HTTPException(status_code=503, detail="query failed") from exc

    @router.get("/trend", response_model=list[TimeBucket])
    def entities_trend(
        entity: str | None = Query(default=None),
        window_days: int = Query(14, ge=1, le=365),
        granularity: str = Query("day", pattern="^(day|week|month)$"),
    ) -> list[TimeBucket]:
        term = _validate_term(entity)
        try:
            return _engine().entity_trend(
                term, window_days=window_days, granularity=granularity
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("entities_trend_failed", error=str(exc))
            raise HTTPException(status_code=503, detail="query failed") from exc

    @router.get("/correlation", response_model=CorrelationResult)
    def entities_correlation(
        a: str | None = Query(default=None),
        b: str | None = Query(default=None),
        window_days: int = Query(30, ge=1, le=365),
    ) -> CorrelationResult:
        term_a = _validate_term(a)
        term_b = _validate_term(b)
        try:
            return _engine().correlation(term_a, term_b, window_days=window_days)
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("entities_correlation_failed", error=str(exc))
            raise HTTPException(status_code=503, detail="query failed") from exc

    return router
