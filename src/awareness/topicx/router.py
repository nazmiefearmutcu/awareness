"""FastAPI router exposing topic lifecycle / source-impact under ``/topicx``.

The router is a factory: the integrator binds it to the process-wide index::

    app.include_router(create_topicx_router(_get_index))

Error contract (all endpoints):

* ``400`` — bad input. Query parameters are validated through the pydantic
  request models in :mod:`awareness.topicx.models` (empty/too-long terms,
  out-of-range windows) and engine-raised ``ValueError``s map to 400 as
  well.
* ``503`` — topicx index not ready (mirrors the ``/healthz``
  ``index_ready`` contract) or the DuckDB query failed.
"""

from __future__ import annotations

import inspect
from typing import Any, TypeVar

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError

from awareness.obs.logging import get_logger
from awareness.topicx.engine import TopicEngine
from awareness.topicx.models import (
    DominanceRequest,
    EmergingRequest,
    EmergingTopic,
    ImpactRequest,
    LifecycleRequest,
    SourceImpact,
    TopicDominance,
    TopicLifecycle,
)

logger = get_logger("topicx.router")

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
            logger.warning("topicx_index_probe_failed", err=str(exc))
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


def create_topicx_router(index_getter: Any) -> APIRouter:
    """Build the ``/topicx`` APIRouter bound to *index_getter*.

    *index_getter* is a :class:`~awareness.storage.duckdb_index.DuckDbIndex`
    (or a duck-typed shim exposing ``execute`` + ``health_snapshot``), or a
    zero-arg callable returning one (resolved lazily per request so app
    construction never touches the index).
    """

    def _resolve() -> Any:
        return index_getter() if inspect.isfunction(index_getter) else index_getter

    router = APIRouter(prefix="/topicx", tags=["topicx"])

    def _query(method_name: str, **kwargs: Any) -> Any:
        """Shared guard: 503 when the index is not ready; 400/503 on errors."""
        idx = _resolve()
        if not _index_ready(idx):
            raise HTTPException(status_code=503, detail="topicx index not ready")
        engine = TopicEngine(idx)
        try:
            return getattr(engine, method_name)(**kwargs)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.warning("topicx_query_failed", err=str(exc))
            raise HTTPException(
                status_code=503, detail=f"topicx query failed: {exc}"
            ) from exc

    @router.get("/lifecycle", response_model=TopicLifecycle)
    def lifecycle(
        term: str = "",
        window_days: int | None = None,
    ) -> TopicLifecycle:
        """Lifecycle phase + daily series for *term* over the window.

        ``term`` — word-boundary, case-insensitive; ``window_days`` — window
        length in days (1..365). Phases: EMERGING / EXPANDING / PEAKING /
        DECLINING / DORMANT / STABLE.
        """
        req = _parse_or_400(
            LifecycleRequest,
            term=term,
            window_days=window_days,
        )
        return _query(
            "lifecycle",
            term=req.term,
            window_days=req.window_days,
        )

    @router.get("/emerging", response_model=list[EmergingTopic])
    def emerging(
        window_days: int | None = None,
        limit: int | None = None,
    ) -> list[EmergingTopic]:
        """Corpus-wide terms first seen within 3 days, ranked by volume.

        ``window_days`` — scan window (1..365); ``limit`` — max terms
        (clamped to 1..500).
        """
        req = _parse_or_400(EmergingRequest, window_days=window_days, limit=limit)
        return _query(
            "top_emerging",
            window_days=req.window_days,
            limit=req.limit,
        )

    @router.get("/impact", response_model=list[SourceImpact])
    def impact(
        window_days: int | None = None,
        limit: int | None = None,
    ) -> list[SourceImpact]:
        """Origin-domain impact over the replication map.

        ``window_days`` — replication window (1..365); ``limit`` — max
        domains (clamped to 1..500).
        """
        req = _parse_or_400(ImpactRequest, window_days=window_days, limit=limit)
        return _query(
            "source_impact",
            window_days=req.window_days,
            limit=req.limit,
        )

    @router.get("/dominance", response_model=list[TopicDominance])
    def dominance(
        term: str = "",
        window_days: int | None = None,
    ) -> list[TopicDominance]:
        """Per-domain share of the docs mentioning *term*.

        ``term`` — word-boundary, case-insensitive; ``window_days`` — window
        length in days (1..365). ``doc_fraction`` sums to 1.0 across the
        result set; ``avg_sentiment`` is the mean lexicon score of the
        docs' first 200 characters.
        """
        req = _parse_or_400(DominanceRequest, term=term, window_days=window_days)
        return _query(
            "topic_dominance",
            term=req.term,
            window_days=req.window_days,
        )

    return router
