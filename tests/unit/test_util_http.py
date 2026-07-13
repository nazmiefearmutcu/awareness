from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from awareness.util.http import (
    RETRYABLE_STATUS,
    RetryableHTTPError,
    _retry_after_seconds,
    acquire_fetch_slot,
    get_with_retries,
    global_fetch_semaphore,
    reset_global_fetch_semaphore,
)


def _client_with_handler(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _reset_fetch_sem() -> None:
    reset_global_fetch_semaphore()
    yield
    reset_global_fetch_semaphore()


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


async def test_global_fetch_semaphore_caps_concurrent_holders() -> None:
    """Process-wide semaphore must not admit more than ``limit`` holders."""
    limit = 2
    holders = 0
    peak = 0
    lock = asyncio.Lock()

    async def hold() -> None:
        nonlocal holders, peak
        async with acquire_fetch_slot(limit):
            async with lock:
                holders += 1
                peak = max(peak, holders)
            await asyncio.sleep(0.05)
            async with lock:
                holders -= 1

    await asyncio.gather(*[hold() for _ in range(6)])
    assert peak == limit
    # Semaphore should be fully released.
    sem = global_fetch_semaphore(limit)
    assert sem._value == limit  # noqa: SLF001 — unit probe of free slots


async def test_get_with_retries_acquires_fetch_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each HTTP attempt must go through the process-wide fetch slot."""
    enters = {"n": 0}

    @asynccontextmanager
    async def fake_slot(limit: int | None = None):
        enters["n"] += 1
        yield

    monkeypatch.setattr("awareness.util.http.acquire_fetch_slot", fake_slot)

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(503)
        return httpx.Response(200, content=b"ok")

    async with _client_with_handler(handler) as client:
        resp = await get_with_retries(
            client, "https://example.test/x", max_attempts=4, base_delay=0.0
        )
    assert resp.status_code == 200
    # One acquisition per attempt (retry after 503, then success).
    assert enters["n"] == 2
    assert calls["n"] == 2


def test_408_is_retryable_status() -> None:
    assert 408 in RETRYABLE_STATUS


async def test_retries_then_succeeds_on_408() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(408)
        return httpx.Response(200, content=b"ok")

    async with _client_with_handler(handler) as client:
        resp = await get_with_retries(
            client, "https://example.test/x", max_attempts=4, base_delay=0.0
        )
    assert resp.status_code == 200
    assert calls["n"] == 2


def test_retry_after_delta_seconds() -> None:
    resp = httpx.Response(429, headers={"Retry-After": "12"})
    assert _retry_after_seconds(resp) == 12.0


def test_retry_after_http_date(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP-date Retry-After is converted to a non-negative delay from now."""
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    fixed_now = datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    monkeypatch.setattr("awareness.util.http.datetime", _FixedDateTime)

    future = fixed_now + timedelta(seconds=45)
    resp = httpx.Response(503, headers={"Retry-After": format_datetime(future, usegmt=True)})
    delay = _retry_after_seconds(resp)
    assert delay is not None
    assert abs(delay - 45.0) < 1.0


def test_retry_after_http_date_past_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    fixed_now = datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    monkeypatch.setattr("awareness.util.http.datetime", _FixedDateTime)

    past = fixed_now - timedelta(seconds=30)
    resp = httpx.Response(503, headers={"Retry-After": format_datetime(past, usegmt=True)})
    assert _retry_after_seconds(resp) == 0.0


def test_retry_after_garbage_returns_none() -> None:
    resp = httpx.Response(503, headers={"Retry-After": "not-a-date"})
    assert _retry_after_seconds(resp) is None
