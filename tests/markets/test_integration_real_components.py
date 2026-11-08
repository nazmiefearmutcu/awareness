"""End-to-end with the REAL components (no fakes): recorded RSS → feeds parser → pipeline
(entity linker, taxonomy, lexicon sentiment, quality gate, clustering) → store → HTTP API.

Guards the owner's acceptance bar on recorded data: nothing visible without publisher,
canonical URL and UTC publish time; low/rejected items hidden by default; enrichment present.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from awareness.markets.app import create_app
from awareness.markets.config import MarketsSettings
from awareness.markets.news.feeds import parse_feed
from awareness.markets.news.pipeline import NewsPipeline
from awareness.markets.sources.registry import PublisherRegistry
from awareness.markets.store import MarketsStore, NewsQuery

FEED_FIXTURES = {
    "cnbc.top": "rss_cnbc_top.xml",
    "marketwatch.top": "rss_mw_top.xml",
    "wsj.markets": "rss_wsj_markets.xml",
    "bloomberg.markets": "rss_bloomberg_markets.xml",
    "ft.home": "rss_ft_home.xml",
    "bbc.business": "rss_bbc_business.xml",
    "coindesk.all": "rss_coindesk.xml",
    "fed.press_all": "rss_fed_press.xml",
}


@pytest.fixture()
def settings(tmp_path) -> MarketsSettings:  # type: ignore[no-untyped-def]
    return MarketsSettings(
        data_dir=tmp_path / "markets", autostart_runner=False, sentiment_backend="lexicon", fulltext=False,
        max_item_age_days=36500,  # recorded fixtures age; the freshness rule has its own unit tests
    )


def _raws(registry: PublisherRegistry, fixture_bytes, fetched_at: datetime):  # type: ignore[no-untyped-def]
    feeds = {f.id: (p, f) for p, f in registry.feeds()}
    out = []
    for feed_id, name in FEED_FIXTURES.items():
        if feed_id not in feeds:
            continue
        publisher, feed = feeds[feed_id]
        out.extend(parse_feed(fixture_bytes(name), feed=feed, publisher=publisher, fetched_at=fetched_at))
    return out


def test_recorded_feeds_through_the_real_pipeline(settings: MarketsSettings, fixture_bytes) -> None:  # type: ignore[no-untyped-def]
    registry = PublisherRegistry.load(settings.config_file("publishers.yaml"))
    store = MarketsStore(settings.store_path)
    pipeline = NewsPipeline.build(settings=settings, store=store, registry=registry, fetcher=None)
    raws = _raws(registry, fixture_bytes, datetime.now(UTC))
    assert len(raws) >= 40, "recorded feeds should yield plenty of items"

    stored = asyncio.run(pipeline.process(raws))
    assert stored, "pipeline stored nothing"
    # idempotent: the same batch again stores nothing new
    assert asyncio.run(pipeline.process(raws)) == []

    visible, _ = store.query_news(NewsQuery(limit=500, min_quality="standard"))
    assert visible, "no item passed the quality floor"
    for item in visible:
        assert item.publisher_name and item.publisher_id in {p.id for p in registry.all()}
        assert item.url.startswith(("http://", "https://"))
        assert item.published_at.tzinfo is not None
        assert item.quality_label in {"premium", "standard"}
        assert item.sentiment is not None and item.sentiment.model.startswith("lexicon:")
        assert item.quality_reasons, "every verdict must be explainable"
    # enrichment actually happened on real headlines
    assert any(i.symbols for i in visible)
    assert any(i.topics for i in visible)
    assert any(i.countries for i in visible)
    # official sources are typed as official
    fed = [i for i in stored if i.publisher_id == "federal_reserve"]
    assert fed and all(i.content_type == "official" for i in fed if i.quality_label != "rejected")
    store.close()


def test_api_serves_only_sourced_items(settings: MarketsSettings, fixture_bytes) -> None:  # type: ignore[no-untyped-def]
    registry = PublisherRegistry.load(settings.config_file("publishers.yaml"))
    store = MarketsStore(settings.store_path)
    pipeline = NewsPipeline.build(settings=settings, store=store, registry=registry, fetcher=None)
    asyncio.run(pipeline.process(_raws(registry, fixture_bytes, datetime.now(UTC))))
    store.close()
    with TestClient(create_app(settings, start_runner=False, collectors=[])) as client:
        since = (datetime.now(UTC) - timedelta(days=36500)).date().isoformat()
        body = client.get("/v1/markets/news", params={"limit": 200, "since": since}).json()
        assert body["data"], body["meta"]
        for row in body["data"]:
            assert row["publisher_name"] and row["url"] and row["published_at"]
            assert row["quality_label"] in {"premium", "standard"}
            assert "body_text" not in row
        everything = client.get("/v1/markets/news", params={"limit": 200, "since": since, "min_quality": "rejected", "story": "false"}).json()
        assert len(everything["data"]) >= len(body["data"])
