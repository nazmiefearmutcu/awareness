"""Robots.txt network fetch latency histogram + outcome counters."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from awareness.obs.metrics import MetricsRegistry
from awareness.util.robots import (
    RobotsCache,
    _record_robots_fetch,
    _status_class,
)


@pytest.fixture()
def metrics(monkeypatch: pytest.MonkeyPatch) -> MetricsRegistry:
    reg = MetricsRegistry()
    monkeypatch.setattr("awareness.util.robots.get_metrics", lambda: reg)
    return reg


def test_status_class() -> None:
    assert _status_class(200) == "2xx"
    assert _status_class(404) == "4xx"
    assert _status_class(503) == "5xx"
    assert _status_class(50) == "unknown"


def test_record_robots_fetch_emits_counter_and_hist(metrics: MetricsRegistry) -> None:
    _record_robots_fetch(outcome="ok", status_class="2xx", elapsed=0.08)
    assert metrics.counter_sum("robots.fetch_attempts") == 1.0
    assert (
        metrics.counter_value(
            "robots.fetch_attempts",
            labels={"outcome": "ok", "status_class": "2xx"},
        )
        == 1.0
    )
    snap = metrics.snapshot()
    hists = [h for h in snap["histograms"] if h["name"] == "robots.fetch_seconds"]
    assert hists and hists[0]["count"] == 1
    assert hists[0]["sum"] == pytest.approx(0.08, abs=1e-6)
    assert hists[0]["labels"].get("outcome") == "ok"


@pytest.mark.asyncio
async def test_load_ok_records_metrics(metrics: MetricsRegistry) -> None:
    cache = RobotsCache(state_db=None, ttl=60)
    with patch(
        "awareness.util.robots._get_public_robots_url", new_callable=AsyncMock
    ) as mock_fetch:
        mock_fetch.return_value = httpx.Response(
            200, text="User-agent: *\nAllow: /\n"
        )
        allowed = await cache.is_allowed("https://example.com/page", "TestBot")
        assert allowed is True

    assert metrics.counter_sum("robots.fetch_attempts") == 1.0
    assert (
        metrics.counter_value(
            "robots.fetch_attempts",
            labels={"outcome": "ok", "status_class": "2xx"},
        )
        == 1.0
    )
    snap = metrics.snapshot()
    hists = [h for h in snap["histograms"] if h["name"] == "robots.fetch_seconds"]
    assert len(hists) == 1
    assert hists[0]["count"] == 1
    assert hists[0]["sum"] >= 0.0

    # Memory cache hit must not record another network fetch.
    await cache.is_allowed("https://example.com/other", "TestBot")
    assert metrics.counter_sum("robots.fetch_attempts") == 1.0


@pytest.mark.asyncio
async def test_load_forbidden_records_metrics(metrics: MetricsRegistry) -> None:
    cache = RobotsCache(state_db=None, ttl=60)
    with patch(
        "awareness.util.robots._get_public_robots_url", new_callable=AsyncMock
    ) as mock_fetch:
        mock_fetch.return_value = httpx.Response(403)
        allowed = await cache.is_allowed("https://example.com/", "TestBot")
        assert allowed is False

    assert (
        metrics.counter_value(
            "robots.fetch_attempts",
            labels={"outcome": "forbidden", "status_class": "4xx"},
        )
        == 1.0
    )


@pytest.mark.asyncio
async def test_load_missing_records_metrics(metrics: MetricsRegistry) -> None:
    cache = RobotsCache(state_db=None, ttl=60)
    with patch(
        "awareness.util.robots._get_public_robots_url", new_callable=AsyncMock
    ) as mock_fetch:
        mock_fetch.return_value = httpx.Response(404)
        allowed = await cache.is_allowed("https://example.com/x", "TestBot")
        assert allowed is True

    assert (
        metrics.counter_value(
            "robots.fetch_attempts",
            labels={"outcome": "missing", "status_class": "4xx"},
        )
        == 1.0
    )


@pytest.mark.asyncio
async def test_load_transport_error_records_metrics(metrics: MetricsRegistry) -> None:
    cache = RobotsCache(state_db=None, ttl=60)
    with patch(
        "awareness.util.robots._get_public_robots_url", new_callable=AsyncMock
    ) as mock_fetch:
        mock_fetch.side_effect = httpx.ConnectError("boom")
        # Transport failure → permissive allow-all with short TTL.
        allowed = await cache.is_allowed("https://example.com/y", "TestBot")
        assert allowed is True

    assert (
        metrics.counter_value(
            "robots.fetch_attempts",
            labels={"outcome": "error", "status_class": "transport"},
        )
        == 1.0
    )


@pytest.mark.asyncio
async def test_load_blocked_non_public_records_metrics(metrics: MetricsRegistry) -> None:
    cache = RobotsCache(state_db=None, ttl=60)
    with patch(
        "awareness.util.robots._get_public_robots_url", new_callable=AsyncMock
    ) as mock_fetch:
        mock_fetch.return_value = None  # left public space / not fetchable
        await cache.is_allowed("https://example.com/z", "TestBot")

    assert (
        metrics.counter_value(
            "robots.fetch_attempts",
            labels={"outcome": "blocked", "status_class": "none"},
        )
        == 1.0
    )
