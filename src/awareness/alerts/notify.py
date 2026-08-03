"""Webhook delivery for alert firings.

:func:`deliver_webhook` POSTs a firing as JSON ``{"event": "alert", "firing":
{...}}`` to the rule's webhook URL. Delivery is best-effort: one retry after a
short delay, never raises, and logs failures through structlog so the caller
can record delivery status without error handling.
"""

from __future__ import annotations

import asyncio

import httpx

from awareness.alerts.models import AlertFiring
from awareness.obs.logging import get_logger

logger = get_logger("alerts.notify")

# Request timeout for a single POST (seconds).
TIMEOUT_SECONDS = 10.0
# Delay before the single retry (seconds).
RETRY_DELAY_SECONDS = 2.0
# Total attempts: original + one retry.
_ATTEMPTS = 2


async def deliver_webhook(webhook_url: str, firing: AlertFiring) -> bool:
    """POST *firing* to *webhook_url*; return True on a 2xx delivery.

    Retries once after :data:`RETRY_DELAY_SECONDS`. Never raises: connection
    errors, timeouts and non-2xx responses all resolve to ``False`` with a
    structlog warning.
    """
    payload = {"event": "alert", "firing": firing.model_dump(mode="json")}
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                resp = await client.post(webhook_url, json=payload)
            if resp.status_code < 400:
                return True
            logger.warning(
                "alert_webhook_non_2xx",
                url=webhook_url,
                status=resp.status_code,
                attempt=attempt,
            )
        except Exception as exc:
            logger.warning(
                "alert_webhook_failed",
                url=webhook_url,
                err=str(exc),
                attempt=attempt,
            )
        if attempt < _ATTEMPTS:
            await asyncio.sleep(RETRY_DELAY_SECONDS)
    return False
