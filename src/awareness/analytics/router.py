"""FastAPI router exposing the analytics engine under ``/analytics``.

The router is a factory: the integrator binds it to the process-wide index::

    app.include_router(create_analytics_router(_get_index()))

Error contract (all endpoints):

* ``400`` — bad input. Query parameters are validated through the pydantic
  request models in :mod:`awareness.analytics.models` and engine-raised
  ``ValueError``s (e.g. ``start`` after ``end``) map to 400 as well.
* ``503`` — analytics index not ready (mirrors the ``/healthz``
  ``index_ready`` contract) or the DuckDB query failed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, TypeVar

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError

from awareness.analytics.engine import TermFrequencyEngine
from awareness.analytics.models import (
    CoOccurringRequest,
    DomainBreakdownRequest,
    DomainCount,
    LanguageBreakdownRequest,
    LanguageCount,
    Spike,
    SpikesRequest,
    TermCount,
    TermFrequencyRequest,
    TimeBucket,
    TopTermsRequest,
)
from awareness.obs.logging import get_logger

logger = get_logger("analytics.router")

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
            logger.warning("analytics_index_probe_failed", err=str(exc))
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


def create_analytics_router(index: Any) -> APIRouter:
    """Build the ``/analytics`` APIRouter bound to *index*.

    *index* is a :class:`~awareness.storage.duckdb_index.DuckDbIndex` (or a
    duck-typed shim exposing ``execute`` + ``health_snapshot``), or a
    zero-arg callable returning one (resolved lazily per request so app
    construction never touches the index).
    """
    def _resolve() -> Any:
        import inspect

        return index() if inspect.isfunction(index) else index

    router = APIRouter(prefix="/analytics", tags=["analytics"])

    def _query(method_name: str, **kwargs: Any) -> list[Any]:
        """Shared guard: 503 when the index is not ready; 400/503 on errors."""
        idx = _resolve()
        if not _index_ready(idx):
            raise HTTPException(status_code=503, detail="analytics index not ready")
        engine = TermFrequencyEngine(idx)
        try:
            return getattr(engine, method_name)(**kwargs)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.warning("analytics_query_failed", err=str(exc))
            raise HTTPException(
                status_code=503, detail=f"analytics query failed: {exc}"
            ) from exc

    @router.get("/term-frequency", response_model=list[TimeBucket])
    def term_frequency(
        term: str = "",
        window_days: int | None = None,
        granularity: str = "day",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[TimeBucket]:
        """Document counts per bucket for docs containing *term*.

        ``term`` — word-boundary, case-insensitive; ``window_days`` — window
        length in days (1..365); ``granularity`` — day | week | month;
        ``start``/``end`` — UTC ISO-8601 window bounds.
        """
        req = _parse_or_400(
            TermFrequencyRequest,
            term=term,
            window_days=window_days,
            granularity=granularity,
            start=start,
            end=end,
        )
        return _query(
            "term_frequency_over_time",
            term=req.term,
            window_days=req.window_days,
            granularity=req.granularity,
            start=req.start,
            end=req.end,
        )

    @router.get("/top-terms", response_model=list[TermCount])
    def top_terms(
        limit: int | None = None,
        min_count: int | None = None,
    ) -> list[TermCount]:
        """Most frequent corpus tokens, stopwords excluded.

        ``limit`` — max terms (clamped to 1..500); ``min_count`` — minimum
        token frequency to include.
        """
        req = _parse_or_400(TopTermsRequest, limit=limit, min_count=min_count)
        return _query(
            "top_terms",
            start=None,
            end=None,
            limit=req.limit,
            min_count=req.min_count,
        )

    @router.get("/spikes", response_model=list[Spike])
    def spikes(
        term: str = "",
        window_days: int | None = None,
        zscore_threshold: float | None = None,
        min_absolute: int | None = None,
    ) -> list[Spike]:
        """Anomalous days where *term* volume bursts above the window baseline.

        ``window_days`` — window length (1..365); ``zscore_threshold`` —
        minimum z-score to flag a day; ``min_absolute`` — minimum raw count.
        """
        req = _parse_or_400(
            SpikesRequest,
            term=term,
            window_days=window_days,
            zscore_threshold=zscore_threshold,
            min_absolute=min_absolute,
        )
        return _query(
            "detect_spikes",
            term=req.term,
            window_days=req.window_days,
            zscore_threshold=req.zscore_threshold,
            min_absolute=req.min_absolute,
        )

    @router.get("/domains", response_model=list[DomainCount])
    def domains(
        limit: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[DomainCount]:
        """Capture counts grouped by domain.

        ``limit`` — max domains (clamped to 1..500); ``start``/``end`` — UTC
        ISO-8601 window bounds.
        """
        req = _parse_or_400(
            DomainBreakdownRequest, limit=limit, start=start, end=end
        )
        return _query(
            "domain_breakdown",
            start=req.start,
            end=req.end,
            limit=req.limit,
        )

    @router.get("/languages", response_model=list[LanguageCount])
    def languages(
        limit: int | None = None,
    ) -> list[LanguageCount]:
        """Capture counts grouped by primary language tag (``None`` = undetected).

        ``limit`` — max languages (clamped to 1..500).
        """
        req = _parse_or_400(LanguageBreakdownRequest, limit=limit)
        return _query(
            "language_breakdown",
            start=None,
            end=None,
            limit=req.limit,
        )

    @router.get("/co-occurring", response_model=list[TermCount])
    def co_occurring(
        term: str = "",
        limit: int | None = None,
    ) -> list[TermCount]:
        """Top tokens co-occurring in docs that contain *term*.

        ``term`` — anchor term (word-boundary, case-insensitive); ``limit`` —
        max co-occurring terms (clamped to 1..500).
        """
        req = _parse_or_400(CoOccurringRequest, term=term, limit=limit)
        return _query(
            "entity_term_counts",
            term=req.term,
            start=None,
            end=None,
            limit=req.limit,
        )

    return router
