"""FastAPI router exposing corpus-quality analytics under ``/corpus``.

The router is a factory: the integrator binds it to the process-wide index::

    app.include_router(create_corpusx_router(_get_index))

Error contract:

* ``400`` — bad input: empty/too-many ``terms`` (at most 20) or engine
  ``ValueError``s (e.g. an out-of-range ``window_days``).
* ``503`` — corpus index not ready (mirrors the ``/healthz``
  ``index_ready`` contract) or the DuckDB query failed.
* ``/corpus/quality`` always returns 200 once the index is ready — an empty
  corpus yields a zeroed snapshot, never an error.
"""

from __future__ import annotations

import inspect
from typing import Any

from fastapi import APIRouter, HTTPException

from awareness.corpusx.engine import CorpusXEngine
from awareness.corpusx.models import QualitySnapshot, TopicMatrix
from awareness.obs.logging import get_logger

logger = get_logger("corpusx.router")

_MAX_TERMS = 20


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
            logger.warning("corpusx_index_probe_failed", err=str(exc))
            return False
    return bool(getattr(index, "index_ready", False))


def _parse_terms(raw: str | None) -> list[str]:
    """Split the comma-joined ``terms`` query param; raise HTTP 400 when bad."""
    if raw is None:
        raise HTTPException(status_code=400, detail="bad request: terms is required")
    terms = [part.strip() for part in raw.split(",") if part.strip()]
    if not terms:
        raise HTTPException(status_code=400, detail="bad request: terms must not be empty")
    if len(terms) > _MAX_TERMS:
        raise HTTPException(
            status_code=400,
            detail=f"bad request: at most {_MAX_TERMS} terms are supported",
        )
    return terms


def create_corpusx_router(index_getter: Any) -> APIRouter:
    """Build the ``/corpus`` APIRouter bound to *index_getter*.

    *index_getter* is a :class:`~awareness.storage.duckdb_index.DuckDbIndex`
    (or a duck-typed shim exposing ``execute`` + ``health_snapshot``), or a
    zero-arg callable returning one (resolved lazily per request so app
    construction never touches the index).
    """

    def _resolve() -> Any:
        return index_getter() if inspect.isfunction(index_getter) else index_getter

    def _engine() -> CorpusXEngine:
        """Ready-checked engine; raises HTTP 503 when the index is not ready."""
        idx = _resolve()
        if not _index_ready(idx):
            raise HTTPException(status_code=503, detail="corpus index not ready")
        return CorpusXEngine(idx)

    router = APIRouter(prefix="/corpus", tags=["corpus"])

    @router.get("/topic-matrix", response_model=TopicMatrix)
    def topic_matrix(
        terms: str = "",
        window_days: int | None = None,
        top_domains: int | None = None,
    ) -> TopicMatrix:
        """Term x domain matrix over the capture window.

        ``terms`` — comma-separated (at most 20); ``window_days`` — window
        length in days (1..365), defaulting to the corpus tail;
        ``top_domains`` — matrix columns, ranked by in-window volume.
        """
        term_list = _parse_terms(terms)
        engine = _engine()
        try:
            return engine.topic_matrix(
                term_list,
                window_days=window_days if window_days is not None else 30,
                top_domains=top_domains if top_domains is not None else 20,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.warning("corpusx_matrix_failed", err=str(exc))
            raise HTTPException(
                status_code=503, detail=f"corpus query failed: {exc}"
            ) from exc

    @router.get("/quality", response_model=QualitySnapshot)
    def quality(window_days: int | None = None) -> QualitySnapshot:
        """Corpus health snapshot; ``window_days`` restricts the capture window."""
        engine = _engine()
        try:
            return engine.quality_snapshot(window_days=window_days)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.warning("corpusx_quality_failed", err=str(exc))
            raise HTTPException(
                status_code=503, detail=f"corpus query failed: {exc}"
            ) from exc

    return router
