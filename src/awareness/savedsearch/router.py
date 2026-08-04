"""FastAPI router exposing saved searches under ``/saved``.

The router is a factory: the integrator binds it to getters for the
process-wide store and search index::

    app.include_router(create_savedsearch_router(store_getter, index_getter))

CRUD endpoints are pure store I/O and never touch the index; only
``GET /saved/{id}/run`` needs the :class:`~awareness.storage.duckdb_index.DuckDbIndex`
(probed via ``health_snapshot`` first, mirroring the ``/healthz`` contract).

Error contract (all endpoints):

* ``400`` — invalid saved-search payload / patch (pydantic
  ``ValidationError`` or ``ValueError`` from the store).
* ``404`` — unknown saved-search id (GET/PUT/DELETE/PIN/RUN).
* ``503`` — index not ready on ``/run`` (``RuntimeError``).
* ``500`` — any unexpected failure, logged through structlog.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ValidationError

from awareness.obs.logging import get_logger
from awareness.savedsearch.models import SavedSearchCreate
from awareness.savedsearch.store import SavedSearchStore
from awareness.storage.duckdb_index import DuckDbIndex

logger = get_logger("savedsearch.router")


class PinBody(BaseModel):
    """Body for POST /saved/{id}/pin."""

    pinned: bool


def _validation_detail(prefix: str, exc: ValidationError) -> str:
    details = "; ".join(
        ".".join(str(part) for part in err["loc"]) + ": " + err["msg"]
        for err in exc.errors()
    )
    return f"{prefix}: {details}"


def create_savedsearch_router(  # noqa: PLR0915 - spec-mandated endpoint surface
    store_getter: Callable[[], SavedSearchStore],
    index_getter: Callable[[], DuckDbIndex],
) -> APIRouter:
    """Build the ``/saved`` APIRouter bound to *store_getter* / *index_getter*.

    *store_getter* returns the process-wide
    :class:`~awareness.savedsearch.store.SavedSearchStore`; *index_getter*
    returns the :class:`~awareness.storage.duckdb_index.DuckDbIndex` (or a
    duck-typed shim exposing ``health_snapshot`` + ``search``) used only by
    the ``/run`` endpoint.
    """
    router = APIRouter(prefix="/saved", tags=["saved"])

    def _call(fn: Callable[..., Any], *, not_found: str | None = None) -> Any:
        """Shared guard: 404/503/400/500 mapping for store+index calls."""
        try:
            return fn()
        except KeyError as exc:
            if not_found:
                raise HTTPException(status_code=404, detail=not_found) from exc
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(
                status_code=503, detail=f"saved index not ready: {exc}"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.warning("saved_endpoint_failed", err=str(exc))
            raise HTTPException(
                status_code=500, detail=f"saved endpoint failed: {exc}"
            ) from exc

    @router.get("")
    def list_saved() -> list[dict[str, Any]]:
        """List all saved searches (pinned first)."""

        def _list() -> list[dict[str, Any]]:
            return [s.model_dump(mode="json") for s in store_getter().list()]

        return _call(_list)

    @router.post("", status_code=201)
    def create_saved(body: dict[str, Any]) -> dict[str, Any]:
        """Create a saved search; 400 on validation failure."""
        try:
            payload = SavedSearchCreate.model_validate(body)
        except ValidationError as exc:
            raise HTTPException(
                status_code=400, detail=_validation_detail("bad saved search", exc)
            ) from exc

        def _create() -> dict[str, Any]:
            return (
                store_getter()
                .create(**payload.model_dump())
                .model_dump(mode="json")
            )

        return _call(_create)

    @router.get("/{saved_id}")
    def get_saved(saved_id: str) -> dict[str, Any]:
        """Fetch a single saved search; 404 when unknown."""

        def _get() -> dict[str, Any]:
            saved = store_getter().get(saved_id)
            if saved is None:
                raise KeyError(saved_id)
            return saved.model_dump(mode="json")

        return _call(_get, not_found="saved search not found")

    @router.put("/{saved_id}")
    def update_saved(saved_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Patch a saved search; 404 when unknown, 400 on a bad patch."""

        def _update() -> dict[str, Any]:
            return store_getter().update(saved_id, body).model_dump(mode="json")

        return _call(_update, not_found="saved search not found")

    @router.delete("/{saved_id}")
    def delete_saved(saved_id: str) -> Response:
        """Delete a saved search; 404 when unknown, else 204.

        No ``status_code`` on the decorator — declaring 204 would force every
        response (including the 404) to 204.
        """

        def _delete() -> bool:
            if not store_getter().delete(saved_id):
                raise KeyError(saved_id)
            return True

        _call(_delete, not_found="saved search not found")
        return Response(status_code=204)

    @router.post("/{saved_id}/pin")
    def pin_saved(saved_id: str, body: PinBody) -> dict[str, Any]:
        """Set the pinned flag; 404 when unknown."""

        def _pin() -> dict[str, Any]:
            return store_getter().pin(saved_id, body.pinned).model_dump(mode="json")

        return _call(_pin, not_found="saved search not found")

    @router.get("/{saved_id}/run")
    def run_saved(saved_id: str) -> dict[str, Any]:
        """Execute the saved query against the index; 404/503 when applicable.

        Returns the :meth:`~awareness.storage.duckdb_index.DuckDbIndex.search`
        payload verbatim and bumps the saved search's ``updated_at`` (last-run
        tracking).
        """

        def _run() -> dict[str, Any]:
            saved = store_getter().get(saved_id)
            if saved is None:
                raise KeyError(saved_id)
            idx = index_getter()
            snap = idx.health_snapshot()
            if not snap.get("ready"):
                raise RuntimeError("index not ready")
            fields = [f.strip().lower() for f in saved.fields.split(",") if f.strip()]
            payload = idx.search(
                saved.query,
                limit=saved.limit,
                mode=saved.mode,
                fields=fields,
            )
            store_getter().touch(saved_id)
            return payload

        return _call(_run, not_found="saved search not found")

    return router
