"""Article extraction and robots gating. Tests never touch the network."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx

from awareness.markets.config import MarketsSettings
from awareness.markets.http import Fetcher
from awareness.markets.news.extract import (
    MIN_WORDS,
    RobotsChecker,
    extract_from_html,
    fetch_and_extract,
)

ARTICLE_URL = "https://www.example-finance.test/markets/rates-hold-2026-11-07.html"
UA = "AwarenessMarkets/0.3 (+https://github.com/nazmiefearmutcu/awareness; market research)"


async def _no_sleep(_seconds: float) -> None:
    return None


def _fetcher(settings: MarketsSettings, handler: Callable[[httpx.Request], httpx.Response]) -> Fetcher:
    return Fetcher(settings, None, transport=httpx.MockTransport(handler), sleep=_no_sleep)


def test_extracts_body_author_and_date_from_article(fixture_path: Callable[[str], Path]) -> None:
    html = fixture_path("article_sample.html").read_text(encoding="utf-8")
    article = extract_from_html(html, url=ARTICLE_URL)
    assert article is not None
    assert article.word_count >= 150
    assert "benchmark interest rate unchanged" in article.text
    # navigation, ads and footer boilerplate are not part of the body
    assert "Advertisement" not in article.text
    assert "Copyright 2026" not in article.text
    assert article.author is not None and "Jane Analyst" in article.author
    assert article.published_at == datetime(2026, 11, 7, 14, 32, tzinfo=UTC)
    assert article.title is not None and "Central bank holds rates" in article.title


def test_short_page_yields_none(fixture_path: Callable[[str], Path]) -> None:
    short = (
        "<html><head><title>Brief</title></head><body><article>"
        "<p>Stocks rose slightly on Friday after a quiet session in which investors waited for data.</p>"
        "</article></body></html>"
    )
    assert extract_from_html(short, url=ARTICLE_URL) is None
    assert MIN_WORDS == 80


def test_empty_or_unparseable_input_yields_none() -> None:
    assert extract_from_html("", url=ARTICLE_URL) is None
    assert extract_from_html("   \n", url=ARTICLE_URL) is None


def test_unparseable_date_becomes_none(fixture_path: Callable[[str], Path]) -> None:
    html = fixture_path("article_sample.html").read_text(encoding="utf-8")
    html = html.replace('content="2026-11-07T14:32:00+00:00"', 'content="sometime last week"')
    article = extract_from_html(html, url=ARTICLE_URL)
    assert article is not None
    assert article.published_at is None


def test_bare_year_is_never_turned_into_a_publish_date(fixture_path: Callable[[str], Path]) -> None:
    html = fixture_path("article_sample.html").read_text(encoding="utf-8")
    html = html.replace('content="2026-11-07T14:32:00+00:00"', 'content="2026"')
    html = html.replace("Copyright 2026", "Copyright 2026 and more")
    article = extract_from_html(html, url="https://www.example-finance.test/markets/rates-hold.html")
    assert article is not None
    assert article.published_at is None


def test_time_element_and_json_ld_are_used_when_meta_absent(fixture_path: Callable[[str], Path]) -> None:
    html = fixture_path("article_sample.html").read_text(encoding="utf-8")
    html = html.replace('<meta property="article:published_time" content="2026-11-07T14:32:00+00:00">', "")
    html = html.replace("<p class=\"byline\">", '<time datetime="2026-11-06T09:00:00+00:00">Nov 6</time><p class="byline">')
    article = extract_from_html(html, url="https://www.example-finance.test/markets/rates-hold.html")
    assert article is not None
    assert article.published_at == datetime(2026, 11, 6, 9, 0, tzinfo=UTC)

    jsonld = '<script type="application/ld+json">{"datePublished": "2026-11-05T08:15:00Z"}</script>'
    html2 = html.replace('<time datetime="2026-11-06T09:00:00+00:00">Nov 6</time>', "")
    html2 = html2.replace("</head>", jsonld + "</head>")
    article2 = extract_from_html(html2, url="https://www.example-finance.test/markets/rates-hold.html")
    assert article2 is not None
    assert article2.published_at == datetime(2026, 11, 5, 8, 15, tzinfo=UTC)


def test_page_without_any_date_has_none_published_at(fixture_path: Callable[[str], Path]) -> None:
    html = fixture_path("article_sample.html").read_text(encoding="utf-8")
    html = html.replace('<meta property="article:published_time" content="2026-11-07T14:32:00+00:00">', "")
    article = extract_from_html(html, url="https://www.example-finance.test/markets/rates-hold.html")
    assert article is not None
    assert article.published_at is None


async def test_robots_disallow_rule_is_respected(markets_settings: MarketsSettings) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /markets/\n")
        return httpx.Response(200, text="<html></html>")

    fetcher = _fetcher(markets_settings, handler)
    robots = RobotsChecker()
    assert await robots.allowed(fetcher, "https://www.example-finance.test/markets/x.html", UA) is False
    assert await robots.allowed(fetcher, "https://www.example-finance.test/opinion/x.html", UA) is True
    # robots.txt fetched once per origin and cached
    assert calls.count("https://www.example-finance.test/robots.txt") == 1


async def test_robots_404_allows_everything(markets_settings: MarketsSettings) -> None:
    fetcher = _fetcher(markets_settings, lambda request: httpx.Response(404, text="nope"))
    assert await RobotsChecker().allowed(fetcher, ARTICLE_URL, UA) is True


async def test_robots_403_disallows_everything(markets_settings: MarketsSettings) -> None:
    fetcher = _fetcher(markets_settings, lambda request: httpx.Response(403, text="forbidden"))
    assert await RobotsChecker().allowed(fetcher, ARTICLE_URL, UA) is False


async def test_robots_server_error_allows(markets_settings: MarketsSettings) -> None:
    fetcher = _fetcher(markets_settings, lambda request: httpx.Response(500, text="oops"))
    assert await RobotsChecker().allowed(fetcher, ARTICLE_URL, UA) is True


async def test_fetch_and_extract_end_to_end(
    markets_settings: MarketsSettings, fixture_path: Callable[[str], Path]
) -> None:
    html = fixture_path("article_sample.html").read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
        if request.url.path == "/markets/rates-hold-2026-11-07.html":
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})
        return httpx.Response(404)

    fetcher = _fetcher(markets_settings, handler)
    article = await fetch_and_extract(fetcher, ARTICLE_URL, robots=RobotsChecker(), user_agent=UA)
    assert article is not None and article.word_count >= 150


async def test_fetch_and_extract_skips_disallowed_and_failed_fetches(
    markets_settings: MarketsSettings,
) -> None:
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
        return httpx.Response(500, text="down")

    fetcher = _fetcher(markets_settings, handler)
    robots = RobotsChecker()
    # disallowed: the page itself is never requested
    assert await fetch_and_extract(
        fetcher, "https://www.example-finance.test/private/a.html", robots=robots, user_agent=UA
    ) is None
    assert "/private/a.html" not in hits
    # allowed but the fetch fails: None, no exception
    assert await fetch_and_extract(
        fetcher, "https://www.example-finance.test/markets/b.html", robots=robots, user_agent=UA
    ) is None
