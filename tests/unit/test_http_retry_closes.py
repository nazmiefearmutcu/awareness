"""H-27: get_with_retries must close retryable responses before sleeping/raising.

A response that was not consumed leaks its connection back to the pool only on
GC — under sustained 429/503 bursts that starves the pool.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from awareness.util import http as http_mod
from awareness.util.http import RetryableHTTPError, get_with_retries


@pytest.mark.asyncio
async def test_retryable_response_is_closed_before_retry() -> None:
    first = httpx.Response(503, request=httpx.Request("GET", "https://x.example/"))
    second = httpx.Response(200, text="ok", request=httpx.Request("GET", "https://x.example/"))
    first_aclose = AsyncMock(wraps=first.aclose)
    second_aclose = AsyncMock(wraps=second.aclose)
    first.aclose = first_aclose  # type: ignore[method-assign]
    second.aclose = second_aclose  # type: ignore[method-assign]
    client = AsyncMock()
    client.get = AsyncMock(side_effect=[first, second])

    resp = await get_with_retries(client, "https://x.example/", max_attempts=3, base_delay=0.01)
    assert resp is second
    # The retryable 503 response body must have been closed before the retry.
    first_aclose.assert_awaited_once()
    # The returned response stays open for the caller.
    second_aclose.assert_not_awaited()


@pytest.mark.asyncio
async def test_retryable_response_closed_on_final_raise() -> None:
    responses = [
        httpx.Response(429, request=httpx.Request("GET", "https://x.example/")),
        httpx.Response(429, request=httpx.Request("GET", "https://x.example/")),
    ]
    closes = [AsyncMock(wraps=r.aclose) for r in responses]
    for r, c in zip(responses, closes, strict=True):
        r.aclose = c  # type: ignore[method-assign]
    client = AsyncMock()
    client.get = AsyncMock(side_effect=responses)

    with pytest.raises(RetryableHTTPError):
        await get_with_retries(client, "https://x.example/", max_attempts=2, base_delay=0.01)
    for c in closes:
        c.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_after_is_floor_with_jitter() -> None:
    """M-35: server Retry-After is honored as a floor (capped), not clamped to 30s."""
    # Values well above the old 30s clamp must be honored (capped at 600s).
    big = http_mod._backoff_delay(0, 0.5, 120.0)
    assert big >= 120.0 and big <= 120.0 * (1.0 + http_mod.RETRY_AFTER_JITTER_FRAC)
    capped = http_mod._backoff_delay(0, 0.5, 900.0)
    assert capped <= 600.0 * (1.0 + http_mod.RETRY_AFTER_JITTER_FRAC)
    assert capped >= 600.0
    # Small values still respected.
    small = http_mod._backoff_delay(0, 0.5, 1.0)
    assert small >= 1.0 and small <= 1.0 * (1.0 + http_mod.RETRY_AFTER_JITTER_FRAC)
