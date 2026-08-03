"""H-26 + M-11 + M-12 + L-03: ratelimit/robots/cache hardening regressions."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from awareness.jobsearch.cache import JobSearchCache
from awareness.jobsearch.profile_store import load_profile, save_profile
from awareness.jobsearch.models import JobProfile
from awareness.obs.metrics import MetricsRegistry
from awareness.util.ratelimit import PerDomainLimiter
from awareness.util.robots import RobotsCache, RobotsEntry


@pytest.fixture()
def metrics(monkeypatch: pytest.MonkeyPatch) -> MetricsRegistry:
    reg = MetricsRegistry()
    monkeypatch.setattr("awareness.util.robots.get_metrics", lambda: reg)
    return reg


@pytest.mark.asyncio
async def test_release_only_after_acquire_h26() -> None:
    """Cancellation while waiting must not over-release the semaphore (H-26)."""
    limiter = PerDomainLimiter(concurrency=1, min_delay_sec=0.0)
    slot = limiter._slot("example.com")
    sem = slot.sem

    # First holder acquires the only slot.
    ctx1 = limiter.domain("example.com")
    await ctx1.__aenter__()
    assert sem.locked()

    # Second acquirer is cancelled while waiting on the semaphore.
    ctx2 = limiter.domain("example.com")

    async def _try_acquire() -> None:
        await ctx2.__aenter__()

    task = asyncio.create_task(_try_acquire())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ctx2._acquired is False

    # The semaphore must still be held exactly once — no over-release.
    assert sem._value == 0  # type: ignore[attr-defined]

    # Normal release still works.
    await ctx1.__aexit__(None, None, None)
    assert sem._value == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_slots_bounded_lru_eviction() -> None:
    limiter = PerDomainLimiter(concurrency=1, min_delay_sec=0.0)
    for i in range(PerDomainLimiter.MAX_SLOTS + 50):
        limiter._slot(f"d{i}.example.com")
    assert len(limiter._slots) <= PerDomainLimiter.MAX_SLOTS


@pytest.mark.asyncio
async def test_robots_200_empty_labeled_empty_not_http_error(metrics) -> None:
    cache = RobotsCache(state_db=None, ttl=60)
    with patch("awareness.util.robots._get_public_robots_url", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = httpx.Response(200, content=b"")
        allowed = await cache.is_allowed("https://example.com/x", "TestBot")
        assert allowed is True
    assert (
        metrics.counter_value(
            "robots.fetch_attempts",
            labels={"outcome": "empty", "status_class": "2xx"},
        )
        == 1.0
    )
    assert (
        metrics.counter_value(
            "robots.fetch_attempts",
            labels={"outcome": "http_error", "status_class": "2xx"},
        )
        == 0.0
    )


@pytest.mark.asyncio
async def test_robots_entries_bounded() -> None:
    cache = RobotsCache(state_db=None, ttl=60)
    for i in range(RobotsCache.MAX_ENTRIES + 20):
        cache._remember(
            f"https://d{i}.example.com/",
            RobotsEntry(parser=None, expires_at=time.time() + 60, crawl_delay=None),
        )
    assert len(cache._entries) <= RobotsCache.MAX_ENTRIES


@pytest.mark.asyncio
async def test_robots_crawl_delay_expiry_not_stale() -> None:
    """L-09: an expired memory entry must not be served as a stale delay."""
    cache = RobotsCache(state_db=None, ttl=60)
    cache._entries["https://x.example.com"] = RobotsEntry(
        parser=None, expires_at=time.time() - 5, crawl_delay=9.0
    )
    assert cache.crawl_delay("https://x.example.com/page") is None
    assert await cache.crawl_delay_async("https://x.example.com/page") is None


def test_cache_atomic_write_and_startup_sweep(tmp_path: Path) -> None:
    cache = JobSearchCache(tmp_path)
    cache.set("linkedin_search", {"q": "python"}, "<html>ok</html>", 60)
    files = list(cache.root.glob("*.json"))
    assert len(files) == 1
    # No stray .tmp files.
    assert not list(cache.root.glob("*.tmp"))
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["value"] == "<html>ok</html>"

    # Startup sweep removes expired entries.
    stale = cache.root / "stale.json"
    stale.write_text(json.dumps({"expires_at": time.time() - 10, "value": "x"}), encoding="utf-8")
    JobSearchCache(tmp_path)  # re-open → sweep
    assert not stale.exists()


def test_profile_store_atomic_write(tmp_path: Path) -> None:
    profile = JobProfile(titles=["Backend Engineer"], skills=["python"])
    save_profile(tmp_path, profile)
    loaded = load_profile(tmp_path)
    assert loaded.titles == ["Backend Engineer"]
    assert not list((tmp_path).glob("*.tmp"))
