from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from awareness.obs.metrics import MetricsRegistry
from awareness.util.http import (
    DEFAULT_CONNECT_TIMEOUT_CAP,
    DEFAULT_CONNECT_TIMEOUT_FLOOR,
    RETRYABLE_STATUS,
    RetryableHTTPError,
    _retry_after_seconds,
    _status_class,
    aclose_shared_async_clients,
    acquire_fetch_slot,
    build_http_timeout,
    get_shared_async_client,
    get_with_retries,
    global_fetch_semaphore,
    reset_global_fetch_semaphore,
    reset_shared_async_clients,
    shared_async_client_pool_size,
)


def _client_with_handler(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _reset_fetch_sem() -> None:
    reset_global_fetch_semaphore()
    reset_shared_async_clients()
    yield
    reset_global_fetch_semaphore()
    reset_shared_async_clients()


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


@pytest.mark.asyncio
async def test_shared_async_client_reuses_same_instance() -> None:
    """Same (timeout, follow_redirects) key returns one pooled client."""
    c1 = await get_shared_async_client(timeout=12.0, follow_redirects=True)
    c2 = await get_shared_async_client(timeout=12.0, follow_redirects=True)
    assert c1 is c2
    assert shared_async_client_pool_size() == 1
    await aclose_shared_async_clients()
    assert shared_async_client_pool_size() == 0


@pytest.mark.asyncio
async def test_shared_async_client_keys_by_timeout_and_redirects() -> None:
    """Different timeout or redirect policy → separate pool entries."""
    a = await get_shared_async_client(timeout=10.0, follow_redirects=True)
    b = await get_shared_async_client(timeout=60.0, follow_redirects=True)
    c = await get_shared_async_client(timeout=10.0, follow_redirects=False)
    assert a is not b
    assert a is not c
    assert b is not c
    assert shared_async_client_pool_size() == 3
    # Same key again reuses.
    assert await get_shared_async_client(timeout=10.0, follow_redirects=True) is a
    await aclose_shared_async_clients()


@pytest.mark.asyncio
async def test_shared_async_client_works_with_mock_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pool factory accepts limits; GET works through get_with_retries."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=b"pooled-ok")

    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr("awareness.util.http.httpx.AsyncClient", factory)
    client = await get_shared_async_client(timeout=5.0, follow_redirects=True)
    # Second acquire must reuse the MockTransport client.
    assert await get_shared_async_client(timeout=5.0, follow_redirects=True) is client
    resp = await get_with_retries(client, "https://example.test/pooled", max_attempts=2, base_delay=0.0)
    assert resp.status_code == 200
    assert resp.content == b"pooled-ok"
    assert calls["n"] == 1
    await aclose_shared_async_clients()


def test_status_class_buckets() -> None:
    assert _status_class(200) == "2xx"
    assert _status_class(301) == "3xx"
    assert _status_class(404) == "4xx"
    assert _status_class(503) == "5xx"
    assert _status_class(99) == "other"


def test_build_http_timeout_splits_connect_and_read() -> None:
    """Connect is capped/fractional; read/write keep the full budget."""
    t = build_http_timeout(30.0)
    assert t.connect == pytest.approx(7.5)  # 0.25 * 30
    assert t.read == pytest.approx(30.0)
    assert t.write == pytest.approx(30.0)
    assert t.pool == pytest.approx(7.5)

    # Cap connect at 10s for long overall budgets.
    long = build_http_timeout(120.0)
    assert long.connect == pytest.approx(DEFAULT_CONNECT_TIMEOUT_CAP)
    assert long.read == pytest.approx(120.0)

    # Floor connect for tiny budgets (never below 1s unless total is smaller).
    tiny = build_http_timeout(2.0)
    assert tiny.connect == pytest.approx(DEFAULT_CONNECT_TIMEOUT_FLOOR)
    assert tiny.read == pytest.approx(2.0)

    # Connect never exceeds total.
    micro = build_http_timeout(0.5)
    assert micro.connect == pytest.approx(0.5)
    assert micro.read == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_shared_async_client_uses_split_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pooled clients receive build_http_timeout, not a bare float."""
    seen: dict[str, object] = {}
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return original(*args, **kwargs)

    monkeypatch.setattr("awareness.util.http.httpx.AsyncClient", factory)
    await get_shared_async_client(timeout=40.0, follow_redirects=True)
    timeout = seen["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == pytest.approx(10.0)  # capped
    assert timeout.read == pytest.approx(40.0)
    await aclose_shared_async_clients()


@pytest.mark.asyncio
async def test_get_with_retries_records_latency_and_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful GET records latency hist + attempt/status counters."""
    isolated = MetricsRegistry()
    monkeypatch.setattr("awareness.obs.metrics._REGISTRY", isolated)
    # util.http imported get_metrics at module load via function body — patch
    # the call site module's get_metrics binding used inside _record_fetch_attempt.
    monkeypatch.setattr("awareness.util.http.get_metrics", lambda: isolated)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"ok")

    async with _client_with_handler(handler) as client:
        resp = await get_with_retries(
            client, "https://example.test/ok", max_attempts=2, base_delay=0.0
        )
    assert resp.status_code == 200
    assert isolated.counter_sum("http.fetch_attempts") == 1.0
    assert isolated.counter_sum("http.fetch_retries") == 0.0
    assert isolated.counter_value("http.fetch_status", labels={"status_class": "2xx"}) == 1.0
    snap = isolated.snapshot()
    hists = [h for h in snap["histograms"] if h["name"] == "http.fetch_seconds"]
    assert len(hists) == 1
    assert hists[0]["count"] == 1
    assert hists[0]["labels"].get("outcome") == "ok"
    assert hists[0]["labels"].get("status_class") == "2xx"
    assert hists[0]["sum"] >= 0.0


@pytest.mark.asyncio
async def test_get_with_retries_records_retry_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient 503 then success records retryable + ok attempts and a retry."""
    isolated = MetricsRegistry()
    monkeypatch.setattr("awareness.util.http.get_metrics", lambda: isolated)

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(503)
        return httpx.Response(200, content=b"ok")

    async with _client_with_handler(handler) as client:
        resp = await get_with_retries(
            client, "https://example.test/retry", max_attempts=4, base_delay=0.0
        )
    assert resp.status_code == 200
    assert isolated.counter_sum("http.fetch_attempts") == 2.0
    # Second attempt (index 1) increments retries.
    assert isolated.counter_sum("http.fetch_retries") == 1.0
    assert isolated.counter_value("http.fetch_status", labels={"status_class": "5xx"}) == 1.0
    assert isolated.counter_value("http.fetch_status", labels={"status_class": "2xx"}) == 1.0
    assert (
        isolated.counter_value("http.fetch_attempts", labels={"outcome": "retryable"}) == 1.0
    )
    assert isolated.counter_value("http.fetch_attempts", labels={"outcome": "ok"}) == 1.0


@pytest.mark.asyncio
async def test_get_with_retries_records_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exhausted transport failures still emit transport_error metrics."""
    isolated = MetricsRegistry()
    monkeypatch.setattr("awareness.util.http.get_metrics", lambda: isolated)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    async with _client_with_handler(handler) as client:
        with pytest.raises(RetryableHTTPError):
            await get_with_retries(
                client, "https://example.test/down", max_attempts=2, base_delay=0.0
            )
    assert isolated.counter_sum("http.fetch_attempts") == 2.0
    assert (
        isolated.counter_value(
            "http.fetch_attempts", labels={"outcome": "transport_error"}
        )
        == 2.0
    )
    # First attempt has no retry flag; second attempt does.
    assert isolated.counter_sum("http.fetch_retries") == 1.0
