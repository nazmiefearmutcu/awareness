"""Feed/sitemap fetch latency histogram + outcome counters."""

from __future__ import annotations

import httpx
import pytest

from awareness.obs.metrics import MetricsRegistry
from awareness.sources.feeds import (
    _read_feed,
    _read_sitemap,
    _record_feed_fetch,
    _sitemap_probe_depth_label,
    _status_class,
)
from awareness.util.http import RetryableHTTPError, get_with_retries, reset_global_fetch_semaphore, reset_shared_async_clients


@pytest.fixture(autouse=True)
def _reset_fetch_sem() -> None:
    reset_global_fetch_semaphore()
    reset_shared_async_clients()
    yield
    reset_global_fetch_semaphore()
    reset_shared_async_clients()


@pytest.fixture()
def metrics(monkeypatch: pytest.MonkeyPatch) -> MetricsRegistry:
    reg = MetricsRegistry()
    monkeypatch.setattr("awareness.sources.feeds.get_metrics", lambda: reg)
    return reg


def _patch_client(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def fake_shared(**kwargs):
        return mock_client

    monkeypatch.setattr("awareness.sources.feeds.get_shared_async_client", fake_shared)

    real = get_with_retries

    async def fast_get(client, url, **kwargs):
        kwargs.setdefault("base_delay", 0.0)
        kwargs.setdefault("max_attempts", 5)
        return await real(client, url, **kwargs)

    monkeypatch.setattr("awareness.sources.feeds.get_with_retries", fast_get)


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
</urlset>
"""


def test_status_class() -> None:
    assert _status_class(200) == "2xx"
    assert _status_class(404) == "4xx"
    assert _status_class(503) == "5xx"
    assert _status_class(50) == "unknown"


def test_sitemap_probe_depth_label() -> None:
    assert _sitemap_probe_depth_label(1) == "root"
    assert _sitemap_probe_depth_label(2) == "root"
    assert _sitemap_probe_depth_label(0) == "nested"
    assert _sitemap_probe_depth_label(-1) == "nested"


def test_record_feed_fetch_emits_counter_and_hist(metrics: MetricsRegistry) -> None:
    _record_feed_fetch(kind="rss", outcome="ok", status_class="2xx", elapsed=0.12)
    assert metrics.counter_sum("feeds.fetch_attempts") == 1.0
    assert (
        metrics.counter_value(
            "feeds.fetch_attempts",
            labels={"kind": "rss", "outcome": "ok", "status_class": "2xx"},
        )
        == 1.0
    )
    snap = metrics.snapshot()
    hists = [h for h in snap["histograms"] if h["name"] == "feeds.fetch_seconds"]
    assert hists and hists[0]["count"] == 1
    assert hists[0]["sum"] == pytest.approx(0.12, abs=1e-6)


def test_record_sitemap_fetch_includes_depth_label(metrics: MetricsRegistry) -> None:
    _record_feed_fetch(
        kind="sitemap",
        outcome="ok",
        status_class="2xx",
        elapsed=0.05,
        depth=1,
    )
    _record_feed_fetch(
        kind="sitemap",
        outcome="ok",
        status_class="2xx",
        elapsed=0.2,
        depth=0,
    )
    assert (
        metrics.counter_value(
            "feeds.fetch_attempts",
            labels={
                "kind": "sitemap",
                "outcome": "ok",
                "status_class": "2xx",
                "depth": "root",
            },
        )
        == 1.0
    )
    assert (
        metrics.counter_value(
            "feeds.fetch_attempts",
            labels={
                "kind": "sitemap",
                "outcome": "ok",
                "status_class": "2xx",
                "depth": "nested",
            },
        )
        == 1.0
    )
    # RSS still omits depth (no label key explosion for non-sitemaps).
    _record_feed_fetch(kind="rss", outcome="ok", status_class="2xx", elapsed=0.01)
    assert (
        metrics.counter_value(
            "feeds.fetch_attempts",
            labels={"kind": "rss", "outcome": "ok", "status_class": "2xx"},
        )
        == 1.0
    )


@pytest.mark.asyncio
async def test_read_feed_ok_records_metrics(
    metrics: MetricsRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=RSS_OK)

    _patch_client(monkeypatch, handler)
    urls = await _read_feed("https://example.com/feed.xml", "TestBot/1.0")
    assert urls == ["https://example.com/story/1"]
    assert metrics.counter_sum("feeds.fetch_attempts") == 1.0
    assert (
        metrics.counter_value(
            "feeds.fetch_attempts",
            labels={"kind": "rss", "outcome": "ok", "status_class": "2xx"},
        )
        == 1.0
    )
    snap = metrics.snapshot()
    hists = [h for h in snap["histograms"] if h["name"] == "feeds.fetch_seconds"]
    assert hists and hists[0]["count"] >= 1


@pytest.mark.asyncio
async def test_read_feed_404_records_http_error(
    metrics: MetricsRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    _patch_client(monkeypatch, handler)
    urls = await _read_feed("https://example.com/missing.xml", "TestBot/1.0")
    assert urls == []
    assert (
        metrics.counter_value(
            "feeds.fetch_attempts",
            labels={"kind": "rss", "outcome": "http_error", "status_class": "4xx"},
        )
        == 1.0
    )
    assert metrics.counter_sum("feeds.fetch_non_200") == 1.0


@pytest.mark.asyncio
async def test_read_feed_retry_exhausted_records_outcome(
    metrics: MetricsRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def fake_shared(**kwargs):
        return mock_client

    monkeypatch.setattr("awareness.sources.feeds.get_shared_async_client", fake_shared)

    real = get_with_retries

    async def fast_get(client, url, **kwargs):
        kwargs.setdefault("base_delay", 0.0)
        kwargs.setdefault("max_attempts", 2)
        return await real(client, url, **kwargs)

    monkeypatch.setattr("awareness.sources.feeds.get_with_retries", fast_get)

    with pytest.raises(RetryableHTTPError):
        await _read_feed("https://example.com/feed.xml", "TestBot/1.0")

    assert (
        metrics.counter_value(
            "feeds.fetch_attempts",
            labels={"kind": "rss", "outcome": "retry_exhausted", "status_class": "5xx"},
        )
        == 1.0
    )
    assert metrics.counter_sum("feeds.retryable_http_error") == 1.0
    snap = metrics.snapshot()
    hists = [h for h in snap["histograms"] if h["name"] == "feeds.fetch_seconds"]
    assert hists and hists[0]["count"] >= 1


SITEMAP_INDEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/sitemap-child.xml</loc></sitemap>
</sitemapindex>
"""


@pytest.mark.asyncio
async def test_read_sitemap_ok_records_metrics(
    metrics: MetricsRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=SITEMAP_OK)

    _patch_client(monkeypatch, handler)
    urls = await _read_sitemap("https://example.com/sitemap.xml", "TestBot/1.0")
    assert urls == ["https://example.com/page-a"]
    assert (
        metrics.counter_value(
            "feeds.fetch_attempts",
            labels={
                "kind": "sitemap",
                "outcome": "ok",
                "status_class": "2xx",
                "depth": "root",
            },
        )
        == 1.0
    )


@pytest.mark.asyncio
async def test_read_sitemap_index_labels_root_and_nested(
    metrics: MetricsRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("sitemap-index.xml"):
            return httpx.Response(200, content=SITEMAP_INDEX)
        return httpx.Response(200, content=SITEMAP_OK)

    _patch_client(monkeypatch, handler)
    urls = await _read_sitemap(
        "https://example.com/sitemap-index.xml", "TestBot/1.0", depth=1
    )
    assert urls == ["https://example.com/page-a"]
    assert (
        metrics.counter_value(
            "feeds.fetch_attempts",
            labels={
                "kind": "sitemap",
                "outcome": "ok",
                "status_class": "2xx",
                "depth": "root",
            },
        )
        == 1.0
    )
    assert (
        metrics.counter_value(
            "feeds.fetch_attempts",
            labels={
                "kind": "sitemap",
                "outcome": "ok",
                "status_class": "2xx",
                "depth": "nested",
            },
        )
        == 1.0
    )
    assert metrics.counter_sum("feeds.fetch_attempts") == 2.0
