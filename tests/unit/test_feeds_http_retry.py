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
    dedupe_feed_urls,
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

# Many publishers omit the sitemap xmlns; local-name fallback must still work.
SITEMAP_NO_NS = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset>
  <url><loc>https://example.com/no-ns-a</loc></url>
  <url><loc>https://example.com/no-ns-b</loc></url>
</urlset>
"""

SITEMAP_INDEX_NO_NS = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex>
  <sitemap><loc>https://example.com/child-sitemap.xml</loc></sitemap>
</sitemapindex>
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
async def test_read_sitemap_without_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un-namespaced urlset still yields loc URLs (publisher quality gap)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=SITEMAP_NO_NS)

    _patch_client_and_retries(monkeypatch, handler, module="awareness.sources.feeds")
    urls = await _read_sitemap("https://example.com/sitemap-nons.xml", "TestBot/1.0")
    assert urls == ["https://example.com/no-ns-a", "https://example.com/no-ns-b"]


@pytest.mark.asyncio
async def test_read_sitemap_index_without_namespace_follows_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un-namespaced sitemapindex still follows one level of nested sitemaps."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = str(request.url.path)
        if path.endswith("child-sitemap.xml"):
            return httpx.Response(200, content=SITEMAP_NO_NS)
        return httpx.Response(200, content=SITEMAP_INDEX_NO_NS)

    _patch_client_and_retries(monkeypatch, handler, module="awareness.sources.feeds")
    urls = await _read_sitemap("https://example.com/sitemap-index.xml", "TestBot/1.0")
    assert urls == ["https://example.com/no-ns-a", "https://example.com/no-ns-b"]


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


def test_entry_primary_url_falls_back_to_http_guid() -> None:
    """Many RSS feeds put the permalink only in guid when link is empty."""
    entry = SimpleNamespace(link=None, links=[], id="https://example.com/via-guid/1")
    assert entry_primary_url(entry) == "https://example.com/via-guid/1"


def test_entry_primary_url_ignores_non_http_guid() -> None:
    entry = SimpleNamespace(
        link=None,
        links=[],
        id="tag:example.com,2026:story-9",
        guid="urn:uuid:1234",
    )
    assert entry_primary_url(entry) is None


def test_entry_primary_url_prefers_link_over_guid() -> None:
    entry = SimpleNamespace(
        link="https://example.com/article",
        links=[],
        id="https://example.com/via-guid",
    )
    assert entry_primary_url(entry) == "https://example.com/article"


def test_dedupe_feed_urls_collapses_canonical_variants() -> None:
    urls = [
        "https://example.com/a",
        "http://example.com/a",  # same identity after http→https
        "https://example.com/a?utm_source=rss",
        "https://example.com/b",
        "https://example.com/b/",
    ]
    assert dedupe_feed_urls(urls) == [
        "https://example.com/a",
        "https://example.com/b",
    ]


RSS_GUID_ONLY = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example</title>
    <item>
      <title>Guid story</title>
      <guid isPermaLink="true">https://example.com/guid-only/1</guid>
    </item>
    <item>
      <title>Dup scheme</title>
      <link>http://example.com/story/2</link>
    </item>
    <item>
      <title>Dup https</title>
      <link>https://example.com/story/2?utm_source=feed</link>
    </item>
  </channel>
</rss>
"""


@pytest.mark.asyncio
async def test_read_feed_guid_fallback_and_in_feed_dedupe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """guid permalinks are discovered; scheme/utm duplicates collapse once."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=RSS_GUID_ONLY)

    _patch_client_and_retries(monkeypatch, handler, module="awareness.sources.feeds")

    urls = await _read_feed("https://example.com/feed.xml", "TestBot/1.0")
    assert "https://example.com/guid-only/1" in urls
    # story/2 listed twice (http + https+utm) → one entry
    story2 = [u for u in urls if "story/2" in u]
    assert len(story2) == 1
    assert len(urls) == 2


SITEMAP_DUPES = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/page-a</loc></url>
  <url><loc>http://example.com/page-a</loc></url>
  <url><loc>https://example.com/page-b?utm_campaign=map</loc></url>
  <url><loc>https://example.com/page-b</loc></url>
</urlset>
"""


@pytest.mark.asyncio
async def test_read_sitemap_dedupes_canonical_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=SITEMAP_DUPES)

    _patch_client_and_retries(monkeypatch, handler, module="awareness.sources.feeds")
    urls = await _read_sitemap("https://example.com/sitemap.xml", "TestBot/1.0")
    assert len(urls) == 2
    assert urls[0] == "https://example.com/page-a"
    assert "page-b" in urls[1]

