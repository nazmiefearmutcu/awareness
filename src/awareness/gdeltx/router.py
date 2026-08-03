"""FastAPI router exposing the GDELT analytics bridge under ``/gdelt``.

The router is a factory: the integrator binds it to the process-wide index::

    app.include_router(create_gdeltx_router(_get_index))

Error contract (all endpoints):

* ``400`` — bad input: empty/too-long/control-character terms, invalid
  ``window_days``, more than 20 terms on ``/gaps``. Query parameters are
  validated through the pydantic request models in
  :mod:`awareness.gdeltx.models`; engine-raised :class:`ValueError` map to
  400 as well.
* ``503`` — index not ready (mirrors the ``/healthz`` ``index_ready``
  contract) or an unexpected engine failure.

A GDELT API failure is NOT an error: the bridge degrades to an empty
``gdelt_series`` with an explanatory ``note`` and the endpoint still
returns 200.
"""

from __future__ import annotations

from typing import Any, TypeVar

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError

from awareness.gdeltx.engine import GdeltBridge
from awareness.gdeltx.models import (
    CompareRequest,
    GapReport,
    GapsRequest,
    GdeltComparison,
)
from awareness.obs.logging import get_logger

logger = get_logger("gdeltx.router")

_REQUEST_T = TypeVar("_REQUEST_T", bound=BaseModel)

_MAX_GAP_TERMS = 20


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
            logger.warning("gdeltx_index_probe_failed", err=str(exc))
            return False
    return bool(getattr(index, "index_ready", False))


def _parse_or_400(model_cls: type[_REQUEST_T], **values: Any) -> _REQUEST_T:
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


def create_gdeltx_router(index_getter: Any) -> APIRouter:
    """Build the ``/gdelt`` APIRouter bound to an index accessor.

    *index_getter* is a zero-arg callable returning the process-wide
    :class:`~awareness.storage.duckdb_index.DuckDbIndex` (resolved lazily per
    request so app construction never touches the index), or the index
    itself.
    """
    router = APIRouter(prefix="/gdelt", tags=["gdelt"])

    def _bridge() -> GdeltBridge:
        import inspect  # noqa: PLC0415

        index = index_getter() if inspect.isfunction(index_getter) else index_getter
        if not _index_ready(index):
            raise HTTPException(status_code=503, detail="gdelt index not ready")
        return GdeltBridge(index)

    @router.get("/compare", response_model=GdeltComparison)
    def compare(term: str = "", window_days: int | None = None) -> GdeltComparison:
        """Compare local capture volume with external GDELT volume for *term*.

        ``term`` — word-boundary, case-insensitive (1..80 chars, no control
        characters); ``window_days`` — window length in days (1..60). When
        the GDELT API is unreachable the response is still 200 with
        ``gdelt_series`` empty and a ``note`` explaining.
        """
        req = _parse_or_400(CompareRequest, term=term, window_days=window_days)
        try:
            return _bridge().compare_with_local(term=req.term, window_days=req.window_days)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.warning("gdeltx_compare_failed", err=str(exc))
            raise HTTPException(status_code=503, detail=f"gdelt compare failed: {exc}") from exc

    @router.get("/gaps", response_model=list[GapReport])
    def gaps(terms: str = "", window_days: int | None = None) -> list[GapReport]:
        """Terms where GDELT volume is high but local capture is near-zero.

        ``terms`` — comma-separated list, max 20; ``window_days`` — window
        length in days (1..60).
        """
        req = _parse_or_400(GapsRequest, terms=terms, window_days=window_days)
        term_list = [term.strip() for term in req.terms.split(",") if term.strip()]
        if not term_list:
            raise HTTPException(status_code=400, detail="terms must not be empty")
        if len(term_list) > _MAX_GAP_TERMS:
            raise HTTPException(status_code=400, detail=f"at most {_MAX_GAP_TERMS} terms")
        try:
            return _bridge().coverage_gap(term_list, window_days=req.window_days)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.warning("gdeltx_gaps_failed", err=str(exc))
            raise HTTPException(status_code=503, detail=f"gdelt gaps failed: {exc}") from exc

    return router
