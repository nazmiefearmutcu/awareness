"""L-07/L-06 regression: redirect hops obey per-domain robots + limiter."""

from __future__ import annotations

import httpx
import pytest

from awareness.obs.metrics import MetricsRegistry
from awareness.schemas.doc import SourceKind
from awareness.sources.base import AdapterContext, PartitionSpec
from awareness.sources.tail_recrawl import TailRecrawlAdapter, _get_public_url


class _HopClient:
    """Returns a redirect to a NEW domain, then a 200 with an empty body."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    async def get(self, url: str, **kwargs) -> httpx.Response:
        self.urls.append(url)
        request = httpx.Request("GET", url)
        if url == "https://news.example.com/story":
            return httpx.Response(
                302,
                headers={"Location": "https://cdn.example.com/asset"},
                request=request,
            )
        return httpx.Response(200, content=b"", request=request)


class _RecordingRobots:
    def __init__(self, disallowed: set[str] | None = None) -> None:
        self._disallowed = disallowed or set()
        self.checks: list[str] = []

    async def is_allowed(self, url: str, ua: str) -> bool:
        self.checks.append(url)
        return url not in self._disallowed

    def crawl_delay(self, url: str) -> float | None:
        return None


class _RecordingLimiter:
    def __init__(self) -> None:
        self.acquired: list[str] = []

    def domain(self, dom: str, override_delay: float | None = None):
        self.acquired.append(dom)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_redirect_to_new_domain_checks_robots_and_limiter(monkeypatch) -> None:
    """L-07: a hop onto a NEW domain is robots-checked and limiter-slotted."""
    monkeypatch.setattr(
        "awareness.sources.tail_recrawl.is_public_http_url", lambda url: True
    )
    client = _HopClient()
    robots = _RecordingRobots()
    limiter = _RecordingLimiter()

    resp = await _get_public_url(
        client,
        "https://news.example.com/story",
        headers={"User-Agent": "TestBot/1.0"},
        user_agent="TestBot/1.0",
        robots=robots,
        limiter=limiter,
    )

    assert resp is not None
    assert resp.status_code == 200
    # Both hops fetched; the new domain was robots-checked before its GET.
    assert client.urls == [
        "https://news.example.com/story",
        "https://cdn.example.com/asset",
    ]
    assert "https://cdn.example.com/asset" in robots.checks
    assert "cdn.example.com" in limiter.acquired
    # The first hop is the caller's domain — not re-checked inside the helper.
    assert "news.example.com" not in limiter.acquired


@pytest.mark.asyncio
async def test_redirect_to_disallowed_new_domain_is_blocked(monkeypatch) -> None:
    """L-07: redirect target whose robots.txt disallows → return None."""
    monkeypatch.setattr(
        "awareness.sources.tail_recrawl.is_public_http_url", lambda url: True
    )
    client = _HopClient()
    robots = _RecordingRobots(disallowed={"https://cdn.example.com/asset"})
    limiter = _RecordingLimiter()

    resp = await _get_public_url(
        client,
        "https://news.example.com/story",
        headers={"User-Agent": "TestBot/1.0"},
        user_agent="TestBot/1.0",
        robots=robots,
        limiter=limiter,
    )

    assert resp is None
    # The disallowed hop must never be fetched.
    assert client.urls == ["https://news.example.com/story"]
    assert "cdn.example.com" not in limiter.acquired


@pytest.mark.asyncio
async def test_empty_200_classified_empty_outcome(monkeypatch) -> None:
    """L-06: a 200 with an empty body is 'empty', not 'non_200'."""
    reg = MetricsRegistry()
    monkeypatch.setattr("awareness.sources.tail_recrawl.get_metrics", lambda: reg)

    url = "https://news.example.com/empty"

    async def _fake_get(client, u, **kw):
        request = httpx.Request("GET", u)
        return httpx.Response(200, content=b"", request=request)

    monkeypatch.setattr("awareness.sources.tail_recrawl.is_public_http_url", lambda u: True)
    monkeypatch.setattr("awareness.sources.tail_recrawl._get_public_url", _fake_get)

    ctx = AdapterContext(
        user_agent="TestBot/1.0",
        job_id="job-1",
        task_id="task-1",
        batch_id="b1",
        ingest_version="0",
        checkpoint={},
        is_stopping=lambda: False,
        extras={
            "limiter": _RecordingLimiter(),
            "robots": _RecordingRobots(),
        },
    )
    partition = PartitionSpec(
        source_type=SourceKind.TAIL_RECRAWL,
        partition_key=f"tail:{url}",
        payload={"url": url, "discovery_channel": "rss"},
    )
    adapter = TailRecrawlAdapter()
    out = [c async for c in adapter.run_partition(partition, ctx)]
    assert out == []
    assert (
        reg.counter_value(
            "tail.fetch_attempts", labels={"outcome": "empty", "domain": "example.com"}
        )
        == 1.0
    )
    assert reg.counter_sum("tail.fetch_non_200") == 0.0
