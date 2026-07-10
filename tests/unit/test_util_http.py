from __future__ import annotations

import httpx
import pytest

from awareness.util.http import RetryableHTTPError, get_with_retries


def _client_with_handler(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_retries_then_succeeds_on_500() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500)
        return httpx.Response(200, content=b"ok")

    async with _client_with_handler(handler) as client:
        resp = await get_with_retries(
            client, "https://example.test/x", max_attempts=5, base_delay=0.0
        )
    assert resp.status_code == 200
    assert resp.content == b"ok"
    assert calls["n"] == 3


async def test_raises_after_exhausting_retries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with _client_with_handler(handler) as client:
        with pytest.raises(RetryableHTTPError):
            await get_with_retries(
                client, "https://example.test/x", max_attempts=3, base_delay=0.0
            )


async def test_404_is_not_retried_and_returns_response() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    async with _client_with_handler(handler) as client:
        resp = await get_with_retries(
            client, "https://example.test/x", max_attempts=5, base_delay=0.0
        )
    assert resp.status_code == 404
    assert calls["n"] == 1
