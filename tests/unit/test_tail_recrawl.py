"""Tail recrawl SSRF guard + transient HTTP retry tests."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from awareness.sources.tail_recrawl import _get_public_url
from awareness.util.http import RetryableHTTPError, get_with_retries, reset_global_fetch_semaphore


class RecordingClient:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def get(self, url: str, **kwargs) -> httpx.Response:
        self.urls.append(url)
        request = httpx.Request("GET", url)
        if url == "https://public.example/news":
            return httpx.Response(
                302,
                headers={"Location": "http://127.0.0.1:8080/admin"},
                request=request,
            )
        return httpx.Response(200, text="internal secret", request=request)


@pytest.fixture(autouse=True)
def _reset_fetch_sem() -> None:
    reset_global_fetch_semaphore()
    yield
    reset_global_fetch_semaphore()


def test_get_public_url_rejects_internal_redirect_before_fetch(monkeypatch) -> None:
    def fake_is_public_http_url(url: str | None) -> bool:
        return url == "https://public.example/news"

    monkeypatch.setattr("awareness.sources.tail_recrawl.is_public_http_url", fake_is_public_http_url)
    client = RecordingClient()

    response = asyncio.run(_get_public_url(client, "https://public.example/news"))  # type: ignore[arg-type]

    assert response is None
    assert client.urls == ["https://public.example/news"]


@pytest.mark.asyncio
async def test_get_public_url_retries_on_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """Article fetch path must retry transient 503 via get_with_retries."""
    monkeypatch.setattr(
        "awareness.sources.tail_recrawl.is_public_http_url",
        lambda url: bool(url and str(url).startswith("https://")),
    )
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, text="<html>ok</html>", request=request)

    real = get_with_retries

    async def fast_get(client, url, **kwargs):
        kwargs.setdefault("base_delay", 0.0)
        kwargs.setdefault("max_attempts", 5)
        return await real(client, url, **kwargs)

    monkeypatch.setattr("awareness.sources.tail_recrawl.get_with_retries", fast_get)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resp = await _get_public_url(client, "https://news.example/article")

    assert resp is not None
    assert resp.status_code == 200
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_get_public_url_raises_after_exhausted_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "awareness.sources.tail_recrawl.is_public_http_url",
        lambda url: True,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    real = get_with_retries

    async def fast_get(client, url, **kwargs):
        kwargs.setdefault("base_delay", 0.0)
        kwargs.setdefault("max_attempts", 3)
        return await real(client, url, **kwargs)

    monkeypatch.setattr("awareness.sources.tail_recrawl.get_with_retries", fast_get)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RetryableHTTPError):
            await _get_public_url(client, "https://news.example/down")


@pytest.mark.asyncio
async def test_run_partition_retryable_error_increments_metric() -> None:
    """Exhausted HTTP retries count tail.retryable_http_error (and re-raise)."""
    from unittest.mock import AsyncMock, patch

    from awareness.obs.metrics import get_metrics
    from awareness.schemas.doc import SourceKind
    from awareness.sources.base import AdapterContext, PartitionSpec
    from awareness.sources.tail_recrawl import TailRecrawlAdapter

    url = "https://news.example.com/down"
    get_mock = AsyncMock(side_effect=RetryableHTTPError(f"{url} -> 503 after 3 attempts"))

    class _FakeLimiter:
        def domain(self, dom: str, override_delay: float | None = None):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    class _FakeRobots:
        async def is_allowed(self, u, ua):
            return True

        def crawl_delay(self, u):
            return None

    ctx = AdapterContext(
        user_agent="TestBot/1.0",
        job_id="job-1",
        task_id="task-1",
        batch_id="b1",
        ingest_version="0",
        checkpoint={},
        is_stopping=lambda: False,
        extras={"limiter": _FakeLimiter(), "robots": _FakeRobots()},
    )
    partition = PartitionSpec(
        source_type=SourceKind.TAIL_RECRAWL,
        partition_key=f"tail:{url}",
        payload={"url": url, "discovery_channel": "rss"},
    )

    before = get_metrics().counter_sum("tail.retryable_http_error")
    with (
        patch("awareness.sources.tail_recrawl.is_public_http_url", return_value=True),
        patch("awareness.sources.tail_recrawl._get_public_url", get_mock),
    ):
        adapter = TailRecrawlAdapter()
        with pytest.raises(RetryableHTTPError):
            async for _ in adapter.run_partition(partition, ctx):
                pass
    assert get_metrics().counter_sum("tail.retryable_http_error") == before + 1

