"""News feed parsing and collectors. Tests never touch the network (httpx.MockTransport)."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

import httpx
import pytest

from awareness.markets.collectors.base import CollectorContext
from awareness.markets.config import PACKAGE_DATA_DIR, MarketsSettings
from awareness.markets.http import Fetcher
from awareness.markets.models import FeedSpec, NewsItem, Publisher, RawNewsItem
from awareness.markets.news.feeds import (
    _MAX_BACKOFF_S,
    FeedCollector,
    GoogleNewsCollector,
    _ingest,
    _schedule_next,
    _stagger_offset,
    canonicalize_url,
    clean_text,
    google_news_url,
    parse_feed,
    parse_google_news,
    watchlist_query,
)
from awareness.markets.ref.sec_tickers import cache_path
from awareness.markets.sources.registry import PublisherRegistry
from awareness.markets.store import MarketsStore

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
TAG_RE = re.compile(r"<[^>]+>")


class FakePipeline:
    """Stands in for the news pipeline: records each call and stores nothing."""

    def __init__(self) -> None:
        self.calls: list[list[RawNewsItem]] = []

    async def process(self, raws: list[RawNewsItem]) -> list[NewsItem]:
        self.calls.append(list(raws))
        return cast(list[NewsItem], list(raws))  # counted as "stored" by the collectors

    @property
    def urls(self) -> list[str]:
        return [raw.url for call in self.calls for raw in call]


async def _no_sleep(_seconds: float) -> None:
    return None


def _publisher(pid: str = "cnbc", tier: int = 2, feeds: list[FeedSpec] | None = None) -> Publisher:
    return Publisher(
        id=pid,
        name=pid.upper(),
        domain=f"{pid}.com",
        tier=tier,
        kind="financial_press",
        feeds=feeds or [],
    )


def _feed(fid: str, url: str, poll: int = 120, kind: Literal["rss", "atom"] = "rss") -> FeedSpec:
    return FeedSpec(id=fid, url=url, categories=["markets"], poll_seconds=poll, kind=kind)


def _ctx(
    settings: MarketsSettings,
    store: MarketsStore,
    registry: PublisherRegistry,
    handler: Callable[[httpx.Request], httpx.Response],
    pipeline: FakePipeline | None,
    now: datetime = NOW,
) -> CollectorContext:
    http = Fetcher(settings, store, transport=httpx.MockTransport(handler), sleep=_no_sleep)
    return CollectorContext(
        settings=settings,
        store=store,
        http=http,
        registry=registry,
        pipeline=pipeline,
        now=lambda: now,
    )


# ── URL canonicalisation and text cleaning ──────────────────────────────


def test_canonicalize_strips_tracking_fragment_and_sorts_query() -> None:
    raw = "HTTPS://WWW.Example.com/a/b?utm_source=x&z=1&a=2&fbclid=abc&utm_medium=email#frag"
    assert canonicalize_url(raw) == "https://www.example.com/a/b?a=2&z=1"


def test_canonicalize_is_idempotent_and_keeps_www() -> None:
    once = canonicalize_url("https://www.cnbc.com/2026/11/08/x.html?ref=home&id=7")
    assert once == "https://www.cnbc.com/2026/11/08/x.html?id=7"
    assert canonicalize_url(once) == once


def test_canonicalize_leaves_non_absolute_values_alone() -> None:
    assert canonicalize_url("  not a url ") == "not a url"


def test_canonicalize_keeps_valueless_params_as_written() -> None:
    assert canonicalize_url("https://www.example.com/a?flag&b=1") == "https://www.example.com/a?b=1&flag"
    assert canonicalize_url("https://www.example.com/a?flag") == "https://www.example.com/a?flag"


def test_clean_text_unescapes_strips_tags_and_collapses_whitespace() -> None:
    assert clean_text("  <p>Tom &amp; Jerry&#39;s   <b>hold</b></p>\n\n now ") == "Tom & Jerry's hold now"


def test_clean_text_keeps_comparison_operators() -> None:
    assert clean_text("Margins &lt;5% and sales &gt;10% this quarter") == "Margins <5% and sales >10% this quarter"
    assert clean_text("Price < 5% and > 3% today") == "Price < 5% and > 3% today"


def test_clean_text_inline_tags_do_not_split_words_and_blocks_do_separate() -> None:
    assert clean_text("Tom<b>son</b>'s shares") == "Tomson's shares"
    assert clean_text("First line.</p><p>Second line.") == "First line. Second line."


def test_clean_text_truncates_and_handles_empty() -> None:
    assert clean_text(None) is None
    assert clean_text("<br/>   ") is None
    out = clean_text("word " * 50, max_len=20)
    assert out is not None and len(out) <= 20 and out.endswith("…")


# ── RSS / Atom parsing against recorded payloads ────────────────────────

# (fixture, expected item count, ASCII prefix of the first title). Pinned from the recorded payloads.
FIXTURES: list[tuple[str, int, str]] = [
    ("rss_cnbc_top.xml", 10, "Anthropic IPO meets AI backlash"),
    ("rss_mw_top.xml", 10, "This retired professor has reinvented himself"),
    ("rss_wsj_markets.xml", 10, "Stocks Sink in Broad AI Rout"),
    ("rss_bloomberg_markets.xml", 10, "Australia Home Sales Rise"),
    ("rss_ft_home.xml", 8, "Israel"),
    ("rss_yahoo_aapl.xml", 10, "Dow Jones Futures"),
    ("rss_seekingalpha_aapl.xml", 10, "Earnings Scoreboard"),
    ("rss_fed_press.xml", 10, "Federal Reserve Board announces termination"),
    ("rss_coindesk.xml", 10, "Binance users in some European countries"),
    ("rss_bbc_business.xml", 10, "Labubu hype seemed unstoppable"),
    ("rss_aa_ekonomi.xml", 10, "Irakl"),
    ("rss_bloomberght.xml", 10, "Erdo"),
    ("rss_prnewswire_fin.xml", 10, "Four Course Records"),
    ("reddit_stocks_new.atom", 10, "What about the FCF"),
]


@pytest.mark.parametrize(("name", "count", "title_prefix"), FIXTURES)
def test_recorded_feed_parses_into_clean_items(
    fixture_bytes: Callable[[str], bytes], name: str, count: int, title_prefix: str
) -> None:
    kind: Literal["rss", "atom"] = "atom" if name.endswith(".atom") else "rss"
    feed = _feed(f"test.{name}", "https://example.test/feed", kind=kind)
    items = parse_feed(fixture_bytes(name), feed=feed, publisher=_publisher(), fetched_at=NOW)
    assert len(items) == count, name
    assert items[0].title.startswith(title_prefix), items[0].title
    for item in items:
        assert item.url.startswith(("http://", "https://"))
        assert item.title and not TAG_RE.search(item.title)
        assert item.summary is None or not TAG_RE.search(item.summary)
        if item.published_at is not None:
            assert item.published_at.tzinfo is not None
            assert item.published_at.utcoffset() == timedelta(0)
        assert item.feed_id == feed.id
        assert item.fetched_at == NOW


def test_summary_equal_to_title_is_dropped_and_author_prefix_stripped() -> None:
    xml = b"""<?xml version="1.0"?><rss version="2.0"><channel>
    <item><title>Fed holds rates steady</title><link>https://www.cnbc.com/a?utm_campaign=z</link>
      <description>Fed holds rates steady</description><author>By Jane Doe</author>
      <category>Rates</category><category>Central Bank</category>
      <pubDate>Sun, 08 Nov 2026 09:00:00 GMT</pubDate></item>
    <item><title>   </title><link>https://www.cnbc.com/blank</link></item>
    <item><title>No link here</title></item>
    </channel></rss>"""
    feed = _feed("cnbc.top", "https://example.test/feed")
    items = parse_feed(xml, feed=feed, publisher=_publisher(), fetched_at=NOW)
    assert len(items) == 1
    item = items[0]
    assert item.url == "https://www.cnbc.com/a"
    assert item.summary is None
    assert item.author == "Jane Doe"
    assert item.published_at == datetime(2026, 11, 8, 9, 0, tzinfo=UTC)
    assert item.categories[:1] == ["markets"]
    assert "rates" in item.categories


def test_body_text_kept_only_when_long_enough() -> None:
    long_body = "<p>" + ("Earnings beat expectations. " * 30) + "</p>"
    xml = (
        f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>'
        f"<title>Long story</title><link href=\"https://www.ft.com/x\"/>"
        f'<content type="html">{long_body}</content></entry>'
        f"<entry><title>Short story</title><link href=\"https://www.ft.com/y\"/>"
        f'<content type="html"><p>Short.</p></content></entry></feed>'
    ).encode()
    feed = _feed("ft.home", "https://example.test/feed", kind="atom")
    items = parse_feed(xml, feed=feed, publisher=_publisher("ft", 2), fetched_at=NOW)
    by_title = {i.title: i for i in items}
    assert by_title["Long story"].body_text is not None
    assert len(by_title["Long story"].body_text or "") >= 600
    assert by_title["Short story"].body_text is None


def test_language_falls_back_to_publisher_then_feed() -> None:
    xml = b'<?xml version="1.0"?><rss version="2.0"><channel><item><title>Haber basligi burada</title><link>https://www.aa.com.tr/a</link></item></channel></rss>'
    publisher = Publisher(id="aa", name="AA", domain="aa.com.tr", tier=3, kind="wire", languages=["tr"])
    tr_feed = _feed("aa.ekonomi", "https://example.test/feed")
    tr_feed = tr_feed.model_copy(update={"language": "tr"})
    assert parse_feed(xml, feed=tr_feed, publisher=publisher, fetched_at=NOW)[0].language == "tr"
    assert parse_feed(xml, feed=_feed("aa.x", "https://e.test"), publisher=publisher, fetched_at=NOW)[0].language == "tr"


# ── Google News ─────────────────────────────────────────────────────────


def test_google_news_keeps_only_allowlisted_origins(fixture_bytes: Callable[[str], bytes]) -> None:
    registry = PublisherRegistry.load(PACKAGE_DATA_DIR / "publishers.yaml")
    items = parse_google_news(
        fixture_bytes("rss_google_news_aapl.xml"),
        registry=registry,
        feed_id="gnews.AAPL",
        fetched_at=NOW,
    )
    assert items, "fixture should contain allowlisted origins"
    origins = {item.publisher_id for item in items}
    assert origins <= {p.id for p in registry.all() if p.tier <= 3}
    assert "yahoo_finance" not in origins  # tier 4
    for item in items:
        assert item.feed_id == "gnews.AAPL"
        assert item.summary is None
        assert item.origin_publisher_name
        assert not item.title.endswith(f" - {item.origin_publisher_name}")
        assert item.url.startswith("https://news.google.com/")


def test_google_news_drops_unknown_and_low_tier_origins() -> None:
    xml = b"""<?xml version="1.0"?><rss version="2.0"><channel>
    <item><title>Story one - TheStreet</title><link>https://news.google.com/rss/articles/a</link>
      <source url="https://www.thestreet.com">TheStreet</source></item>
    <item><title>Story two - Motley Fool</title><link>https://news.google.com/rss/articles/b</link>
      <source url="https://www.fool.com">The Motley Fool</source></item>
    <item><title>Story three - CNBC</title><link>https://news.google.com/rss/articles/c</link>
      <source url="https://www.cnbc.com">CNBC</source></item>
    </channel></rss>"""
    registry = PublisherRegistry.load(PACKAGE_DATA_DIR / "publishers.yaml")
    items = parse_google_news(xml, registry=registry, feed_id="gnews.X", fetched_at=NOW)
    assert [i.publisher_id for i in items] == ["cnbc"]
    assert items[0].title == "Story three"
    assert items[0].origin_publisher_domain == "cnbc.com"


def test_google_news_url_encodes_query() -> None:
    url = google_news_url('"Apple" OR AAPL stock when:2d')
    assert url.startswith("https://news.google.com/rss/search?")
    assert "q=%22Apple%22+OR+AAPL+stock+when%3A2d" in url
    assert "ceid=US%3Aen" in url


def test_watchlist_query_variants() -> None:
    assert watchlist_query("BTC", None) == "Bitcoin when:2d"
    assert watchlist_query("SPY", None) == '"S&P 500" when:1d'
    assert watchlist_query("AAPL", "APPLE INC.") == '"Apple" OR AAPL stock when:2d'
    assert watchlist_query("NVDA", "NVIDIA CORP") == '"Nvidia" OR NVDA stock when:2d'
    assert watchlist_query("BRK-B", "BERKSHIRE HATHAWAY INC") == '"Berkshire Hathaway" OR BRK-B stock when:2d'
    assert watchlist_query("GOOGL", "Alphabet Inc. Class A") == '"Alphabet" OR GOOGL stock when:2d'
    assert watchlist_query("AAPL", None) is None
    assert watchlist_query("XYZ", None) is None


# ── FeedCollector ───────────────────────────────────────────────────────


def _cnbc_registry(extra: list[Publisher] | None = None) -> PublisherRegistry:
    publishers = [
        _publisher("cnbc", 2, [_feed("cnbc.top", "https://feeds.test/cnbc", poll=120)]),
        _publisher("bbc", 3, [_feed("bbc.business", "https://feeds.test/bbc", poll=240)]),
    ]
    return PublisherRegistry([*publishers, *(extra or [])])


def _route(
    fixtures: dict[str, bytes], calls: list[str] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if calls is not None:
            calls.append(url)
        if url in fixtures:
            return httpx.Response(200, content=fixtures[url], headers={"content-type": "application/xml"})
        return httpx.Response(404, text="not routed")

    return handler


async def test_feed_collector_isolates_failures_and_records_health(
    markets_settings: MarketsSettings, store: MarketsStore, fixture_bytes: Callable[[str], bytes]
) -> None:
    registry = _cnbc_registry()
    routed = _route({"https://feeds.test/cnbc": fixture_bytes("rss_cnbc_top.xml")})

    def mixed(request: httpx.Request) -> httpx.Response:
        if request.url.host == "feeds.test" and request.url.path == "/bbc":
            return httpx.Response(500, text="boom")
        return routed(request)

    pipeline = FakePipeline()
    ctx = _ctx(markets_settings, store, registry, mixed, pipeline)
    result = await FeedCollector().run(ctx)

    assert result.items == len(pipeline.urls) > 0
    assert "feeds_due=2" in result.notes and "feeds_failed=1" in result.notes
    ok = store.get_health("feed.cnbc.top")
    bad = store.get_health("feed.bbc.business")
    assert ok is not None and ok.status == "ok" and ok.items_last_ok > 0
    assert bad is not None and bad.consecutive_failures == 1 and bad.last_error


async def test_feed_collector_raises_only_when_every_due_feed_failed(
    markets_settings: MarketsSettings, store: MarketsStore
) -> None:
    def always_500(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    ctx = _ctx(markets_settings, store, _cnbc_registry(), always_500, FakePipeline())
    with pytest.raises(RuntimeError, match="all feeds failed"):
        await FeedCollector().run(ctx)


async def test_feed_collector_respects_due_schedule(
    markets_settings: MarketsSettings, store: MarketsStore, fixture_bytes: Callable[[str], bytes]
) -> None:
    calls: list[str] = []
    handler = _route({"https://feeds.test/cnbc": fixture_bytes("rss_cnbc_top.xml")}, calls=calls)
    registry = _cnbc_registry()
    pipeline = FakePipeline()

    await FeedCollector().run(_ctx(markets_settings, store, registry, handler, pipeline))
    first_calls = len(calls)
    assert first_calls >= 1

    # Immediately again: next_due is in the future for both feeds, nothing is fetched.
    second = await FeedCollector().run(_ctx(markets_settings, store, registry, handler, pipeline))
    assert len(calls) == first_calls
    assert second.notes[0] == "feeds_due=0"

    # Next due is poll_seconds ±10 % after the first run.
    due = store.kv_get("feeds.next_due:cnbc.top")
    delta = (datetime.fromisoformat(due) - NOW).total_seconds()
    assert 120 * 0.9 - 1 <= delta <= 120 * 1.1 + 1


async def test_feed_collector_handles_not_modified(
    markets_settings: MarketsSettings, store: MarketsStore, fixture_bytes: Callable[[str], bytes]
) -> None:
    registry = _cnbc_registry()
    url = "https://feeds.test/cnbc"
    body = fixture_bytes("rss_cnbc_top.xml")

    def etag_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "feeds.test" or request.url.path != "/cnbc":
            return httpx.Response(500)
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, content=body, headers={"etag": '"v1"'})

    pipeline = FakePipeline()
    first = await FeedCollector().run(_ctx(markets_settings, store, registry, etag_handler, pipeline))
    assert first.items > 0
    assert store.kv_get(f"http.validators:{url}") == {"etag": '"v1"', "last_modified": None}

    later = NOW + timedelta(hours=1)
    seen = len(pipeline.urls)
    second = await FeedCollector().run(_ctx(markets_settings, store, registry, etag_handler, pipeline, now=later))
    assert second.items == 0
    assert len(pipeline.urls) == seen  # nothing new handed to the pipeline
    health = store.get_health("feed.cnbc.top")
    assert health is not None and health.status == "ok" and health.items_last_ok == 0


async def test_feed_collector_drops_stale_and_already_stored(
    markets_settings: MarketsSettings, store: MarketsStore
) -> None:
    rss = b"""<?xml version="1.0"?><rss version="2.0"><channel>
    <item><title>Old story about markets</title><link>https://www.cnbc.com/old?utm_source=x</link>
      <pubDate>Mon, 01 Jun 2026 10:00:00 GMT</pubDate></item>
    <item><title>Fresh story about markets</title><link>https://www.cnbc.com/fresh#top</link>
      <pubDate>Sun, 08 Nov 2026 09:00:00 GMT</pubDate>
      <description>&lt;p&gt;Summary &amp;amp; more&lt;/p&gt;</description></item>
    <item><title>Already stored story</title><link>https://www.cnbc.com/stored</link>
      <pubDate>Sun, 08 Nov 2026 08:00:00 GMT</pubDate></item>
    </channel></rss>"""
    stored = NewsItem(
        id=NewsItem.id_for_url("https://www.cnbc.com/stored"),
        url="https://www.cnbc.com/stored",
        title="Already stored story",
        publisher_id="cnbc",
        publisher_name="CNBC",
        publisher_domain="cnbc.com",
        publisher_tier=2,
        published_at=NOW,
        first_seen_at=NOW,
        feed_id="cnbc.top",
    )
    store.upsert_news([stored])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=rss)

    pipeline = FakePipeline()
    registry = _cnbc_registry()
    # Only the cnbc feed matters here; bbc returns the same payload and is deduplicated.
    result = await FeedCollector().run(_ctx(markets_settings, store, registry, handler, pipeline))
    assert pipeline.urls == ["https://www.cnbc.com/fresh"]
    fresh = pipeline.calls[0][0]
    assert fresh.summary == "Summary & more"
    assert result.items == 1


async def test_feed_collector_requires_pipeline(markets_settings: MarketsSettings, store: MarketsStore) -> None:
    ctx = _ctx(markets_settings, store, _cnbc_registry(), lambda r: httpx.Response(200), None)
    with pytest.raises(RuntimeError, match="pipeline"):
        await FeedCollector().run(ctx)


# ── GoogleNewsCollector ─────────────────────────────────────────────────


async def test_google_news_collector_queries_watchlist_and_filters_origins(
    markets_settings: MarketsSettings, store: MarketsStore, fixture_bytes: Callable[[str], bytes]
) -> None:
    store.set_watchlist(["AAPL", "BTC"])
    sec_payload = b'{"0": {"cik_str": 320193, "ticker": "AAPL", "title": "APPLE INC."}}'
    path = cache_path(markets_settings.ref_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(sec_payload)  # the loader reads the cache as-is (no 1000-issuer check here)

    queries: list[str] = []
    gnews = fixture_bytes("rss_google_news_aapl.xml")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "news.google.com":
            queries.append(request.url.params["q"])
            return httpx.Response(200, content=gnews)
        return httpx.Response(404)

    registry = PublisherRegistry.load(PACKAGE_DATA_DIR / "publishers.yaml")
    pipeline = FakePipeline()
    result = await GoogleNewsCollector().run(_ctx(markets_settings, store, registry, handler, pipeline))

    assert queries == ['"Apple" OR AAPL stock when:2d', "Bitcoin when:2d"]
    kept = [raw for call in pipeline.calls for raw in call]
    assert kept, "allowlisted origins from the fixture should be stored"
    assert all(raw.feed_id in ("gnews.AAPL", "gnews.BTC") for raw in kept)
    assert "gnews_queries=2" in result.notes


async def test_google_news_collector_raises_when_every_query_fails(
    markets_settings: MarketsSettings, store: MarketsStore
) -> None:
    store.set_watchlist(["SPY"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    registry = PublisherRegistry.load(PACKAGE_DATA_DIR / "publishers.yaml")
    ctx = _ctx(markets_settings, store, registry, handler, FakePipeline())
    with pytest.raises(RuntimeError, match="all google news queries failed"):
        await GoogleNewsCollector().run(ctx)
    health = store.get_health("gnews.SPY")
    assert health is not None and health.consecutive_failures == 1


def test_news_item_id_for_url_is_stable_across_tracking_params() -> None:
    a = NewsItem.id_for_url(canonicalize_url("https://www.cnbc.com/x?utm_source=a"))
    b = NewsItem.id_for_url(canonicalize_url("https://www.cnbc.com/x#y"))
    assert a == b


def test_malformed_entry_is_skipped_without_failing_the_feed() -> None:
    xml = b"""<?xml version="1.0"?><rss version="2.0"><channel>
    <item><title>Broken link</title><link>http://[bad</link></item>
    <item><title>Good link</title><link>https://www.cnbc.com/good</link></item>
    </channel></rss>"""
    items = parse_feed(xml, feed=_feed("cnbc.top", "https://e.test"), publisher=_publisher(), fetched_at=NOW)
    assert [i.url for i in items] == ["https://www.cnbc.com/good"]


def test_google_news_unresolved_source_url_is_dropped_even_when_name_matches() -> None:
    xml = b"""<?xml version="1.0"?><rss version="2.0"><channel>
    <item><title>Impostor story - Reuters</title><link>https://news.google.com/rss/articles/x</link>
      <source url="https://reuters-clone.example">Reuters</source></item>
    </channel></rss>"""
    registry = PublisherRegistry.load(PACKAGE_DATA_DIR / "publishers.yaml")
    assert parse_google_news(xml, registry=registry, feed_id="gnews.X", fetched_at=NOW) == []


def test_failure_backoff_doubles_is_capped_and_never_shortens_poll(
    markets_settings: MarketsSettings, store: MarketsStore
) -> None:
    ctx = _ctx(markets_settings, store, _cnbc_registry(), lambda r: httpx.Response(200), None)

    def delay_of(feed: FeedSpec, streak: int) -> float:
        _schedule_next(ctx, feed, NOW, streak)
        stored = datetime.fromisoformat(store.kv_get(f"feeds.next_due:{feed.id}"))
        return (stored - NOW).total_seconds()

    short = _feed("x.short", "https://e.test", poll=120)
    assert 960 * 0.9 <= delay_of(short, 3) <= 960 * 1.1  # 120 * 2**3
    assert _MAX_BACKOFF_S * 0.9 <= delay_of(short, 10) <= _MAX_BACKOFF_S * 1.1  # capped at 30 min
    long_poll = _feed("x.long", "https://e.test", poll=3600)
    assert delay_of(long_poll, 1) >= 3600 * 0.9  # the cap never shortens a long poll


def test_stagger_offset_applies_only_to_community_feeds_and_is_stable() -> None:
    community = _publisher("reddit", 5, [])
    community = community.model_copy(update={"kind": "community"})
    reddit = _feed("reddit.stocks", "https://www.reddit.com/r/stocks/new/.rss", poll=900, kind="atom")
    offset = _stagger_offset(community, reddit)
    assert 0 <= offset <= 600
    assert offset == _stagger_offset(community, reddit)
    assert offset == 135  # crc32("reddit.stocks") % 601: stable across processes
    assert _stagger_offset(_publisher("cnbc", 2), _feed("cnbc.top", "https://e.test")) == 0


async def test_community_feed_is_not_fetched_before_its_staggered_start(
    markets_settings: MarketsSettings, store: MarketsStore, fixture_bytes: Callable[[str], bytes]
) -> None:
    reddit_feed = _feed("reddit.stocks", "https://www.reddit.com/r/stocks/new/.rss", poll=900, kind="atom")
    reddit = _publisher("reddit", 5, [reddit_feed]).model_copy(update={"kind": "community"})
    calls: list[str] = []
    handler = _route({"https://www.reddit.com/r/stocks/new/.rss": fixture_bytes("reddit_stocks_new.atom")}, calls)
    registry = PublisherRegistry([reddit])
    result = await FeedCollector().run(_ctx(markets_settings, store, registry, handler, FakePipeline()))
    assert calls == [] and result.notes[0] == "feeds_due=0"  # first poll waits for its offset
    later = NOW + timedelta(seconds=_stagger_offset(reddit, reddit_feed) + 1)
    await FeedCollector().run(_ctx(markets_settings, store, registry, handler, FakePipeline(), now=later))
    assert len(calls) == 1


async def test_run_takes_the_40_most_overdue_feeds_and_defers_the_rest(
    markets_settings: MarketsSettings, store: MarketsStore, fixture_bytes: Callable[[str], bytes]
) -> None:
    publishers = [_publisher(f"p{i:02d}", 2, [_feed(f"p{i:02d}.top", f"https://feeds.test/p{i:02d}")]) for i in range(45)]
    registry = PublisherRegistry(publishers)
    # The five last publishers were due three days ago; they must be picked before the rest.
    for i in range(40, 45):
        store.kv_set(f"feeds.next_due:p{i:02d}.top", (NOW - timedelta(days=3)).isoformat())
    calls: list[str] = []
    body = fixture_bytes("rss_cnbc_top.xml")

    def serve(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=body)

    result = await FeedCollector().run(_ctx(markets_settings, store, registry, serve, FakePipeline()))
    assert len(calls) == 40
    assert "feeds_deferred=5" in result.notes
    for i in range(40, 45):
        assert f"https://feeds.test/p{i:02d}" in calls


async def test_each_feed_reaches_the_pipeline_as_its_own_batch(
    markets_settings: MarketsSettings, store: MarketsStore, fixture_bytes: Callable[[str], bytes]
) -> None:
    registry = _cnbc_registry()
    routed = _route(
        {
            "https://feeds.test/cnbc": fixture_bytes("rss_cnbc_top.xml"),
            "https://feeds.test/bbc": fixture_bytes("rss_bbc_business.xml"),
        }
    )
    pipeline = FakePipeline()
    result = await FeedCollector().run(_ctx(markets_settings, store, registry, routed, pipeline))
    assert len(pipeline.calls) == 2  # one call per completed feed, not one call for the whole run
    assert result.items == sum(len(c) for c in pipeline.calls) == 20


async def test_ingest_sends_at_most_25_items_per_pipeline_call(
    markets_settings: MarketsSettings, store: MarketsStore
) -> None:
    ctx = _ctx(markets_settings, store, _cnbc_registry(), lambda r: httpx.Response(200), None)
    pipeline = FakePipeline()
    raws = [
        RawNewsItem(
            url=f"https://www.cnbc.com/story-{i}",
            title=f"Story {i} about markets",
            feed_id="cnbc.top",
            publisher_id="cnbc",
            published_at=NOW,
        )
        for i in range(30)
    ]
    stored = await _ingest(ctx, pipeline, raws)
    assert [len(c) for c in pipeline.calls] == [25, 5]
    assert stored == 30


def test_collectors_declare_the_900_second_run_timeout() -> None:
    assert FeedCollector.timeout_s == 900
    assert GoogleNewsCollector.timeout_s == 900

