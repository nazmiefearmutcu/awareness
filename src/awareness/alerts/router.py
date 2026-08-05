"""FastAPI router exposing alert rules and firings under ``/alerts``.

The router is a factory: the integrator binds it to getters for the
process-wide index and store::

    app.include_router(create_alerts_router(index_getter, store_getter))

Error contract (all endpoints):

* ``400`` — invalid rule payload / patch (pydantic ``ValidationError`` or
  ``ValueError`` from the store).
* ``404`` — unknown rule id (GET/PUT/DELETE).
* ``503`` — index not ready (the engine raises ``RuntimeError("index not
  ready")`` when the DuckDB index cannot answer queries, mirroring the
  ``/healthz`` contract).
* ``500`` — any unexpected failure, logged through structlog.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Response
from pydantic import ValidationError

from awareness.alerts.engine import AlertEngine
from awareness.alerts.models import AlertFiring, AlertRule, AlertRuleCreate, AlertStatus
from awareness.alerts.notify import deliver_webhook
from awareness.alerts.store import AlertStore
from awareness.obs.logging import get_logger
from awareness.storage.duckdb_index import DuckDbIndex

logger = get_logger("alerts.router")

_MAX_FIRINGS_LIMIT = 500
_DEFAULT_FIRINGS_LIMIT = 50


def create_alerts_router(  # noqa: PLR0915 - spec-mandated endpoint surface
    index_getter: Callable[[], DuckDbIndex],
    store_getter: Callable[[], AlertStore],
) -> APIRouter:
    """Build the ``/alerts`` APIRouter bound to *index_getter* / *store_getter*.

    *index_getter* returns the process-wide
    :class:`~awareness.storage.duckdb_index.DuckDbIndex` (or a duck-typed shim
    exposing ``health_snapshot`` + ``execute``); *store_getter* returns the
    :class:`~awareness.alerts.store.AlertStore`.
    """
    router = APIRouter(prefix="/alerts", tags=["alerts"])

    def _ensure_ready() -> None:
        """Probe index readiness; RuntimeError propagates to the 503 mapping."""
        AlertEngine(index_getter(), store_getter()).ensure_ready()

    def _call(fn: Callable[..., Any], *, not_found: str | None = None) -> Any:
        """Shared guard: 503 when the index is not ready; 400/404/500 mapping."""
        try:
            return fn()
        except KeyError as exc:
            if not_found:
                raise HTTPException(status_code=404, detail=not_found) from exc
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(
                status_code=503, detail=f"alerts index not ready: {exc}"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.warning("alerts_endpoint_failed", err=str(exc))
            raise HTTPException(
                status_code=500, detail=f"alerts endpoint failed: {exc}"
            ) from exc

    @router.get("/rules")
    def list_rules() -> list[dict[str, Any]]:
        """List all alert rules."""

        def _list() -> list[dict[str, Any]]:
            _ensure_ready()
            return [r.model_dump(mode="json") for r in store_getter().list_rules()]

        return _call(_list)

    @router.post("/rules", status_code=201)
    def create_rule(body: dict[str, Any]) -> dict[str, Any]:
        """Create an alert rule; 400 on validation failure."""
        try:
            payload = AlertRuleCreate.model_validate(body)
        except ValidationError as exc:
            details = "; ".join(
                ".".join(str(part) for part in err["loc"]) + ": " + err["msg"]
                for err in exc.errors()
            )
            raise HTTPException(status_code=400, detail=f"bad rule: {details}") from exc

        def _create() -> dict[str, Any]:
            _ensure_ready()
            return store_getter().create_rule(payload).model_dump(mode="json")

        return _call(_create)

    @router.get("/rules/export", response_model=list[AlertRule])
    def export_rules() -> list[dict[str, Any]]:
        """Download all rules as a JSON array (webhooks included)."""

        def _export() -> list[dict[str, Any]]:
            _ensure_ready()
            return store_getter().export_rules()

        return _call(_export)

    @router.post("/rules/import")
    def import_rules(body: Any = Body()) -> dict[str, int]:  # noqa: B008
        """Bulk-create rules from a JSON array (or ``{"rules": [...],
        "replace": bool}``); returns ``{"created": N, "skipped": M}``.

        Rules whose name already exists are skipped unless ``replace`` is set
        (delete + recreate). Validation failures map to a 400 before any
        write. Import is pure store I/O — no index required.
        """
        if isinstance(body, dict):
            rules = body.get("rules", [])
            replace = bool(body.get("replace", False))
        elif isinstance(body, list):
            rules = body
            replace = False
        else:
            raise HTTPException(
                status_code=400,
                detail="import body must be a JSON array of rules "
                "or {rules: [...], replace: bool}",
            )
        if not isinstance(rules, list):
            raise HTTPException(
                status_code=400, detail="import body must be a JSON array of rules"
            )
        for raw in rules:
            try:
                AlertRuleCreate.model_validate(raw)
            except ValidationError as exc:
                details = "; ".join(
                    ".".join(str(part) for part in err["loc"]) + ": " + err["msg"]
                    for err in exc.errors()
                )
                raise HTTPException(
                    status_code=400, detail=f"bad rule: {details}"
                ) from exc

        def _import() -> dict[str, int]:
            created, skipped = store_getter().import_rules(rules, replace)
            return {"created": created, "skipped": skipped}

        return _call(_import)

    @router.get("/rules/{rule_id}")
    def get_rule(rule_id: str) -> dict[str, Any]:
        """Fetch a single alert rule; 404 when unknown."""

        def _get() -> dict[str, Any]:
            _ensure_ready()
            rule = store_getter().get_rule(rule_id)
            if rule is None:
                raise KeyError(rule_id)
            return rule.model_dump(mode="json")

        return _call(_get, not_found="alert rule not found")

    @router.put("/rules/{rule_id}")
    def update_rule(rule_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Patch an alert rule; 404 when unknown, 400 on a bad patch."""

        def _update() -> dict[str, Any]:
            _ensure_ready()
            return store_getter().update_rule(rule_id, body).model_dump(mode="json")

        return _call(_update, not_found="alert rule not found")

    @router.delete("/rules/{rule_id}")
    def delete_rule(rule_id: str) -> Response:
        """Delete an alert rule; 404 when unknown, else 204.

        Note: no ``status_code`` on the decorator — declaring 204 would make
        FastAPI force every response (including the 404) to 204.
        """

        def _delete() -> bool:
            _ensure_ready()
            if not store_getter().delete_rule(rule_id):
                raise KeyError(rule_id)
            return True

        _call(_delete, not_found="alert rule not found")
        return Response(status_code=204)

    @router.post("/rules/{rule_id}/test")
    def test_rule(rule_id: str) -> dict[str, Any]:
        """Evaluate ONE rule ignoring cooldown (manual test; never persists).

        Returns the current status report: ``fired``, the firing detail when
        it fired, ``count`` vs ``threshold``, and whether a normal run would
        have been suppressed by cooldown. 404 for unknown rules; 503 when the
        index is not ready.
        """

        def _test() -> dict[str, Any]:
            _ensure_ready()
            engine = AlertEngine(index_getter(), store_getter())
            report = engine.check_rule_report(rule_id)
            if report is None:
                raise KeyError(rule_id)
            return {
                "fired": report.fired,
                "firing": (
                    report.firing.model_dump(mode="json")
                    if report.firing is not None
                    else None
                ),
                "count": report.count,
                "threshold": report.threshold,
                "suppressed_by_cooldown": report.suppressed_by_cooldown,
                "active": report.active,
                "required": report.required,
            }

        return _call(_test, not_found="alert rule not found")

    @router.post("/check")
    async def check() -> dict[str, Any]:
        """Evaluate all active rules; return firings plus webhook delivery
        results (one entry per firing whose rule has a webhook URL)."""
        engine = AlertEngine(index_getter(), store_getter())
        try:
            firings = await asyncio.to_thread(engine.evaluate_rules)
        except RuntimeError as exc:
            raise HTTPException(
                status_code=503, detail=f"alerts index not ready: {exc}"
            ) from exc
        except Exception as exc:
            logger.warning("alerts_check_failed", err=str(exc))
            raise HTTPException(status_code=500, detail=f"alerts check failed: {exc}") from exc

        store = store_getter()
        deliveries: list[dict[str, Any]] = []
        for firing in firings:
            rule = store.get_rule(firing.rule_id)
            if rule is None:
                continue
            for url in rule.webhooks:
                delivered = await deliver_webhook(
                    url, firing, format=rule.webhook_format
                )
                deliveries.append(
                    {"rule_id": firing.rule_id, "webhook_url": url, "delivered": delivered}
                )
        return {
            "firings": [f.model_dump(mode="json") for f in firings],
            "deliveries": deliveries,
        }

    @router.get("/status")
    def status() -> dict[str, Any]:
        """Subsystem status: rule counts, firings in the last 24h, last firing."""

        def _status() -> dict[str, Any]:
            _ensure_ready()
            store = store_getter()
            rules = store.list_rules()
            active = [r for r in rules if r.active]
            since = datetime.now(UTC) - timedelta(hours=24)
            last = store.list_firings(limit=1)
            return AlertStatus(
                rules_total=len(rules),
                rules_active=len(active),
                firings_24h=store.count_firings_since(since),
                last_firing=last[0]["fired_at"] if last else None,
            ).model_dump(mode="json")

        return _call(_status)

    @router.get("/firings")
    def firings(limit: int = Query(_DEFAULT_FIRINGS_LIMIT)) -> list[dict[str, Any]]:
        """Recent firing history (newest first); limit clamped to 1..500."""

        def _list() -> list[dict[str, Any]]:
            _ensure_ready()
            clamped = min(max(int(limit), 1), _MAX_FIRINGS_LIMIT)
            rows = store_getter().list_firings(limit=clamped)
            return [AlertFiring.model_validate(r).model_dump(mode="json") for r in rows]

        return _call(_list)

    return router
