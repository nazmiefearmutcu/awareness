"""Feeds/sitemap HTTP retries via get_with_retries (mocked transport)."""

from __future__ import annotations

import gzip
from types import SimpleNamespace

import httpx
import pytest

from awareness.sources.feeds import (
    _maybe_decompress_body,
    _read_feed,
    _read_sitemap,
    entry_primary_url,
)
from awareness.util.http import (
    RetryableHTTPError,
    get_with_retries,
    reset_global_fetch_semaphore,
    reset_shared_async_clients,
)


@pytest.fixture(autouse=True)
def _reset_fetch_sem() -> None:
    reset_global_fetch_semaphore()
    reset_shared_async_clients()
    yield
    reset_global_fetch_semaphore()
    reset_shared_async_clients()


def _patch_client_and_retries(
    monkeypatch: pytest.MonkeyPatch,
    handler,
    *,
    module: str,
) -> None:
    """Inject MockTransport via the shared pooled client + fast retries."""
    original = httpx.AsyncClient
    # Fresh mock client each call is fine for unit tests; production pooling
    # is covered in test_util_http.
    mock_client = original(transport=httpx.MockTransport(handler))

    async def fake_shared(**kwargs):
        return mock_client

    monkeypatch.setattr(f"{module}.get_shared_async_client", fake_shared)

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

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def fake_shared(**kwargs):
        return mock_client

    monkeypatch.setattr("awareness.sources.feeds.get_shared_async_client", fake_shared)

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

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def fake_shared(**kwargs):
        return mock_client

    monkeypatch.setattr("awareness.sources.feeds.get_shared_async_client", fake_shared)

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

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def fake_shared(**kwargs):
        return mock_client

    monkeypatch.setattr("awareness.sources.feeds.get_shared_async_client", fake_shared)

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


def test_maybe_decompress_body_gunzips() -> None:
    raw = b"<rss/>"
    assert _maybe_decompress_body(gzip.compress(raw)) == raw
    assert _maybe_decompress_body(raw) == raw
    assert _maybe_decompress_body(b"") == b""


def test_entry_primary_url_prefers_link() -> None:
    entry = SimpleNamespace(link="https://example.com/a", links=[])
    assert entry_primary_url(entry) == "https://example.com/a"


def test_entry_primary_url_from_atom_links_alternate() -> None:
    """Atom entries often only populate links[] (no entry.link)."""
    entry = SimpleNamespace(
        link=None,
        links=[
            {"rel": "self", "href": "https://example.com/atom/entry/1"},
            {"rel": "alternate", "href": "https://example.com/article/1"},
        ],
    )
    assert entry_primary_url(entry) == "https://example.com/article/1"


def test_entry_primary_url_fallback_any_http() -> None:
    entry = SimpleNamespace(
        link="",
        links=[{"rel": "related", "href": "https://cdn.example.com/x"}],
    )
    assert entry_primary_url(entry) == "https://cdn.example.com/x"


def test_entry_primary_url_skips_non_http() -> None:
    entry = SimpleNamespace(link="mailto:a@b.com", links=[{"href": "/relative"}])
    assert entry_primary_url(entry) is None


@pytest.mark.asyncio
async def test_read_feed_accepts_gzip_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gzip-wrapped RSS (no Content-Encoding) still yields entry links."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=gzip.compress(RSS_OK))

    _patch_client_and_retries(monkeypatch, handler, module="awareness.sources.feeds")

    urls = await _read_feed("https://example.com/feed.xml.gz", "TestBot/1.0")
    assert urls == ["https://example.com/story/1"]


@pytest.mark.asyncio
async def test_read_sitemap_accepts_gzip_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=gzip.compress(SITEMAP_OK))

    _patch_client_and_retries(monkeypatch, handler, module="awareness.sources.feeds")

    urls = await _read_sitemap("https://example.com/sitemap.xml.gz", "TestBot/1.0")
    assert urls == ["https://example.com/page-a", "https://example.com/page-b"]


ATOM_LINKS_ONLY = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example</title>
  <entry>
    <title>Story</title>
    <link rel="alternate" href="https://example.com/atom-story/1"/>
    <id>tag:example.com,2026:1</id>
  </entry>
</feed>
"""


@pytest.mark.asyncio
async def test_read_feed_atom_links_without_link_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=ATOM_LINKS_ONLY)

    _patch_client_and_retries(monkeypatch, handler, module="awareness.sources.feeds")

    urls = await _read_feed("https://example.com/atom.xml", "TestBot/1.0")
    assert urls == ["https://example.com/atom-story/1"]

