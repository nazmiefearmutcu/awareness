"""SSRF gates: seed write validation + per-hop public-URL checks in feeds."""

from __future__ import annotations

import socket

import httpx
import pytest

from awareness.config import reset_settings
from awareness.config.persist import write_tail_seeds
from awareness.sources import feeds
from awareness.util.http import (
    get_with_retries,
    reset_global_fetch_semaphore,
    reset_shared_async_clients,
)

RSS_OK = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Example</title>
  <item><title>Story</title><link>https://example.com/story/1</link></item>
</channel></rss>
"""

SITEMAP_INDEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>http://169.254.169.254/child.xml</loc></sitemap>
</sitemapindex>
"""


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("AW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("AW_CONFIG_FILE", raising=False)
    reset_settings()
    reset_global_fetch_semaphore()
    reset_shared_async_clients()
    yield
    reset_global_fetch_semaphore()
    reset_shared_async_clients()
    reset_settings()


def _patch_client(monkeypatch: pytest.MonkeyPatch, handler, *, module: str = "awareness.sources.feeds") -> None:
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def fake_shared(**kwargs):
        return mock_client

    monkeypatch.setattr(f"{module}.get_shared_async_client", fake_shared)

    real = get_with_retries

    async def fast_get(client, url, **kwargs):
        kwargs.setdefault("base_delay", 0.0)
        kwargs.setdefault("max_attempts", 5)
        return await real(client, url, **kwargs)

    monkeypatch.setattr(f"{module}.get_with_retries", fast_get)


# ── C-05: seed write-time validation ─────────────────────────────────────────
def test_write_tail_seeds_rejects_private_ip(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr("awareness.config.persist.tail_seeds_path", lambda: tmp_path / "seeds.yaml")
    with pytest.raises(ValueError, match=r"private|public"):
        write_tail_seeds({"feeds": ["http://169.254.169.254/latest/meta-data"]})
    with pytest.raises(ValueError, match=r"private|public"):
        write_tail_seeds({"feeds": ["http://10.0.0.1/feed"]})


def test_write_tail_seeds_rejects_localhost_and_userinfo(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr("awareness.config.persist.tail_seeds_path", lambda: tmp_path / "seeds.yaml")
    with pytest.raises(ValueError, match=r"private|public"):
        write_tail_seeds({"feeds": ["http://localhost:8080/feed"]})
    with pytest.raises(ValueError, match=r"userinfo"):
        write_tail_seeds({"feeds": ["https://user:pass@example.com/feed"]})


def test_write_tail_seeds_accepts_public(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr("awareness.config.persist.tail_seeds_path", lambda: tmp_path / "seeds.yaml")
    result = write_tail_seeds({"feeds": ["https://example.com/feed.xml"]})
    assert result["feeds"] == ["https://example.com/feed.xml"]


# ── C-05: fetch-time gate (hostnames resolve privately via mocked DNS) ───────
def test_is_public_fetch_url_blocks_private_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host, port, *args, **kwargs):
        assert host in ("internal.corp", "example.com")
        if host == "internal.corp":
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert feeds.is_public_fetch_url("http://internal.corp/feed") is False
    assert feeds.is_public_fetch_url("https://user:pass@example.com/feed") is False
    assert feeds.is_public_fetch_url("https://example.com/feed") is True


@pytest.mark.asyncio
async def test_read_feed_blocks_private_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, int] = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=RSS_OK)

    _patch_client(monkeypatch, handler)
    urls = await feeds._read_feed("http://169.254.169.254/latest/meta-data", "TestBot/1.0")
    assert urls == []
    assert calls["n"] == 0  # no request ever leaves the process


@pytest.mark.asyncio
async def test_read_feed_blocks_private_redirect_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, int] = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/steal"})
        return httpx.Response(200, content=RSS_OK)

    _patch_client(monkeypatch, handler)
    urls = await feeds._read_feed("https://example.com/feed.xml", "TestBot/1.0")
    assert urls == []
    assert calls["n"] == 1  # internal hop blocked before any second request


@pytest.mark.asyncio
async def test_read_feed_follows_public_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, int] = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(302, headers={"Location": "https://example.com/final.xml"})
        return httpx.Response(200, content=RSS_OK)

    _patch_client(monkeypatch, handler)
    urls = await feeds._read_feed("https://example.com/feed.xml", "TestBot/1.0")
    assert urls == ["https://example.com/story/1"]
    assert calls["n"] == 2


# ── M-09: sitemapindex child locs are validated before recursion ─────────────
@pytest.mark.asyncio
async def test_read_sitemap_blocks_private_child_loc(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, int] = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=SITEMAP_INDEX)

    _patch_client(monkeypatch, handler)
    urls = await feeds._read_sitemap("https://example.com/sitemap.xml", "TestBot/1.0")
    assert urls == []
    assert calls["n"] == 1  # child never fetched
