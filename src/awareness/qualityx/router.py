"""FastAPI router exposing qualityx time-series endpoints under ``/qualityx``.

The router is a factory: the integrator binds it to the process-wide index::

    app.include_router(create_qualityx_router(_get_index))

Error contract:

* ``GET /qualityx/history`` — ``400`` for out-of-range ``days`` (1..365) or
  an unknown ``granularity`` (day | week | month), ``503`` when the corpus
  index is not ready (mirrors the ``/healthz`` ``index_ready`` contract) or a
  DuckDB query failed. An empty corpus yields zeroed points, never an error.
* ``GET /qualityx/current`` — ``200`` with the point-in-time quality
  snapshot (delegated to ``CorpusXEngine.quality_snapshot``); ``503`` when
  the index is not ready. An empty corpus yields the zeroed snapshot.
"""

from __future__ import annotations

import inspect
from typing import Any

from fastapi import APIRouter, HTTPException

from awareness.obs.logging import get_logger
from awareness.qualityx.engine import QualityTimeEngine, _GRANULARITIES
from awareness.qualityx.models import QualityHistory

logger = get_logger("qualityx.router")

_MIN_DAYS = 1
_MAX_DAYS = 365


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
            logger.warning("qualityx_index_probe_failed", err=str(exc))
            return False
    return bool(getattr(index, "index_ready", False))


def _validate_days(days: int) -> int:
    """Validate a day window; raise HTTP 400 outside 1..365."""
    try:
        n = int(days)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="bad request: days must be an integer") from exc
    if not _MIN_DAYS <= n <= _MAX_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"bad request: days must be between {_MIN_DAYS} and {_MAX_DAYS}",
        )
    return n


def _validate_granularity(granularity: str) -> str:
    """Validate a granularity; raise HTTP 400 when not day/week/month."""
    value = (granularity or "").strip().lower()
    if value not in _GRANULARITIES:
        raise HTTPException(
            status_code=400,
            detail=f"bad request: granularity must be one of {_GRANULARITIES}",
        )
    return value


def create_qualityx_router(index_getter: Any) -> APIRouter:
    """Build the ``/qualityx`` APIRouter bound to *index_getter*.

    *index_getter* is a :class:`~awareness.storage.duckdb_index.DuckDbIndex`
    (or a duck-typed shim exposing ``execute`` + ``health_snapshot``), or a
    zero-arg callable returning one (resolved lazily per request so app
    construction never touches the index).
    """

    def _resolve() -> Any:
        return index_getter() if inspect.isfunction(index_getter) else index_getter

    def _engine() -> QualityTimeEngine:
        """Ready-checked engine; raises HTTP 503 when the index is not ready."""
        idx = _resolve()
        if not _index_ready(idx):
            raise HTTPException(status_code=503, detail="corpus index not ready")
        return QualityTimeEngine(idx)

    router = APIRouter(prefix="/qualityx", tags=["qualityx"])

    @router.get("/history", response_model=QualityHistory)
    def history(days: int = 30, granularity: str = "day") -> QualityHistory:
        """Per-bucket corpus-quality history over the trailing *days* (1..365).

        ``days`` — trailing window in days; ``granularity`` — day | week |
        month bucket size. An empty corpus yields zeroed points.
        """
        window = _validate_days(days)
        bucket_size = _validate_granularity(granularity)
        engine = _engine()
        try:
            points = engine.history(days=window, granularity=bucket_size)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.warning("qualityx_history_failed", err=str(exc))
            raise HTTPException(
                status_code=503, detail=f"corpus query failed: {exc}"
            ) from exc
        return QualityHistory(days=window, points=points)

    @router.get("/current")
    def current() -> dict[str, Any]:
        """Point-in-time corpus-quality snapshot (whole corpus)."""
        engine = _engine()
        try:
            return engine.current()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.warning("qualityx_current_failed", err=str(exc))
            raise HTTPException(
                status_code=503, detail=f"corpus query failed: {exc}"
            ) from exc

    return router
