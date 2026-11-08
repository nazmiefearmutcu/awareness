"""T8 runtime: news pipeline wiring (with fakes), MarketsRunner, HTTP API contract."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from awareness.markets.app import create_app
from awareness.markets.collectors.base import CollectorContext, CollectorResult
from awareness.markets.config import MarketsSettings
from awareness.markets.http import Fetcher, FetchError
from awareness.markets.models import (
    CorpEvent,
    EconEvent,
    EntityRef,
    Holiday,
    IndicatorPoint,
    NewsItem,
    ProviderRef,
    Publisher,
    RawNewsItem,
    SentimentScore,
    Story,
    stable_id,
)
from awareness.markets.news.pipeline import NewsPipeline
from awareness.markets.scheduler import MarketsRunner
from awareness.markets.sources.registry import PublisherRegistry
from awareness.markets.store import MarketsStore, NewsQuery

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def _registry() -> PublisherRegistry:
    return PublisherRegistry(
        [
            Publisher(id="cnbc", name="CNBC", domain="cnbc.com", tier=2, kind="financial_press", feeds=[]),
            Publisher(id="wsj", name="WSJ", domain="wsj.com", tier=2, kind="financial_press", paywalled=True, feeds=[]),
            Publisher(id="yahoo_finance", name="Yahoo Finance", domain="finance.yahoo.com", tier=4, kind="aggregator", feeds=[]),
        ]
    )


# ── fakes implementing the component protocols ────────────────────────────


class FakeLinker:
    def link(self, title: str, text: str | None = None, *, language: str | None = None) -> list[EntityRef]:
        out = []
        if "Nvidia" in title:
            out.append(EntityRef(type="equity", id="NVDA", name="Nvidia", confidence=0.95, salience=1.0))
        if "Fed" in title:
            out.append(EntityRef(type="central_bank", id="FED", name="Federal Reserve", confidence=0.95, salience=1.0))
            out.append(EntityRef(type="country", id="US", name="United States", confidence=0.7, salience=0.5))
        return out


@dataclass
class FakeTax:
    asset_classes: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    finance_relevance: float = 0.8
    content_type_hint: str | None = None


class FakeTaxonomy:
    def classify(self, title: str, text: str | None, entities: list[EntityRef], **_: Any) -> FakeTax:
        if "celebrity" in title.lower():
            return FakeTax(finance_relevance=0.05)
        topics = ["earnings"] if "earnings" in title.lower() else ["central_bank"] if "Fed" in title else []
        return FakeTax(asset_classes=["equities"], topics=topics)


class FakeScorer:
    model = "fake:1"

    def score_many(self, texts: list[str]) -> list[SentimentScore]:
        return [SentimentScore(label="positive", score=0.6, confidence=0.9, model=self.model) for _ in texts]

    def entity_sentiment(self, title: str, text: str | None, entities: list[EntityRef], **_: Any) -> dict[str, SentimentScore]:
        return {e.id: SentimentScore(label="positive", score=0.5, confidence=0.8, model=self.model) for e in entities}


@dataclass
class Verdict:
    score: float
    label: str
    reasons: list[str]
    content_type: str
    hard_reject: bool = False


class FakeGate:
    def evaluate(self, item: NewsItem, publisher: Publisher | None, *, now: datetime, finance_relevance: float,
                 corroboration: int = 1, timestamp_source: str = "feed", content_type_hint: str | None = None) -> Verdict:
        if publisher is None:
            return Verdict(0, "rejected", ["unknown_publisher"], "news", True)
        if timestamp_source == "fetched":
            return Verdict(0, "rejected", ["missing_timestamp"], "news", True)
        if finance_relevance < 0.25:
            return Verdict(0, "rejected", ["non_finance"], "news", True)
        score = {1: 45, 2: 38, 3: 28, 4: 15, 5: 5}[publisher.tier] + 10 + 5 * (corroboration - 1)
        label = "premium" if score >= 60 else "standard" if score >= 40 else "low"
        return Verdict(score, label, [f"tier{publisher.tier}", f"corroboration:{corroboration}"], "news")


class FakeClusterer:
    def find_story(self, item: NewsItem, candidates: list[NewsItem]) -> str | None:
        key = "nvidia" if "Nvidia" in item.title else None
        for c in candidates:
            if key and key in c.title.lower() and c.story_id and c.id != item.id:
                return c.story_id
        return None

    def new_story_id(self, item: NewsItem) -> str:
        return stable_id("story", item.id)

    def build_story(self, items: list[NewsItem], *, story_id: str) -> Story:
        lead = sorted(items, key=lambda i: (i.publisher_tier, i.published_at))[0]
        return Story(
            id=story_id, title=lead.title, lead_item_id=lead.id, item_ids=[i.id for i in items],
            publishers=list(dict.fromkeys(i.publisher_id for i in items)),
            corroboration=self.corroboration(items), first_seen_at=min(i.first_seen_at for i in items),
            last_updated_at=max(i.published_at for i in items), symbols=sorted({s for i in items for s in i.symbols}),
        )

    @staticmethod
    def corroboration(items: list[NewsItem]) -> int:
        return max(1, len({i.publisher_id for i in items if i.publisher_tier <= 3}))


def _importance(item: NewsItem, *, corroboration: int = 1) -> float:
    return 30.0 + 5 * (corroboration - 1)


def _pipeline(settings: MarketsSettings, store: MarketsStore) -> NewsPipeline:
    return NewsPipeline(
        settings=settings, store=store, registry=_registry(), fetcher=None, linker=FakeLinker(),
        taxonomy=FakeTaxonomy(), scorer=FakeScorer(), gate=FakeGate(), clusterer=FakeClusterer(),
        importance=_importance, now=lambda: NOW,
    )


def _raw(n: int, title: str, publisher: str = "cnbc", *, published: datetime | None = NOW - timedelta(minutes=5), **kw: Any) -> RawNewsItem:
    return RawNewsItem(
        url=f"https://{publisher}.example/{n}", title=title, summary="A sufficiently descriptive summary.",
        published_at=published, feed_id=f"{publisher}.top", publisher_id=publisher, fetched_at=NOW, **kw,
    )


def test_pipeline_enriches_gates_clusters_and_rescored(markets_settings: MarketsSettings, store: MarketsStore) -> None:
    pipe = _pipeline(markets_settings, store)
    first = asyncio.run(pipe.process([_raw(1, "Nvidia earnings beat estimates", "cnbc")]))
    assert len(first) == 1
    a = first[0]
    assert a.symbols == ["NVDA"] and a.topics == ["earnings"] and a.quality_label == "standard"
    assert a.entity_sentiment["NVDA"].score == 0.5 and a.sentiment and a.sentiment.model == "fake:1"
    assert a.excerpt and a.sources[0].provider == "cnbc.top"

    second = asyncio.run(
        pipe.process(
            [
                _raw(2, "Nvidia tops earnings estimates", "wsj"),
                _raw(3, "Fed holds rates steady", "cnbc"),
                _raw(4, "Celebrity wedding photos", "cnbc"),
                _raw(5, "Undated Nvidia item", "cnbc", published=None),
                _raw(6, "Nvidia blog repost", "yahoo_finance", origin_publisher_domain="randomblog.example"),
                _raw(1, "Nvidia earnings beat estimates", "cnbc"),  # already stored → skipped
            ]
        )
    )
    by_title = {i.title: i for i in second}
    assert "Nvidia earnings beat estimates" not in by_title  # existing id skipped
    wsj = by_title["Nvidia tops earnings estimates"]
    assert wsj.story_id == a.story_id  # clustered with the CNBC item
    assert by_title["Celebrity wedding photos"].quality_reasons == ["non_finance"]
    assert by_title["Undated Nvidia item"].quality_reasons == ["missing_timestamp"]
    assert by_title["Nvidia blog repost"].quality_label == "rejected"  # unknown origin behind aggregator
    stored_a = store.get_news(a.id)
    assert stored_a is not None and "corroboration:2" in stored_a.quality_reasons  # existing member re-scored
    story = store.get_story(a.story_id or "")
    assert story is not None and story.corroboration == 2 and set(story.publishers) == {"cnbc", "wsj"}
    visible, _ = store.query_news(NewsQuery(collapse_stories=True))
    titles = {i.title for i in visible}
    assert "Celebrity wedding photos" not in titles and "Fed holds rates steady" in titles
    assert pipe.last_stats.rejected == 3


# ── runner ────────────────────────────────────────────────────────────────


class OkCollector:
    name = "test.ok"
    interval_s = 3600
    lock_group = "g"

    def __init__(self) -> None:
        self.runs = 0

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        self.runs += 1
        return CollectorResult(items=3, notes=["fine"])


class BadCollector:
    name = "test.bad"
    interval_s = 600
    lock_group = "g"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        raise RuntimeError("provider down")


def _ctx(settings: MarketsSettings, store: MarketsStore) -> CollectorContext:
    return CollectorContext(settings=settings, store=store, http=None, registry=_registry())  # type: ignore[arg-type]


def test_runner_run_once_records_health(markets_settings: MarketsSettings, store: MarketsStore) -> None:
    ok, bad = OkCollector(), BadCollector()
    runner = MarketsRunner(_ctx(markets_settings, store), [ok, bad], maintenance=False)

    async def go() -> None:
        res = await runner.run_once("test.ok")
        assert res.items == 3
        with pytest.raises(RuntimeError):
            await runner.run_once("test.bad")

    asyncio.run(go())
    assert store.get_health("test.ok").status == "ok"  # type: ignore[union-attr]
    bad_h = store.get_health("test.bad")
    assert bad_h is not None and bad_h.consecutive_failures == 1 and "provider down" in (bad_h.last_error or "")
    status = runner.status()["collectors"]
    assert {c["name"]: c["last_items"] for c in status}["test.ok"] == 3
    assert runner._next_delay(bad, failed=True) <= 600


def test_runner_loop_runs_and_respects_restart(markets_settings: MarketsSettings, store: MarketsStore) -> None:
    ok = OkCollector()

    async def go() -> None:
        runner = MarketsRunner(_ctx(markets_settings, store), [ok], maintenance=False)
        await runner.start()
        for _ in range(50):
            if ok.runs:
                break
            await asyncio.sleep(0.02)
        await runner.stop()
        assert ok.runs == 1
        # restart: last_ok is fresh, so the collector is not due for another interval
        runner2 = MarketsRunner(_ctx(markets_settings, store), [ok], maintenance=False)
        due = runner2._initial_due(ok)
        assert due > datetime.now(UTC) + timedelta(minutes=50)
        assert runner2.trigger("test.ok") and not runner2.trigger("nope")

    asyncio.run(go())


# ── API ───────────────────────────────────────────────────────────────────


def _seed(store: MarketsStore) -> None:
    ref = ProviderRef(provider="nasdaq", fetched_at=NOW)
    now = datetime.now(UTC)
    items = []
    for n, (title, label, sym) in enumerate(
        [("Nvidia beats", "premium", "NVDA"), ("Apple slips", "standard", "AAPL"), ("Promo junk", "low", "TSLA")]
    ):
        url = f"https://www.cnbc.com/{n}"
        items.append(
            NewsItem(
                id=NewsItem.id_for_url(url), url=url, title=title, summary="summary text", body_text="secret body",
                publisher_id="cnbc", publisher_name="CNBC", publisher_domain="cnbc.com", publisher_tier=2,
                published_at=now - timedelta(minutes=n + 1), first_seen_at=now, feed_id="cnbc.top",
                symbols=[sym], entity_sentiment={sym: SentimentScore(label="positive", score=0.3, confidence=0.8, model="t")},
                quality_label=label, quality_score=70 - 20 * n, story_id=f"s{n}",  # type: ignore[arg-type]
            )
        )
    store.upsert_news(items)
    store.upsert_stories(
        [Story(id="s0", title="Nvidia beats", lead_item_id=items[0].id, item_ids=[items[0].id], publishers=["cnbc"],
               corroboration=1, first_seen_at=now, last_updated_at=now, symbols=["NVDA"])]
    )
    store.upsert_econ_events(
        [EconEvent(id="e1", country="US", title="CPI", scheduled_at=now + timedelta(days=1), importance=3, sources=[ref])]
    )
    store.upsert_corp_events(
        [
            CorpEvent(id="c1", type="earnings", symbol="NVDA", company="NVIDIA", date=now.date() + timedelta(days=2), sources=[ref]),
            CorpEvent(id="f1", type="filing", symbol="AAPL", company="Apple", date=now.date(), datetime_utc=now,
                      fields={"form": "8-K", "material": True, "items": [{"code": "2.02"}]}, sources=[ref]),
        ]
    )
    store.upsert_holidays(
        [Holiday(id="h1", date=now.date() + timedelta(days=3), exchange="XNYS", name="Closed", kind="market_closed", source=ref)]
    )


@pytest.fixture()
def client(markets_settings: MarketsSettings) -> TestClient:
    app = create_app(markets_settings, start_runner=False, collectors=[])
    with TestClient(app) as c:
        yield c  # type: ignore[misc]


def test_api_news_contract(client: TestClient, markets_settings: MarketsSettings) -> None:
    _seed(MarketsStore(markets_settings.store_path))
    body = client.get("/v1/markets/news").json()
    titles = [d["title"] for d in body["data"]]
    assert titles == ["Nvidia beats", "Apple slips"]  # low hidden by default
    first = body["data"][0]
    assert first["publisher_name"] == "CNBC" and first["url"].startswith("https://") and first["published_at"]
    assert "body_text" not in first and first["story_size"] == 1
    assert body["meta"]["providers"][0]["provider"] == "news.feeds"
    assert any("not collected yet" in n for n in body["meta"]["notes"])
    assert len(client.get("/v1/markets/news?min_quality=low").json()["data"]) == 3
    assert [d["title"] for d in client.get("/v1/markets/news?symbols=aapl").json()["data"]] == ["Apple slips"]
    detail = client.get(f"/v1/markets/news/{first['id']}").json()["data"]
    assert detail["story"]["publishers"][0]["name"] == "CNBC"
    assert client.get("/v1/markets/news/nope").status_code == 404
    assert client.get("/v1/markets/news?since=garbage").status_code == 422
    stories = client.get("/v1/markets/stories").json()["data"]
    assert stories[0]["lead"]["title"] == "Nvidia beats"


def test_api_calendars_and_ops(client: TestClient, markets_settings: MarketsSettings) -> None:
    _seed(MarketsStore(markets_settings.store_path))
    today = datetime.now(UTC).date()
    econ = client.get("/v1/markets/calendar/economic", params={"from": today.isoformat(), "min_importance": 3}).json()
    assert [e["title"] for e in econ["data"]] == ["CPI"]
    assert {p["provider"] for p in econ["meta"]["providers"]} >= {"econ.nasdaq", "econ.forexfactory"}
    corp = client.get("/v1/markets/calendar/corporate", params={"symbols": "nvda"}).json()
    assert corp["data"][0]["type"] == "earnings"
    filings = client.get("/v1/markets/filings", params={"forms": "8-K", "material_only": True}).json()
    assert filings["data"][0]["fields"]["form"] == "8-K"
    hol = client.get("/v1/markets/calendar/holidays", params={"exchanges": "XNYS"}).json()
    assert hol["data"][0]["kind"] == "market_closed"
    assert client.get("/v1/markets/calendar/economic", params={"from": "2026-01-10", "to": "2026-01-01"}).status_code == 422
    health = client.get("/v1/markets/health").json()["data"]
    assert health["counts"]["news"] == 3 and health["runner"]["running"] is False
    sources = client.get("/v1/markets/sources").json()["data"]
    assert sources and all("feeds" in s for s in sources)
    wl = client.post("/v1/markets/watchlist", json={"symbols": ["msft", "btc"]}).json()["data"]
    assert wl == ["MSFT", "BTC"]
    assert client.post("/v1/markets/collect/news.feeds").status_code == 409  # no runner here
    assert client.get("/healthz").json()["ok"] is True


def test_api_key_guard(markets_settings: MarketsSettings) -> None:
    settings = markets_settings.model_copy(update={"api_key": "s3cret"})
    with TestClient(create_app(settings, start_runner=False, collectors=[])) as c:
        assert c.post("/v1/markets/watchlist", json={"symbols": ["X"]}).status_code == 401
        ok = c.post("/v1/markets/watchlist", json={"symbols": ["X"]}, headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200
        assert c.get("/v1/markets/news").status_code == 200  # reads stay open on loopback


def test_holiday_model_date_field() -> None:
    h = Holiday(id="x", date=date(2026, 1, 1), name="New Year", source=ProviderRef(provider="nager", fetched_at=NOW))
    assert h.date.year == 2026


def test_sessions_endpoint_tokyo_lunch_break_and_nyse_early_close(client: TestClient) -> None:
    tokyo = client.get("/v1/markets/calendar/sessions", params={"exchange": "XTKS", "from": "2026-11-09", "to": "2026-11-13"}).json()
    rows = tokyo["data"]["sessions"]
    assert rows and tokyo["data"]["timezone"] == "Asia/Tokyo"
    assert rows[0]["open_local"] == "09:00" and rows[0]["break_local"] is not None
    nyse = client.get("/v1/markets/calendar/sessions", params={"exchange": "xnys", "from": "2026-11-23", "to": "2026-11-30"}).json()
    by_day = {r["date"]: r for r in nyse["data"]["sessions"]}
    assert "2026-11-26" not in by_day  # Thanksgiving closed
    assert by_day["2026-11-27"]["early_close"] is True and by_day["2026-11-27"]["close_local"] == "13:00"
    assert by_day["2026-11-24"]["close_local"] == "16:00" and by_day["2026-11-24"]["break_local"] is None
    assert client.get("/v1/markets/calendar/sessions", params={"exchange": "NOPE"}).status_code == 404


def test_orphan_stories_are_removed(store: MarketsStore) -> None:
    now = datetime.now(UTC)
    s_live = Story(id="live", title="t", lead_item_id="x", corroboration=1, first_seen_at=now, last_updated_at=now)
    s_orphan = Story(id="orphan", title="t", lead_item_id="y", corroboration=1, first_seen_at=now, last_updated_at=now)
    store.upsert_stories([s_live, s_orphan])
    url = "https://www.cnbc.com/z"
    store.upsert_news([NewsItem(id=NewsItem.id_for_url(url), url=url, title="Some market story title", publisher_id="cnbc",
                                publisher_name="CNBC", publisher_domain="cnbc.com", publisher_tier=2, published_at=now,
                                first_seen_at=now, feed_id="cnbc.top", quality_label="standard", story_id="live")])
    assert store.delete_orphan_stories() == 1
    assert store.get_story("live") is not None and store.get_story("orphan") is None


def test_reenrich_reapplies_current_rules(markets_settings: MarketsSettings, store: MarketsStore) -> None:
    pipe = _pipeline(markets_settings, store)
    asyncio.run(pipe.process([_raw(41, "Nvidia earnings beat estimates again", "cnbc")]))
    stored = store.recent_news(NOW - timedelta(days=2), min_quality="rejected")
    assert stored and stored[0].symbols == ["NVDA"]
    # simulate stale enrichment written by older rules
    stale = stored[0].model_copy(update={"symbols": ["WRONG"], "entities": [], "topics": []})
    store.upsert_news([stale])
    assert asyncio.run(pipe.reenrich(hours=48)) == 1
    again = store.get_news(stale.id)
    assert again is not None and again.symbols == ["NVDA"] and again.topics == ["earnings"]
    assert again.first_seen_at == stored[0].first_seen_at


def test_reenrich_repairs_stored_replacement_characters(markets_settings: MarketsSettings, store: MarketsStore) -> None:
    pipe = _pipeline(markets_settings, store)
    asyncio.run(pipe.process([_raw(42, "Nvidia earnings beat estimates again", "cnbc")]))
    stored = store.recent_news(NOW - timedelta(days=2), min_quality="rejected")[0]
    broken = stored.model_copy(update={"title": "Nvidia�s earnings beat estimates again", "summary": "Chips � up"})
    store.upsert_news([broken])
    assert asyncio.run(pipe.reenrich(hours=48)) == 1
    again = store.get_news(stored.id)
    assert again is not None
    assert again.title == "Nvidia’s earnings beat estimates again"
    assert again.summary == "Chips – up"


def test_collapsed_pagination_never_repeats_a_story(store: MarketsStore) -> None:
    base = datetime(2026, 11, 8, 10, tzinfo=UTC)

    def item(n: int, minutes: int, story: str, tier: int = 2) -> NewsItem:
        url = f"https://www.cnbc.com/p{n}"
        return NewsItem(id=NewsItem.id_for_url(url), url=url, title=f"Paged story item number {n}", publisher_id="cnbc",
                        publisher_name="CNBC", publisher_domain="cnbc.com", publisher_tier=tier,
                        published_at=base - timedelta(minutes=minutes), first_seen_at=base, feed_id="cnbc.top",
                        quality_label="standard", story_id=story)

    # story S has a lead (tier 1, newest) on page 1 and an older member that must not resurface on page 2
    store.upsert_news([item(1, 1, "S", tier=1), item(2, 2, "A"), item(3, 3, "B"), item(4, 30, "S"), item(5, 40, "C")])
    stories: list[str | None] = []
    cursor: str | None = None
    for _ in range(5):
        page, cursor = store.query_news(NewsQuery(collapse_stories=True, limit=2, cursor=cursor))
        stories.extend(i.story_id for i in page)
        if cursor is None:
            break
    assert sorted(s for s in stories if s) == ["A", "B", "C", "S"], stories


def test_cluster_candidates_exclude_body_and_rejected(store: MarketsStore) -> None:
    now = datetime.now(UTC)
    url = "https://www.cnbc.com/body"
    store.upsert_news([NewsItem(id=NewsItem.id_for_url(url), url=url, title="A story with a long body text", body_text="x" * 5000,
                                publisher_id="cnbc", publisher_name="CNBC", publisher_domain="cnbc.com", publisher_tier=2,
                                published_at=now, first_seen_at=now, feed_id="cnbc.top", quality_label="standard", story_id="s")])
    cands = store.cluster_candidates(now - timedelta(hours=1))
    assert len(cands) == 1 and cands[0].body_text is None


def test_prune_indicators_keeps_daily_snapshot(store: MarketsStore) -> None:
    ref = ProviderRef(provider="apewisdom", fetched_at=NOW)
    old = datetime(2026, 10, 1, tzinfo=UTC)
    pts = [IndicatorPoint(indicator="social.stocks.mentions:NVDA", ts=old + timedelta(hours=h), value=h, source=ref) for h in range(0, 48, 6)]
    store.upsert_indicator_points(pts)
    removed = store.prune_indicators("social.", datetime(2026, 11, 1, tzinfo=UTC), keep_daily=True)
    left = store.query_indicator("social.stocks.mentions:NVDA")
    assert removed == 6 and len(left) == 2  # one point per day kept


def test_fetcher_retry_budget_gives_up_fast(markets_settings: MarketsSettings) -> None:
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "40"}, text="rate limited")

    async def go() -> None:
        f = Fetcher(markets_settings, None, transport=httpx.MockTransport(handler), sleep=fake_sleep,
                    host_intervals={"x.test": 0.0})
        with pytest.raises(FetchError) as exc:
            await f.get("https://x.test/feed", max_retries=3)
        assert "retry budget" in exc.value.message
        await f.aclose()

    asyncio.run(go())
    assert sum(slept) <= 45.0
