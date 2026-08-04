"""Tail recrawl HTTP fetch latency histogram + attempt outcome counters."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from awareness.normalize.html import HtmlExtraction
from awareness.normalize.text import NormalizedText
from awareness.obs.metrics import MetricsRegistry
from awareness.schemas.doc import SourceKind
from awareness.sources.base import AdapterContext, PartitionSpec
from awareness.sources.tail_recrawl import TailRecrawlAdapter
from awareness.util.http import RetryableHTTPError
from awareness.util.urls import canonical_url


class _FakeLimiter:
    def domain(self, dom: str, override_delay: float | None = None) -> _FakeLimiter:
        return self

    async def __aenter__(self) -> _FakeLimiter:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _FakeRobots:
    async def is_allowed(self, url: str, ua: str) -> bool:
        return True

    def crawl_delay(self, url: str) -> float | None:
        return None


def _context() -> AdapterContext:
    return AdapterContext(
        user_agent="test-ua",
        job_id="job-1",
        task_id="task-1",
        batch_id="b1",
        ingest_version="0",
        checkpoint={},
        is_stopping=lambda: False,
        extras={"limiter": _FakeLimiter(), "robots": _FakeRobots()},
    )


def _partition(url: str, **payload_extra: Any) -> PartitionSpec:
    payload = {"url": url, "discovery_channel": "test"}
    payload.update(payload_extra)
    return PartitionSpec(
        source_type=SourceKind.TAIL_RECRAWL,
        partition_key=f"tail:{canonical_url(url) or url}",
        payload=payload,
    )


def _fake_extraction(url: str) -> HtmlExtraction:
    body = "word " * 80
    return HtmlExtraction(
        text=NormalizedText(text=body, n_chars=len(body), n_words=80, n_lines=1),
        title="Example Story",
        published_ts=None,
        canonical_url_hint=url,
        language_hint="en",
        raw_metadata={},
    )


def _ok_response(url: str) -> httpx.Response:
    request = httpx.Request("GET", url)
    return httpx.Response(
        200,
        text="<html><body><p>" + ("news " * 100) + "</p></body></html>",
        headers={"Content-Type": "text/html; charset=utf-8"},
        request=request,
    )


def _err_response(url: str, status: int = 404) -> httpx.Response:
    request = httpx.Request("GET", url)
    return httpx.Response(status, text="missing", request=request)


@pytest.fixture()
def metrics(monkeypatch: pytest.MonkeyPatch) -> MetricsRegistry:
    reg = MetricsRegistry()
    monkeypatch.setattr("awareness.sources.tail_recrawl.get_metrics", lambda: reg)
    return reg


async def _run(
    *,
    get_mock: AsyncMock,
    url: str = "https://news.example.com/story/1",
) -> list[Any]:
    adapter = TailRecrawlAdapter()
    with (
        patch("awareness.sources.tail_recrawl.is_public_http_url", return_value=True),
        patch("awareness.sources.tail_recrawl._get_public_url", get_mock),
        patch(
            "awareness.sources.tail_recrawl.html_to_text",
            side_effect=lambda html, url=None, min_chars=200, max_chars=1_500_000: (
                _fake_extraction(url or "https://news.example.com/story/1")
            ),
        ),
    ):
        out: list[Any] = []
        async for cap in adapter.run_partition(_partition(url), _context()):
            out.append(cap)
        return out


@pytest.mark.asyncio
async def test_ok_fetch_records_attempt_and_duration(metrics: MetricsRegistry) -> None:
    url = "https://news.example.com/story/1"
    get_mock = AsyncMock(side_effect=lambda client, u, **kw: _ok_response(u))
    out = await _run(get_mock=get_mock, url=url)
    assert len(out) == 1
    assert (
        metrics.counter_value(
            "tail.fetch_attempts",
            labels={"outcome": "ok", "domain": "tail"},
        )
        == 1.0
    )
    snap = metrics.snapshot()
    hists = [
        h
        for h in snap["histograms"]
        if h["name"] == "tail.fetch_seconds"
        and (h.get("labels") or {}).get("outcome") == "ok"
    ]
    assert hists and sum(h["count"] for h in hists) >= 1
    assert metrics.counter_value(
        "tail.fetches", labels={"domain": "tail"}
    ) == 1.0


@pytest.mark.asyncio
async def test_non_200_fetch_records_outcome(metrics: MetricsRegistry) -> None:
    url = "https://news.example.com/missing"
    get_mock = AsyncMock(side_effect=lambda client, u, **kw: _err_response(u, 404))
    out = await _run(get_mock=get_mock, url=url)
    assert out == []
    assert (
        metrics.counter_value(
            "tail.fetch_attempts",
            labels={"outcome": "non_200", "domain": "tail"},
        )
        == 1.0
    )
    assert metrics.counter_sum("tail.fetch_non_200") == 1.0
    snap = metrics.snapshot()
    hists = [
        h
        for h in snap["histograms"]
        if h["name"] == "tail.fetch_seconds"
        and (h.get("labels") or {}).get("outcome") == "non_200"
    ]
    assert hists and sum(h["count"] for h in hists) >= 1


@pytest.mark.asyncio
async def test_retryable_error_records_duration_then_raises(
    metrics: MetricsRegistry,
) -> None:
    url = "https://news.example.com/down"
    get_mock = AsyncMock(
        side_effect=RetryableHTTPError(f"{url} -> 503 after 3 attempts")
    )
    with pytest.raises(RetryableHTTPError):
        await _run(get_mock=get_mock, url=url)
    assert (
        metrics.counter_value(
            "tail.fetch_attempts",
            labels={"outcome": "retryable_error", "domain": "tail"},
        )
        == 1.0
    )
    assert metrics.counter_sum("tail.retryable_http_error") == 1.0
    snap = metrics.snapshot()
    hists = [
        h
        for h in snap["histograms"]
        if h["name"] == "tail.fetch_seconds"
        and (h.get("labels") or {}).get("outcome") == "retryable_error"
    ]
    assert hists and sum(h["count"] for h in hists) >= 1


@pytest.mark.asyncio
async def test_network_error_records_outcome(metrics: MetricsRegistry) -> None:
    url = "https://news.example.com/boom"
    get_mock = AsyncMock(side_effect=httpx.ConnectError("boom"))
    out = await _run(get_mock=get_mock, url=url)
    assert out == []
    assert (
        metrics.counter_value(
            "tail.fetch_attempts",
            labels={"outcome": "network_error", "domain": "tail"},
        )
        == 1.0
    )
    assert metrics.counter_sum("tail.fetch_errors") == 1.0
