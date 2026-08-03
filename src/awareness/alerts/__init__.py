"""Alerts subsystem: rule management, corpus evaluation, webhook delivery.

Exposes the :class:`~awareness.alerts.store.AlertStore` for persistence, the
:class:`~awareness.alerts.engine.AlertEngine` that turns rules into firings
against the DuckDbIndex ``captures`` view, and the FastAPI router factory
:func:`~awareness.alerts.router.create_alerts_router`.
"""

from awareness.alerts.engine import AlertEngine
from awareness.alerts.models import AlertFiring, AlertRule, AlertRuleCreate, AlertStatus
from awareness.alerts.notify import deliver_webhook
from awareness.alerts.router import create_alerts_router
from awareness.alerts.store import AlertStore

__all__ = [
    "AlertEngine",
    "AlertFiring",
    "AlertRule",
    "AlertRuleCreate",
    "AlertStatus",
    "AlertStore",
    "create_alerts_router",
    "deliver_webhook",
]
