"""Tests for RobotsCache with persistent state DB backend."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import httpx

from awareness.obs.metrics import MetricsRegistry
from awareness.storage.state import StateDB
from awareness.util.robots import RobotsCache, RobotsEntry, extract_sitemap_urls


@pytest.mark.asyncio
async def test_robots_cache_in_memory_fallback():
    # If state_db is None, it should fall back to memory cache.
    cache = RobotsCache(state_db=None, ttl=60)
    
    with patch("awareness.util.robots._get_public_robots_url", new_callable=AsyncMock) as mock_fetch:
        resp = httpx.Response(200, text="User-agent: *\nDisallow: /secret\nCrawl-delay: 5")
        mock_fetch.return_value = resp
        
        # First check
        allowed = await cache.is_allowed("https://example.com/secret", "TestBot")
        assert not allowed
        allowed = await cache.is_allowed("https://example.com/public", "TestBot")
        assert allowed
        assert cache.crawl_delay("https://example.com/public") == 5.0
        
        assert mock_fetch.call_count == 1

        # Second check (should hit memory cache)
        allowed = await cache.is_allowed("https://example.com/secret", "TestBot")
        assert not allowed
        assert mock_fetch.call_count == 1


@pytest.mark.asyncio
async def test_robots_cache_db_persistence(tmp_path: Path):
    db_path = tmp_path / "state.db"
    db = StateDB(f"sqlite:///{db_path}")
    db.init()
    
    cache1 = RobotsCache(state_db=db, ttl=60)
    
    with patch("awareness.util.robots._get_public_robots_url", new_callable=AsyncMock) as mock_fetch:
        resp = httpx.Response(200, text="User-agent: *\nDisallow: /secret\nCrawl-delay: 10")
        mock_fetch.return_value = resp
        
        allowed = await cache1.is_allowed("https://example.com/secret", "TestBot")
        assert not allowed
        assert mock_fetch.call_count == 1
        
        # Verify it was saved to DB
        row = db.get_robots_cache("https://example.com")
        assert row is not None
        assert "Disallow: /secret" in row.robots_txt
        assert row.crawl_delay == 10.0
        
    # Re-instantiate cache with same DB (simulating restart or separate process)
    cache2 = RobotsCache(state_db=db, ttl=60)
    
    with patch("awareness.util.robots._get_public_robots_url", new_callable=AsyncMock) as mock_fetch:
        # Should load from DB and NOT make any network calls
        allowed = await cache2.is_allowed("https://example.com/secret", "TestBot")
        assert not allowed
        assert mock_fetch.call_count == 0
        assert cache2.crawl_delay("https://example.com/secret") == 10.0


@pytest.mark.asyncio
async def test_robots_cache_expiration(tmp_path: Path):
    db_path = tmp_path / "state.db"
    db = StateDB(f"sqlite:///{db_path}")
    db.init()
    
    # Cache entries expire instantly (ttl=0)
    cache = RobotsCache(state_db=db, ttl=0)
    
    with patch("awareness.util.robots._get_public_robots_url", new_callable=AsyncMock) as mock_fetch:
        resp1 = httpx.Response(200, text="User-agent: *\nDisallow: /secret")
        mock_fetch.return_value = resp1
        
        allowed = await cache.is_allowed("https://example.com/secret", "TestBot")
        assert not allowed
        assert mock_fetch.call_count == 1
        
    # Wait briefly or modify time to simulate expiration
    # Second check should query again because TTL was 0
    with patch("awareness.util.robots._get_public_robots_url", new_callable=AsyncMock) as mock_fetch:
        resp2 = httpx.Response(200, text="User-agent: *\nAllow: /secret")
        mock_fetch.return_value = resp2
        
        allowed = await cache.is_allowed("https://example.com/secret", "TestBot")
        assert allowed
        assert mock_fetch.call_count == 1


@pytest.mark.asyncio
async def test_robots_cache_error_handling(tmp_path: Path):
    db_path = tmp_path / "state.db"
    db = StateDB(f"sqlite:///{db_path}")
    db.init()
    
    cache = RobotsCache(state_db=db, ttl=60)
    
    with patch("awareness.util.robots._get_public_robots_url", new_callable=AsyncMock) as mock_fetch:
        # Simulate HTTP 403 Forbidden -> should disallow everything
        resp = httpx.Response(403)
        mock_fetch.return_value = resp
        
        allowed = await cache.is_allowed("https://example.com/public", "TestBot")
        assert not allowed
        assert mock_fetch.call_count == 1
        
        # Verify 403 behavior persists in DB
        row = db.get_robots_cache("https://example.com")
        assert row is not None
        assert "Disallow: /" in row.robots_txt


def test_extract_sitemap_urls_parses_directives_and_dedupes() -> None:
    body = """
User-agent: *
Disallow: /private

