"""Webhook delivery for alert firings.

:func:`deliver_webhook` POSTs a firing to the rule's webhook URL in one of
two formats:

* ``json`` (default) — ``{"event": "alert", "firing": {...}}``, unchanged for
  backward compatibility with existing consumers;
* ``slack`` — a Slack-compatible ``{"text": ...}`` message for webhooks on
  ``hooks.slack.com`` (auto-detected from the host, or forced per-rule via
  ``webhook_format``).

Delivery is best-effort: one retry after a short delay, never raises, and
logs failures through structlog so the caller can record delivery status
without error handling.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from awareness.alerts.models import AlertFiring
from awareness.obs.logging import get_logger
from awareness.util.urls import is_public_http_url

logger = get_logger("alerts.notify")

# Request timeout for a single POST (seconds).
TIMEOUT_SECONDS = 10.0
# Delay before the single retry (seconds).
RETRY_DELAY_SECONDS = 2.0
# Total attempts: original + one retry.
_ATTEMPTS = 2

# Slack truncates a message ``text`` above this length; longer messages fall
# back to a ``blocks`` payload (one section per chunk).
_SLACK_TEXT_LIMIT = 3000
# Host suffix that marks an incoming webhook as Slack.
_SLACK_HOST = "hooks.slack.com"

VALID_FORMATS = ("json", "slack")


def validate_webhook_url(webhook_url: str) -> str:
    """Reject webhook targets that are not public http(s) endpoints.

    Raises :class:`ValueError` for non-http schemes, loopback/private/
    link-local hosts, userinfo, or unresolvable hosts — preventing the
    alert runner from being used as an SSRF probe into the local network
    or cloud metadata endpoints.
    """
    parsed = urlparse(webhook_url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("webhook_url must be http(s)")
    if parsed.username or parsed.password:
        raise ValueError("webhook_url must not contain credentials")
    if not is_public_http_url(webhook_url):
        raise ValueError("webhook_url must point to a public host")
    return webhook_url


def detect_webhook_format(webhook_url: str) -> str:
    """Return ``"slack"`` for ``hooks.slack.com`` targets, else ``"json"``."""
    host = (urlparse(webhook_url).hostname or "").rstrip(".").lower()
    return "slack" if host == _SLACK_HOST else "json"


def _format_fired_at(fired_at: datetime) -> str:
    """UTC ``YYYY-MM-DDTHH:MM:SSZ`` for the Slack message text."""
    return fired_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slack_text(firing: AlertFiring) -> str:
    return (
        f"{firing.rule_name}: '{firing.term}' fired — count {firing.count:g} "
        f"≥ threshold {firing.threshold:g} at {_format_fired_at(firing.fired_at)} "
        f"({firing.detail})"
    )


def build_slack_payload(firing: AlertFiring) -> dict[str, Any]:
    """Slack-compatible message for *firing*.

    A single ``text`` message when it fits Slack's limit; otherwise a
    ``blocks`` fallback with the message chunked into section blocks.
    """
    text = _slack_text(firing)
    if len(text) <= _SLACK_TEXT_LIMIT:
        return {"text": text}
    blocks: list[dict[str, Any]] = []
    remaining = text
    while remaining:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": remaining[:_SLACK_TEXT_LIMIT]},
            }
        )
        remaining = remaining[_SLACK_TEXT_LIMIT:]
    return {"blocks": blocks}


def _payload_for(firing: AlertFiring, fmt: str) -> dict[str, Any]:
    if fmt == "slack":
        return build_slack_payload(firing)
    return {"event": "alert", "firing": firing.model_dump(mode="json")}


async def deliver_webhook(
    webhook_url: str, firing: AlertFiring, format: str | None = None
) -> bool:
    """POST *firing* to *webhook_url*; return True on a 2xx delivery.

    *format* is a delivery hint: ``"json"`` (default), ``"slack"``, or
    ``None`` to auto-detect from the host (``hooks.slack.com`` → Slack).
    Retries once after :data:`RETRY_DELAY_SECONDS`. Never raises: connection
    errors, timeouts and non-2xx responses all resolve to ``False`` with a
    structlog warning. Invalid (non-public) URLs are rejected up front.
    """
    validate_webhook_url(webhook_url)
    fmt = format if format is not None else detect_webhook_format(webhook_url)
    if fmt not in VALID_FORMATS:
        raise ValueError(f"unknown webhook format: {fmt!r}")
    payload = _payload_for(firing, fmt)
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
