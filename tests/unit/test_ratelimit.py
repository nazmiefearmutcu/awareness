"""PerDomainLimiter: crawl-delay / min-delay spacing under concurrency."""

from __future__ import annotations

import asyncio
import time

import pytest

from awareness.util.ratelimit import PerDomainLimiter


@pytest.mark.asyncio
async def test_crawl_delay_spaces_starts_under_concurrency() -> None:
    """Concurrent holders must not race past crawl-delay (audit: limiter race).

    With concurrency>1 the old last_release-on-release design let two coroutines
    acquire, both see wait<=0, and fire back-to-back. Starts must be spaced by
    at least the robots crawl-delay.
    """
    delay = 0.08
    limiter = PerDomainLimiter(concurrency=2, min_delay_sec=0.0)
    starts: list[float] = []

    async def one() -> None:
        async with limiter.domain("example.com", override_delay=delay):
            starts.append(time.monotonic())
            # Hold briefly so both concurrency slots are contended.
            await asyncio.sleep(0.01)

    await asyncio.gather(one(), one(), one())
    starts.sort()
    assert len(starts) == 3
    gaps = [starts[i + 1] - starts[i] for i in range(len(starts) - 1)]
    # Allow a little scheduler jitter but require crawl-delay spacing.
    for gap in gaps:
        assert gap >= delay * 0.85, f"starts too close: gaps={gaps}"


@pytest.mark.asyncio
async def test_override_delay_never_below_min_delay() -> None:
    """Robots crawl-delay must not undercut the configured min floor."""
    limiter = PerDomainLimiter(concurrency=2, min_delay_sec=0.06)
    assert limiter._effective_delay(None) == 0.06
    assert limiter._effective_delay(0.01) == 0.06  # crawl-delay lower than floor
    assert limiter._effective_delay(0.2) == 0.2  # crawl-delay raises floor
    assert limiter._effective_delay(-1.0) == 0.06

    starts: list[float] = []

    async def one() -> None:
        async with limiter.domain("example.com", override_delay=0.01):
            starts.append(time.monotonic())

    await asyncio.gather(one(), one())
    starts.sort()
    assert starts[1] - starts[0] >= 0.06 * 0.85


@pytest.mark.asyncio
async def test_zero_delay_allows_parallel_starts() -> None:
    """With min_delay=0 and no crawl-delay, concurrent slots may start together."""
    limiter = PerDomainLimiter(concurrency=2, min_delay_sec=0.0)
    starts: list[float] = []

    async def one() -> None:
        async with limiter.domain("example.com"):
            starts.append(time.monotonic())
            await asyncio.sleep(0.05)

    t0 = time.monotonic()
    await asyncio.gather(one(), one())
    starts.sort()
    # Both should start near-instantly (well under a crawl-delay-like gap).
    assert starts[1] - starts[0] < 0.04
    assert time.monotonic() - t0 < 0.2


@pytest.mark.asyncio
async def test_domains_are_independent() -> None:
    limiter = PerDomainLimiter(concurrency=1, min_delay_sec=0.1)
    starts: dict[str, float] = {}

    async def one(dom: str) -> None:
        async with limiter.domain(dom, override_delay=0.1):
            starts[dom] = time.monotonic()

    t0 = time.monotonic()
    await asyncio.gather(one("a.com"), one("b.com"))
    # Independent domains must not serialize on each other's crawl-delay.
    assert abs(starts["a.com"] - starts["b.com"]) < 0.05
    assert time.monotonic() - t0 < 0.15