Sitemap: https://example.com/sitemap.xml
sitemap: https://example.com/news-sitemap.xml  # case-insensitive
Sitemap: https://example.com/sitemap.xml
# Sitemap: https://example.com/commented-out.xml
Sitemap:
Sitemap: https://cdn.example.com/index.xml
"""
    assert extract_sitemap_urls(body) == [
        "https://example.com/sitemap.xml",
        "https://example.com/news-sitemap.xml",
        "https://cdn.example.com/index.xml",
    ]


def test_extract_sitemap_urls_empty_and_none() -> None:
    assert extract_sitemap_urls(None) == []
    assert extract_sitemap_urls("") == []
    assert extract_sitemap_urls("User-agent: *\nDisallow: /\n") == []


@pytest.mark.asyncio
async def test_get_robots_txt_returns_body_with_sitemaps() -> None:
    cache = RobotsCache(state_db=None, ttl=60)
    body = "User-agent: *\nAllow: /\nSitemap: https://example.com/sitemap.xml\n"
    with patch("awareness.util.robots._get_public_robots_url", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = httpx.Response(200, text=body)
        got = await cache.get_robots_txt("https://example.com/", "TestBot")
        assert got is not None
        assert "Sitemap: https://example.com/sitemap.xml" in got
        assert extract_sitemap_urls(got) == ["https://example.com/sitemap.xml"]
        # Second call hits memory cache (no extra fetch).
        again = await cache.get_robots_txt("https://example.com/page", "TestBot")
        assert again == got
        assert mock_fetch.call_count == 1


@pytest.mark.asyncio
async def test_robots_cache_metrics_memory_db_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """robots.cache counters distinguish network fill, db restore, and memory hits."""
    # Isolate global metrics so other tests' counters don't interfere with deltas.
    isolated = MetricsRegistry()
    monkeypatch.setattr("awareness.util.robots.get_metrics", lambda: isolated)
    monkeypatch.setattr("awareness.obs.metrics._REGISTRY", isolated)

    db_path = tmp_path / "state.db"
    db = StateDB(f"sqlite:///{db_path}")
    db.init()

    cache1 = RobotsCache(state_db=db, ttl=3600)
    with patch("awareness.util.robots._get_public_robots_url", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = httpx.Response(
            200, text="User-agent: *\nDisallow: /secret\n"
        )
        await cache1.is_allowed("https://metrics.example/public", "TestBot")
        await cache1.is_allowed("https://metrics.example/other", "TestBot")
        assert mock_fetch.call_count == 1

    layers = {
        c["labels"]["layer"]: c["value"]
        for c in isolated.snapshot()["counters"]
        if c["name"] == "robots.cache"
    }
    assert layers.get("network", 0) == 1.0
    assert layers.get("memory", 0) == 1.0  # second call after fill

    # Fresh cache instance: should hit StateDB, not network.
    cache2 = RobotsCache(state_db=db, ttl=3600)
    with patch("awareness.util.robots._get_public_robots_url", new_callable=AsyncMock) as mock_fetch:
        await cache2.is_allowed("https://metrics.example/secret", "TestBot")
        assert mock_fetch.call_count == 0

    layers = {
        c["labels"]["layer"]: c["value"]
        for c in isolated.snapshot()["counters"]
        if c["name"] == "robots.cache"
    }
    assert layers.get("db", 0) == 1.0
    assert layers.get("network", 0) == 1.0  # unchanged
    # One more memory hit after db hydrate.
    await cache2.is_allowed("https://metrics.example/public", "TestBot")
    layers = {
        c["labels"]["layer"]: c["value"]
        for c in isolated.snapshot()["counters"]
        if c["name"] == "robots.cache"
    }
    assert layers.get("memory", 0) >= 2.0


def test_robots_cache_hit_ratio_gauges(monkeypatch: pytest.MonkeyPatch) -> None:
    """snapshot() derives hit_ratio gauges from robots.cache layer counters."""
    isolated = MetricsRegistry()
    monkeypatch.setattr("awareness.obs.metrics._REGISTRY", isolated)

    # 3 memory + 1 db + 1 network → hit_ratio = 0.8
    for _ in range(3):
        isolated.inc("robots.cache", labels={"layer": "memory"})
    isolated.inc("robots.cache", labels={"layer": "db"})
    isolated.inc("robots.cache", labels={"layer": "network"})

    gauges = {g["name"]: g["value"] for g in isolated.snapshot()["gauges"]}
    assert gauges["robots.cache.resolutions"] == 5.0
    assert abs(gauges["robots.cache.hit_ratio"] - 0.8) < 1e-9
    assert abs(gauges["robots.cache.memory_ratio"] - 0.6) < 1e-9
    assert abs(gauges["robots.cache.db_ratio"] - 0.2) < 1e-9
    assert abs(gauges["robots.cache.network_ratio"] - 0.2) < 1e-9


def test_robots_cache_hit_ratio_zero_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    isolated = MetricsRegistry()
    monkeypatch.setattr("awareness.obs.metrics._REGISTRY", isolated)
    gauges = {g["name"]: g["value"] for g in isolated.snapshot()["gauges"]}
    assert gauges["robots.cache.hit_ratio"] == 0.0
    assert gauges["robots.cache.resolutions"] == 0.0
