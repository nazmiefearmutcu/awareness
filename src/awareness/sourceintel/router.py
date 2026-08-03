"""HTTP surface for the source-intelligence subsystem.

Exposes :class:`SourceIntelEngine` queries under ``/source-intel``:

    GET /source-intel/domains?limit=&start=&end=   — composite domain ranking
    GET /source-intel/domain/{domain}              — single-domain profile
    GET /source-intel/replication?limit=&window_days= — who copies whom
    GET /source-intel/replicators?limit=           — worst copiers
    GET /source-intel/freshness?limit=             — per-domain recency

Error mapping follows the rest of the API: ``400`` for malformed parameters
(FastAPI query constraints, unnormalizable domains), ``404`` for unknown
domains, ``503`` when the index/engine is unavailable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from awareness.obs.logging import get_logger
from awareness.sourceintel.engine import SourceIntelEngine, UnknownDomainError
from awareness.sourceintel.models import (
    DomainFreshness,
    DomainProfile,
    DomainScore,
    ReplicationEdge,
)

logger = get_logger("api.sourceintel")

router = APIRouter(prefix="/source-intel", tags=["source-intel"])


def get_engine() -> SourceIntelEngine:
    """Build an engine over the process-wide :class:`DuckDbIndex`.

    The index is created once per process by the API server and returned via
    its module-level ``_get_index``; importing lazily keeps this router
    importable without pulling the whole server in. Failures here (DuckDB
    down, corrupted views) surface as 503 upstream.
    """
    try:
        from awareness.api.server import _get_index  # noqa: PLC0415

        return SourceIntelEngine(_get_index())
    except Exception as exc:
        logger.warning("sourceintel_index_unavailable", error=str(exc))
        raise HTTPException(503, "source-intel index unavailable") from exc


EngineDep = Annotated[SourceIntelEngine, Depends(get_engine)]


def _fail_503(endpoint: str, exc: Exception) -> None:
    logger.warning("sourceintel_query_failed", endpoint=endpoint, error=str(exc))


@router.get("/domains", response_model=list[DomainScore])
def list_domains(
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    start: Annotated[datetime | None, Query()] = None,
    end: Annotated[datetime | None, Query()] = None,
    engine: EngineDep = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """Composite quality ranking of domains (see engine module docstring)."""
    try:
        ranked = engine.domain_rank(start=start, end=end, limit=limit)
    except Exception as exc:
        _fail_503("domains", exc)
        raise HTTPException(503, "source-intel query failed") from exc
    return [r.model_dump(mode="json") for r in ranked]


@router.get("/domain/{domain}", response_model=DomainProfile)
def domain_detail(
    domain: str,
    engine: EngineDep = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Aggregate profile for a single domain (URL/case-insensitive)."""
    try:
        profile = engine.domain_profile(domain)
    except UnknownDomainError as exc:
        raise HTTPException(404, f"unknown domain: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(400, f"invalid domain: {exc}") from exc
    except Exception as exc:
        _fail_503("domain", exc)
        raise HTTPException(503, "source-intel query failed") from exc
    return profile.model_dump(mode="json")


@router.get("/replication", response_model=list[ReplicationEdge])
def replication(
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    window_days: Annotated[int | None, Query(ge=1, le=3650)] = None,
    engine: EngineDep = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """Directed "who copies whom" edges from the dedup structure."""
    try:
        edges = engine.replication_map(limit=limit, window_days=window_days)
    except Exception as exc:
        _fail_503("replication", exc)
        raise HTTPException(503, "source-intel query failed") from exc
    return [e.model_dump(mode="json") for e in edges]


@router.get("/replicators", response_model=list[DomainScore])
def replicators(
    limit: Annotated[int, Query(ge=1, le=500)] = 20,
    engine: EngineDep = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """Domains with the most outbound replication edges (they copy others)."""
    try:
        ranked = engine.top_replicators(limit=limit)
    except Exception as exc:
        _fail_503("replicators", exc)
        raise HTTPException(503, "source-intel query failed") from exc
    return [r.model_dump(mode="json") for r in ranked]


@router.get("/freshness", response_model=list[DomainFreshness])
def freshness(
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    engine: EngineDep = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """Per-domain recency: last seen, days since, 7d/30d capture counts."""
    try:
        report = engine.freshness_report(limit=limit)
    except Exception as exc:
        _fail_503("freshness", exc)
        raise HTTPException(503, "source-intel query failed") from exc
    return [f.model_dump(mode="json") for f in report]
