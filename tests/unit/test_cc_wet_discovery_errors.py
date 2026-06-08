from __future__ import annotations

import httpx
import pytest

from awareness.util.http import RetryableHTTPError, get_with_retries


async def test_discovery_helper_raises_on_persistent_503() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RetryableHTTPError):
            await get_with_retries(client, "https://data.commoncrawl.test/x", max_attempts=2, base_delay=0.0)


async def test_discovery_helper_returns_on_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resp = await get_with_retries(client, "https://data.commoncrawl.test/x", max_attempts=2, base_delay=0.0)
    assert resp.status_code == 404
