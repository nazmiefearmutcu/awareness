"""text_min_chars honoring + lower floor for news/RSS extracts."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from awareness.normalize.html import html_to_text
from awareness.normalize.text import normalize_text
from awareness.schemas.doc import SourceKind
from awareness.sources.base import AdapterContext, PartitionSpec
from awareness.sources.tail_recrawl import (
    TailRecrawlAdapter,
    is_news_discovery,
    resolve_text_min_chars,
)
from awareness.util.urls import canonical_url


def _settings(*, text_min_chars: int = 200, text_min_chars_news: int = 80, text_max_chars: int = 1_500_000):
    return SimpleNamespace(
        text_min_chars=text_min_chars,
        text_min_chars_news=text_min_chars_news,
        text_max_chars=text_max_chars,
    )


# ── resolve_text_min_chars / is_news_discovery ──────────────────────────────


@pytest.mark.parametrize(
    "channel,kind,expected",
    [
        ("rss:https://example.com/feed", None, True),
        ("atom:https://example.com/atom", None, True),
        ("gdelt:20240608123000", None, True),
        ("sitemap:https://example.com/sitemap.xml", None, True),
        ("cc-wet:CC-MAIN-2024", None, False),
        ("tail", None, False),
        ("test", None, False),
        ("custom", "rss", True),
        ("custom", "atom", True),
        ("custom", "gdelt", True),
        ("custom", "sitemap", True),
        ("custom", "wet", False),
        (None, None, False),
    ],
)
def test_is_news_discovery(channel, kind, expected) -> None:
    assert is_news_discovery(channel, kind) is expected


def test_resolve_min_chars_default_bulk_keeps_200() -> None:
    s = _settings()
    assert resolve_text_min_chars(s, discovery_channel="tail") == 200
    assert resolve_text_min_chars(s, discovery_channel="cc-wet:x") == 200


def test_resolve_min_chars_news_uses_lower_floor() -> None:
    s = _settings(text_min_chars=200, text_min_chars_news=80)
    assert resolve_text_min_chars(s, discovery_channel="rss:https://ex/feed") == 80
    assert resolve_text_min_chars(s, discovery_channel="gdelt:slot") == 80
    assert resolve_text_min_chars(s, source_kind="atom") == 80
    assert resolve_text_min_chars(s, discovery_channel="x", source_kind="sitemap") == 80


def test_resolve_min_chars_news_respects_lower_global() -> None:
    # User lowered global floor below news default → news uses the global value
    # (still clamped to absolute floor of 40).
    s = _settings(text_min_chars=50, text_min_chars_news=80)
    assert resolve_text_min_chars(s, discovery_channel="rss:feed") == 50


def test_resolve_min_chars_news_absolute_floor_40() -> None:
    s = _settings(text_min_chars=10, text_min_chars_news=10)
    assert resolve_text_min_chars(s, discovery_channel="rss:feed") == 40


# ── normalize / html_to_text accept ~100-char news at news floor ────────────


def test_normalize_accepts_100_char_at_news_floor() -> None:
    body = "x" * 100
    out = normalize_text(body, min_chars=80)
    assert out.discarded_reason is None
    assert out.n_chars == 100


def test_normalize_rejects_100_char_at_bulk_floor() -> None:
    body = "x" * 100
    out = normalize_text(body, min_chars=200)
    assert out.discarded_reason is not None
    assert "too_short" in out.discarded_reason


def test_html_to_text_accepts_short_news_body_with_lower_min() -> None:
    # ~100 chars of real-looking article text; must pass with news floor 80.
    paragraph = (
        "Markets opened higher after the overnight futures rally lifted "
        "sentiment across major indices today."
    )
    assert 80 <= len(paragraph) <= 120
    html = f"""
    <html><head><title>Markets open higher</title></head>
    <body><article><h1>Markets open higher</h1><p>{paragraph}</p></article></body></html>
    """
    ext = html_to_text(html, url="https://news.example.com/markets", min_chars=80)
    assert ext is not None, "news-length body should extract with min_chars=80"
    assert len(ext.text.text) >= 80

    # Same HTML must fail the bulk 200 default.
    assert html_to_text(html, url="https://news.example.com/markets", min_chars=200) is None


# ── tail_recrawl threads settings + news floor into html_to_text ────────────


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


def _ok_response(url: str, body: str = "short news body") -> httpx.Response:
    request = httpx.Request("GET", url)
    return httpx.Response(
        200,
        text=f"<html><body><p>{body}</p></body></html>",
        headers={"Content-Type": "text/html; charset=utf-8"},
        request=request,
    )


@pytest.mark.asyncio
async def test_tail_recrawl_passes_news_min_chars_to_html_to_text(monkeypatch) -> None:
    """RSS-discovered URLs must call html_to_text with the news floor (80)."""
    from awareness.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "text_min_chars", 200)
    monkeypatch.setattr(settings, "text_min_chars_news", 80)
    monkeypatch.setattr(settings, "text_max_chars", 1_500_000)

    url = "https://news.example.com/short-story"
    seen: dict[str, Any] = {}

    def capture_html_to_text(html, *, url=None, min_chars=200, max_chars=1_500_000):
        seen["min_chars"] = min_chars
        seen["max_chars"] = max_chars
        # Return None so we don't need full capture plumbing; we only assert kwargs.
        return None

    get_mock = AsyncMock(side_effect=lambda client, u, **kw: _ok_response(u))
    partition = PartitionSpec(
        source_type=SourceKind.TAIL_RECRAWL,
        partition_key=f"tail:{canonical_url(url) or url}",
        payload={
            "url": url,
            "discovery_channel": "rss:https://news.example.com/feed.xml",
            "source_kind": "rss",
        },
    )

    with (
        patch("awareness.sources.tail_recrawl.is_public_http_url", return_value=True),
        patch("awareness.sources.tail_recrawl._get_public_url", get_mock),
        patch("awareness.sources.tail_recrawl.html_to_text", side_effect=capture_html_to_text),
    ):
        adapter = TailRecrawlAdapter()
        out = [c async for c in adapter.run_partition(partition, _context())]

    assert out == []
    assert seen["min_chars"] == 80
    assert seen["max_chars"] == 1_500_000


@pytest.mark.asyncio
async def test_tail_recrawl_bulk_uses_text_min_chars(monkeypatch) -> None:
    from awareness.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "text_min_chars", 350)
    monkeypatch.setattr(settings, "text_min_chars_news", 80)
    monkeypatch.setattr(settings, "text_max_chars", 999_999)

    url = "https://example.com/long-page"
    seen: dict[str, Any] = {}

    def capture_html_to_text(html, *, url=None, min_chars=200, max_chars=1_500_000):
        seen["min_chars"] = min_chars
        seen["max_chars"] = max_chars
        return None

    get_mock = AsyncMock(side_effect=lambda client, u, **kw: _ok_response(u))
    partition = PartitionSpec(
        source_type=SourceKind.TAIL_RECRAWL,
        partition_key=f"tail:{canonical_url(url) or url}",
        payload={"url": url, "discovery_channel": "manual"},
    )

    with (
        patch("awareness.sources.tail_recrawl.is_public_http_url", return_value=True),
        patch("awareness.sources.tail_recrawl._get_public_url", get_mock),
        patch("awareness.sources.tail_recrawl.html_to_text", side_effect=capture_html_to_text),
    ):
        adapter = TailRecrawlAdapter()
        _ = [c async for c in adapter.run_partition(partition, _context())]

    assert seen["min_chars"] == 350
    assert seen["max_chars"] == 999_999


@pytest.mark.asyncio
async def test_tail_recrawl_accepts_100_char_news_via_real_extract(monkeypatch) -> None:
    """End-to-end: RSS channel + real html_to_text keeps a ~100-char news body."""
    from awareness.config import get_settings
    from awareness.normalize.html import HtmlExtraction
    from awareness.normalize.text import NormalizedText

    settings = get_settings()
    monkeypatch.setattr(settings, "text_min_chars", 200)
    monkeypatch.setattr(settings, "text_min_chars_news", 80)
    monkeypatch.setattr(settings, "text_max_chars", 1_500_000)

    paragraph = (
        "Markets opened higher after the overnight futures rally lifted "
        "sentiment across major indices today."
    )
    assert 80 <= len(paragraph) < 200

    url = "https://news.example.com/markets-open"
    html = (
        f"<html><head><title>Markets open higher</title></head>"
        f"<body><article><h1>Markets open higher</h1><p>{paragraph}</p></article></body></html>"
    )

    # Bypass trafilatura variability: feed normalize_text path with known body
    # length by stubbing only the extraction library result through html_to_text
    # while still exercising resolve_text_min_chars + min_chars gating.
    def real_html_to_text(html_in, *, url=None, min_chars=200, max_chars=1_500_000):
        nt = normalize_text(paragraph, title="Markets open higher", min_chars=min_chars, max_chars=max_chars)
        if nt.discarded_reason:
            return None
        return HtmlExtraction(
            text=nt,
            title=nt.title,
            published_ts=None,
            canonical_url_hint=url,
            language_hint="en",
            raw_metadata={},
        )

    get_mock = AsyncMock(
        side_effect=lambda client, u, **kw: httpx.Response(
            200,
            text=html,
            headers={"Content-Type": "text/html"},
            request=httpx.Request("GET", u),
        )
    )
    partition = PartitionSpec(
        source_type=SourceKind.TAIL_RECRAWL,
        partition_key=f"tail:{canonical_url(url) or url}",
        payload={
            "url": url,
            "discovery_channel": "rss:https://news.example.com/feed",
            "source_kind": "rss",
        },
    )

    with (
        patch("awareness.sources.tail_recrawl.is_public_http_url", return_value=True),
        patch("awareness.sources.tail_recrawl._get_public_url", get_mock),
        patch("awareness.sources.tail_recrawl.html_to_text", side_effect=real_html_to_text),
    ):
        adapter = TailRecrawlAdapter()
        caps = [c async for c in adapter.run_partition(partition, _context())]

    assert len(caps) == 1
    assert len(caps[0].text) == len(paragraph)
