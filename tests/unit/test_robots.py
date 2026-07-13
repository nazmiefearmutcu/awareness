"""Tests for RobotsCache with persistent state DB backend."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import httpx

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
