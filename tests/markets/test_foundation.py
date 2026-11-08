"""T0 foundation: models, store, http, health, registry, SEC ticker map."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from awareness.markets.config import MarketsSettings
from awareness.markets.health import HealthTracker
from awareness.markets.http import Fetcher, FetchError
from awareness.markets.models import (
    CorpEvent,
    EconEvent,
    EntityRef,
    Holiday,
    IndicatorPoint,
    NewsItem,
    ProviderHealth,
    ProviderRef,
    Publisher,
    SentimentScore,
    Story,
    stable_id,
)
from awareness.markets.ref.sec_tickers import cik_to_issuer, parse_company_tickers
from awareness.markets.sources.registry import PublisherRegistry, registered_domain
from awareness.markets.store import MarketsStore, NewsQuery

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def _ref(provider: str = "test") -> ProviderRef:
    return ProviderRef(provider=provider, url="https://example.com", fetched_at=NOW)


def make_news(
    i: int,
    *,
    published: datetime | None = None,
    label: str = "standard",
    symbols: list[str] | None = None,
    topics: list[str] | None = None,
    story: str | None = None,
    tier: int = 2,
    title: str | None = None,
    importance: float = 40.0,
) -> NewsItem:
    url = f"https://www.cnbc.com/2026/11/08/story-{i}.html"
    syms = symbols or []
    return NewsItem(
        id=NewsItem.id_for_url(url),
        url=url,
        title=title or f"Story number {i} about markets and earnings",
        summary=f"Summary {i}",
        body_text=f"Body text {i} with inflation and Nvidia",
        publisher_id="cnbc",
        publisher_name="CNBC",
        publisher_domain="cnbc.com",
        publisher_tier=tier,
        published_at=published or NOW - timedelta(minutes=i),
        first_seen_at=NOW,
        feed_id="cnbc.top",
        entities=[EntityRef(type="equity", id=s, name=s, confidence=0.9) for s in syms],
        symbols=syms,
        countries=["US"],
        topics=topics or [],
        sentiment=SentimentScore(label="positive", score=0.5, confidence=0.8, model="test"),
        entity_sentiment={
            s: SentimentScore(label="positive", score=0.4, confidence=0.7, model="test") for s in syms
        },
        quality_score=55.0,
        quality_label=label,  # type: ignore[arg-type]
        importance=importance,
        story_id=story,
    )


# ── models ────────────────────────────────────────────────────────────────


def test_models_force_utc_and_stable_ids() -> None:
    naive = datetime(2026, 1, 1, 10, 0)
    ref = ProviderRef(provider="x", fetched_at=naive)
    assert ref.fetched_at.tzinfo is not None
    assert stable_id("a", 1) == stable_id("a", 1) != stable_id("a", 2)
    item = make_news(1)
    assert "body_text" not in item.public_dict()


def test_provider_health_derivation() -> None:
    h = ProviderHealth(provider="p", expected_interval_s=600)
    assert h.derive_status(NOW) == "never"
    h.last_ok_at = NOW - timedelta(minutes=10)
    assert h.derive_status(NOW) == "ok"
    h.last_ok_at = NOW - timedelta(hours=1)
    assert h.derive_status(NOW) == "degraded"
    h.last_ok_at = NOW - timedelta(days=1)
    assert h.derive_status(NOW) == "down"


# ── store: news ───────────────────────────────────────────────────────────


def test_news_upsert_preserves_first_seen_and_filters(store: MarketsStore) -> None:
    a = make_news(1, symbols=["NVDA"], topics=["earnings"], story="s1")
    b = make_news(2, symbols=["AAPL"], topics=["inflation"], label="low")
    c = make_news(3, symbols=["NVDA"], story="s1", tier=1)
    res = store.upsert_news([a, b, c])
    assert sorted(res.inserted) == sorted([a.id, b.id, c.id])
    later = a.model_copy(update={"first_seen_at": NOW + timedelta(hours=5), "quality_label": "premium"})
    res2 = store.upsert_news([later])
    assert res2.updated == [a.id]
    got = store.get_news(a.id)
    assert got is not None and got.first_seen_at == NOW and got.quality_label == "premium"

    default, _ = store.query_news(NewsQuery())
    assert {x.id for x in default} == {a.id, c.id}  # low hidden by default
    every, _ = store.query_news(NewsQuery(min_quality="rejected"))
    assert len(every) == 3
    nvda, _ = store.query_news(NewsQuery(symbols=["nvda"], min_quality="low"))
    assert {x.id for x in nvda} == {a.id, c.id}
    infl, _ = store.query_news(NewsQuery(topics=["inflation"], min_quality="low"))
    assert [x.id for x in infl] == [b.id]
    collapsed, _ = store.query_news(NewsQuery(collapse_stories=True, min_quality="low"))
    assert {x.id for x in collapsed} == {b.id, c.id}  # tier-1 c leads story s1
    fts, _ = store.query_news(NewsQuery(q="inflation nvidia", min_quality="low"))
    assert len(fts) == 3
    assert store.news_exists([a.id, "nope"]) == {a.id}


def test_news_cursor_pagination(store: MarketsStore) -> None:
    items = [make_news(i) for i in range(1, 8)]
    store.upsert_news(items)
    page1, cur = store.query_news(NewsQuery(limit=3))
    assert len(page1) == 3 and cur
    page2, cur2 = store.query_news(NewsQuery(limit=3, cursor=cur))
    page3, cur3 = store.query_news(NewsQuery(limit=3, cursor=cur2))
    ids = [x.id for x in page1 + page2 + page3]
    assert len(ids) == len(set(ids)) == 7 and cur3 is None
    assert store.query_news(NewsQuery(cursor="garbage"))[0]  # bad cursor ignored


def test_entity_sentiment_and_prune(store: MarketsStore) -> None:
    store.upsert_news([make_news(1, symbols=["TSLA"]), make_news(2, symbols=["TSLA"], published=NOW - timedelta(days=40))])
    rows = store.entity_sentiment_rows("tsla", NOW - timedelta(days=1))
    assert len(rows) == 1 and rows[0].score == pytest.approx(0.4)
    assert store.tag_counts("symbol", NOW - timedelta(days=90))[0] == ("TSLA", 2)
    removed = store.prune_news(NOW - timedelta(days=30))
    assert removed == 1 and store.counts()["news"] == 1


def test_stories_roundtrip(store: MarketsStore) -> None:
    s = Story(
        id="s1", title="t", lead_item_id="x", item_ids=["x"], publishers=["cnbc", "wsj"],
        corroboration=2, first_seen_at=NOW, last_updated_at=NOW, symbols=["NVDA"],
    )
    store.upsert_stories([s])
    assert store.get_story("s1") == s
    assert store.query_stories(symbols=["nvda"])[0].id == "s1"
    assert store.query_stories(min_corroboration=3) == []


# ── store: calendars, indicators, health, kv ──────────────────────────────


def test_calendar_tables(store: MarketsStore) -> None:
    ev = EconEvent(
        id="e1", country="us", title="CPI m/m", scheduled_at=NOW + timedelta(days=2), importance=3,
        category="inflation", sources=[_ref("nasdaq")],
    )
    store.upsert_econ_events([ev])
    got = store.query_econ(NOW, NOW + timedelta(days=7), countries=["US"], min_importance=3)
    assert [e.id for e in got] == ["e1"] and got[0].updated_at is not None
    assert store.query_econ(NOW, NOW + timedelta(days=1)) == []

    ce = CorpEvent(id="c1", type="earnings", symbol="nvda", company="NVIDIA", date=date(2026, 11, 19),
                   time_of_day="amc", fields={"eps_estimate": 1.2}, sources=[_ref("nasdaq")])
    store.upsert_corp_events([ce])
    got_c = store.query_corp(date(2026, 11, 1), date(2026, 11, 30), types=["earnings"], symbols=["NVDA"])
    assert got_c[0].fields["eps_estimate"] == 1.2

    hol = Holiday(id="h1", date=date(2026, 11, 26), exchange="xnys", name="Thanksgiving", kind="market_closed",
                  source=_ref("exchange_calendars"))
    store.upsert_holidays([hol])
    assert store.query_holidays(date(2026, 11, 1), date(2026, 11, 30), exchanges=["XNYS"])[0].name == "Thanksgiving"


def test_indicators(store: MarketsStore) -> None:
    pts = [
        IndicatorPoint(indicator="vix", ts=NOW - timedelta(days=d), value=20 + d, source=_ref("cboe"))
        for d in range(5)
    ]
    pts += [
        IndicatorPoint(indicator=f"social.stocks.mentions:{t}", ts=NOW, value=v, meta={"rank": r}, source=_ref("apewisdom"))
        for r, (t, v) in enumerate([("NVDA", 91), ("TSLA", 50)], start=1)
    ]
    store.upsert_indicator_points(pts)
    assert store.latest_indicator("vix").value == 20  # type: ignore[union-attr]
    assert [p.value for p in store.query_indicator("vix", limit=2)] == [21, 20]
    latest = store.latest_by_prefix("social.stocks.mentions:")
    assert {p.indicator.split(":")[1] for p in latest} == {"NVDA", "TSLA"}


def test_health_tracker_and_kv(store: MarketsStore) -> None:
    clock = [NOW]
    tracker = HealthTracker(store, now=lambda: clock[0])
    tracker.attempt("econ.nasdaq", 1800)
    tracker.error("econ.nasdaq", "boom")
    assert store.get_health("econ.nasdaq").status == "down"  # type: ignore[union-attr]
    tracker.ok("econ.nasdaq", 40)
    snap = tracker.snapshot(["econ"])
    assert snap[0].status == "ok" and snap[0].consecutive_failures == 0 and snap[0].items_last_ok == 40
    store.kv_set("k", {"a": 1})
    assert store.kv_get("k") == {"a": 1} and store.kv_get("missing") is None
    assert store.ensure_watchlist(["aapl", "BTC"]) == ["AAPL", "BTC"]
    assert store.ensure_watchlist(["XYZ"]) == ["AAPL", "BTC"]


# ── registry & SEC tickers ────────────────────────────────────────────────


def test_registered_domain() -> None:
    assert registered_domain("https://www.markets.ft.com/x?y") == "ft.com"
    assert registered_domain("feeds.bbci.co.uk") == "bbci.co.uk"
    assert registered_domain("www.aa.com.tr") == "aa.com.tr"
    assert registered_domain(None) is None


def test_seed_registry_loads(markets_settings: MarketsSettings) -> None:
    reg = PublisherRegistry.load(markets_settings.config_file("publishers.yaml"))
    assert reg.get("cnbc").tier == 2  # type: ignore[union-attr]
    assert reg.by_domain("https://www.cnbc.com/2026/11/08/a.html").id == "cnbc"  # type: ignore[union-attr]
    assert reg.by_domain("feeds.a.dj.com").id == "wsj"  # type: ignore[union-attr]
    assert reg.by_domain("unknown-blog.example") is None
    assert reg.by_name("bloomberg").id == "bloomberg"  # type: ignore[union-attr]
    assert len(reg.feeds()) >= 8


def test_sec_tickers_parse(fixture_path) -> None:  # type: ignore[no-untyped-def]
    issuers = parse_company_tickers(json.loads(fixture_path("sec_company_tickers.json").read_text("utf-8")))
    assert issuers[0].ticker == "AAPL" and issuers[0].rank == 0
    assert cik_to_issuer(issuers)[320193].ticker == "AAPL"
    assert all("." not in i.ticker for i in issuers)


# ── http ──────────────────────────────────────────────────────────────────


class _MemKV:
    def __init__(self) -> None:
        self.d: dict[str, object] = {}

    def kv_get(self, key: str) -> object | None:
        return self.d.get(key)

    def kv_set(self, key: str, value: object) -> None:
        self.d[key] = value


async def _no_sleep(_: float) -> None:
    return None


def test_fetcher_retries_and_conditional(markets_settings: MarketsSettings) -> None:
    calls: list[httpx.Request] = []
    script = iter([503, 200, 304, 404])

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        status = next(script)
        if status == 200:
            return httpx.Response(200, text="ok", headers={"etag": '"v1"', "content-type": "text/plain; charset=utf-8"})
        return httpx.Response(status, text="err")

    kv = _MemKV()

    async def run() -> None:
        f = Fetcher(markets_settings, kv, transport=httpx.MockTransport(handler), sleep=_no_sleep,
                    host_intervals={"example.com": 0.0})
        r = await f.get("https://example.com/feed", conditional=True, profile="browser")
        assert r.status == 200 and r.text == "ok"
        assert "Chrome" in calls[0].headers["user-agent"]
        r2 = await f.get("https://example.com/feed", conditional=True)
        assert r2.not_modified and calls[-1].headers["if-none-match"] == '"v1"'
        with pytest.raises(FetchError) as exc:
            await f.get("https://example.com/missing", max_retries=0)
        assert exc.value.status == 404
        await f.aclose()

    asyncio.run(run())
    assert len(calls) == 4


def test_registry_never_over_attributes_shared_suffixes() -> None:
    reg = PublisherRegistry(
        [
            Publisher(id="ecb", name="ECB", domain="ecb.europa.eu", tier=1, kind="official"),
            Publisher(id="rba", name="RBA", domain="rba.gov.au", tier=1, kind="official"),
            Publisher(id="bbc", name="BBC", domain="bbc.co.uk", tier=3, kind="general_press", aliases=["bbci.co.uk"]),
        ]
    )
    assert reg.by_domain("https://www.ecb.europa.eu/press/x") .id == "ecb"  # type: ignore[union-attr]
    assert reg.by_domain("ec.europa.eu") is None  # European Commission is not the ECB
    assert reg.by_domain("abs.gov.au") is None
    assert reg.by_domain("feeds.bbci.co.uk").id == "bbc"  # type: ignore[union-attr]
    assert registered_domain("https://www.boj.or.jp/en/") == "boj.or.jp"
