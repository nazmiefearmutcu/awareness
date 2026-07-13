"""Feeds/sitemap HTTP retries via get_with_retries (mocked transport)."""

from __future__ import annotations

import httpx
import pytest

from awareness.sources.feeds import _read_feed, _read_sitemap
from awareness.util.http import RetryableHTTPError, get_with_retries, reset_global_fetch_semaphore


@pytest.fixture(autouse=True)
def _reset_fetch_sem() -> None:
    reset_global_fetch_semaphore()
    yield
    reset_global_fetch_semaphore()


def _patch_client_and_retries(
    monkeypatch: pytest.MonkeyPatch,
    handler,
    *,
    module: str,
) -> None:
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(f"{module}.httpx.AsyncClient", factory)

    real = get_with_retries

    async def fast_get(client, url, **kwargs):
        kwargs.setdefault("base_delay", 0.0)
        kwargs.setdefault("max_attempts", 5)
        return await real(client, url, **kwargs)

    monkeypatch.setattr(f"{module}.get_with_retries", fast_get)


RSS_OK = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example</title>
    <item>
      <title>Story</title>
      <link>https://example.com/story/1</link>
    </item>
  </channel>
</rss>
"""

SITEMAP_OK = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/page-a</loc></url>
  <url><loc>https://example.com/page-b</loc></url>
</urlset>
"""


@pytest.mark.asyncio
async def test_read_feed_retries_on_503(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, content=RSS_OK)

    _patch_client_and_retries(monkeypatch, handler, module="awareness.sources.feeds")

    urls = await _read_feed("https://example.com/feed.xml", "TestBot/1.0")
    assert urls == ["https://example.com/story/1"]
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_read_feed_exhausted_503_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr("awareness.sources.feeds.httpx.AsyncClient", factory)

    real = get_with_retries

    async def fast_get(client, url, **kwargs):
        kwargs.setdefault("base_delay", 0.0)
        kwargs.setdefault("max_attempts", 3)
        return await real(client, url, **kwargs)

    monkeypatch.setattr("awareness.sources.feeds.get_with_retries", fast_get)

    with pytest.raises(RetryableHTTPError):
        await _read_feed("https://example.com/feed.xml", "TestBot/1.0")


@pytest.mark.asyncio
async def test_read_sitemap_retries_on_503(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, content=SITEMAP_OK)

    _patch_client_and_retries(monkeypatch, handler, module="awareness.sources.feeds")

    urls = await _read_sitemap("https://example.com/sitemap.xml", "TestBot/1.0")
    assert urls == ["https://example.com/page-a", "https://example.com/page-b"]
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_read_feed_404_returns_empty_no_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    _patch_client_and_retries(monkeypatch, handler, module="awareness.sources.feeds")

    urls = await _read_feed("https://example.com/missing.xml", "TestBot/1.0")
    assert urls == []
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_read_feed_non_200_increments_metric(monkeypatch: pytest.MonkeyPatch) -> None:
    """Permanent non-200 (404) is not retried but is counted for reliability dashboards."""
    from awareness.obs.metrics import get_metrics

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    _patch_client_and_retries(monkeypatch, handler, module="awareness.sources.feeds")
    before = get_metrics().counter_sum("feeds.fetch_non_200")
    urls = await _read_feed("https://example.com/missing.xml", "TestBot/1.0")
    assert urls == []
    assert get_metrics().counter_sum("feeds.fetch_non_200") == before + 1


@pytest.mark.asyncio
async def test_read_sitemap_non_200_increments_metric(monkeypatch: pytest.MonkeyPatch) -> None:
    from awareness.obs.metrics import get_metrics

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    _patch_client_and_retries(monkeypatch, handler, module="awareness.sources.feeds")
    before = get_metrics().counter_sum("feeds.fetch_non_200")
    urls = await _read_sitemap("https://example.com/forbidden.xml", "TestBot/1.0")
    assert urls == []
    assert get_metrics().counter_sum("feeds.fetch_non_200") == before + 1


@pytest.mark.asyncio
async def test_read_feed_retryable_error_increments_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exhausted transient retries raise and count feeds.retryable_http_error."""
    from awareness.obs.metrics import get_metrics

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr("awareness.sources.feeds.httpx.AsyncClient", factory)

    real = get_with_retries

    async def fast_get(client, url, **kwargs):
        kwargs.setdefault("base_delay", 0.0)
        kwargs.setdefault("max_attempts", 3)
        return await real(client, url, **kwargs)

    monkeypatch.setattr("awareness.sources.feeds.get_with_retries", fast_get)

    before = get_metrics().counter_sum("feeds.retryable_http_error")
    with pytest.raises(RetryableHTTPError):
        await _read_feed("https://example.com/feed.xml", "TestBot/1.0")
    assert get_metrics().counter_sum("feeds.retryable_http_error") == before + 1


@pytest.mark.asyncio
async def test_read_sitemap_retryable_error_increments_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awareness.obs.metrics import get_metrics

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr("awareness.sources.feeds.httpx.AsyncClient", factory)

    real = get_with_retries

    async def fast_get(client, url, **kwargs):
        kwargs.setdefault("base_delay", 0.0)
        kwargs.setdefault("max_attempts", 3)
        return await real(client, url, **kwargs)

    monkeypatch.setattr("awareness.sources.feeds.get_with_retries", fast_get)

    before = get_metrics().counter_sum("feeds.retryable_http_error")
    with pytest.raises(RetryableHTTPError):
        await _read_sitemap("https://example.com/sitemap.xml", "TestBot/1.0")
    assert get_metrics().counter_sum("feeds.retryable_http_error") == before + 1

