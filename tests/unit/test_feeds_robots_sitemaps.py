"""Homepage seed → robots Sitemap: discovery (C3-T6 partial)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from awareness.schemas.doc import SourceKind
from awareness.sources.base import AdapterContext, PartitionSpec
from awareness.sources.feeds import FeedsAdapter


def _context(*, robots: Any = None, checkpoint: dict | None = None) -> AdapterContext:
    return AdapterContext(
        user_agent="TestBot/1.0",
        job_id="job-1",
        task_id="task-1",
        batch_id="batch-1",
        ingest_version="v1",
        checkpoint=checkpoint if checkpoint is not None else {},
        is_stopping=lambda: False,
        extras={"robots": robots, "enqueue": []},
    )


@pytest.mark.asyncio
async def test_homepage_seed_enqueues_public_sitemaps_once(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = FeedsAdapter()
    robots_body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Sitemap: https://example.com/sitemap.xml\n"
        "Sitemap: https://example.com/news.xml\n"
        "Sitemap: http://127.0.0.1/private-sitemap.xml\n"
    )
    robots = MagicMock()
    robots.get_robots_txt = AsyncMock(return_value=robots_body)

    # Avoid real network for the homepage HTML / feed fetch.
    async def _no_feed(url: str, user_agent: str) -> list[str]:
        return []

    monkeypatch.setattr("awareness.sources.feeds._read_feed", _no_feed)

    def _public(url: str | None) -> bool:
        if not url:
            return False
        # Reject loopback / private; allow example.com sitemaps without DNS.
        if "127.0.0.1" in url or "localhost" in url:
            return False
        return url.startswith(("http://", "https://"))

    monkeypatch.setattr("awareness.sources.feeds.is_public_http_url", _public)

    ctx = _context(robots=robots)
    partition = PartitionSpec(
        source_type=SourceKind.RSS,
        partition_key="rss:https://example.com/",
        payload={"kind": "rss", "url": "https://example.com/"},
    )
    # Drain async generator.
    async for _ in adapter.run_partition(partition, ctx):
        pass

    enqueue = ctx.extras["enqueue"]
    sm = [p for p in enqueue if p.payload.get("kind") == "sitemap"]
    assert [p.payload["url"] for p in sm] == [
        "https://example.com/sitemap.xml",
        "https://example.com/news.xml",
    ]
    assert all(p.partition_key.startswith("sitemap:") for p in sm)
    assert ctx.checkpoint.get("robots_sitemaps_discovered") is True
    robots.get_robots_txt.assert_awaited_once()

    # Second run with checkpoint flag set must not re-discover.
    ctx2 = _context(
        robots=robots,
        checkpoint={"robots_sitemaps_discovered": True, "seen_urls": []},
    )
    async for _ in adapter.run_partition(partition, ctx2):
        pass
    sm2 = [p for p in ctx2.extras["enqueue"] if p.payload.get("kind") == "sitemap"]
    assert sm2 == []
    assert robots.get_robots_txt.await_count == 1


@pytest.mark.asyncio
async def test_non_homepage_seed_skips_robots_sitemap_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FeedsAdapter()
    robots = MagicMock()
    robots.get_robots_txt = AsyncMock(return_value="Sitemap: https://example.com/s.xml\n")

    async def _no_feed(url: str, user_agent: str) -> list[str]:
        return []

    monkeypatch.setattr("awareness.sources.feeds._read_feed", _no_feed)

    ctx = _context(robots=robots)
    partition = PartitionSpec(
        source_type=SourceKind.RSS,
        partition_key="rss:https://example.com/feed.xml",
        payload={"kind": "rss", "url": "https://example.com/feed.xml"},
    )
    async for _ in adapter.run_partition(partition, ctx):
        pass

    robots.get_robots_txt.assert_not_called()
    assert "robots_sitemaps_discovered" not in ctx.checkpoint
    assert [p for p in ctx.extras["enqueue"] if p.payload.get("kind") == "sitemap"] == []
