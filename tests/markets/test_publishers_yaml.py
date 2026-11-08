"""Shape and policy checks for the shipped ``data/publishers.yaml`` (no network)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import pytest

from awareness.markets.config import PACKAGE_DATA_DIR
from awareness.markets.sources.registry import PublisherRegistry

PUBLISHERS_PATH = PACKAGE_DATA_DIR / "publishers.yaml"


@pytest.fixture(scope="module")
def registry() -> PublisherRegistry:
    return PublisherRegistry.load(Path(PUBLISHERS_PATH))


def test_shipped_yaml_loads(registry: PublisherRegistry) -> None:
    assert len(registry) >= 50


def test_size_targets(registry: PublisherRegistry) -> None:
    feeds = registry.feeds()
    assert len(feeds) >= 60


def test_ids_are_unique(registry: PublisherRegistry) -> None:
    publisher_ids = [p.id for p in registry.all()]
    feed_ids = [f.id for _, f in registry.feeds()]
    assert len(publisher_ids) == len(set(publisher_ids))
    assert len(feed_ids) == len(set(feed_ids))


def test_every_feed_url_is_https(registry: PublisherRegistry) -> None:
    for publisher, feed in registry.feeds():
        parts = urlsplit(feed.url)
        assert parts.scheme == "https", f"{publisher.id}/{feed.id}: {feed.url}"
        assert parts.netloc, feed.id


def test_fulltext_only_for_free_high_tier_publishers(registry: PublisherRegistry) -> None:
    for publisher in registry.all():
        if publisher.fetch_fulltext:
            assert publisher.tier <= 3, publisher.id
            assert not publisher.paywalled, publisher.id


def test_community_publishers_are_tier_5(registry: PublisherRegistry) -> None:
    community = [p for p in registry.all() if p.kind == "community"]
    assert community, "expected at least one community publisher"
    assert all(p.tier == 5 for p in community)


def test_community_feeds_poll_slowly(registry: PublisherRegistry) -> None:
    for publisher in registry.all():
        if publisher.kind == "community":
            assert all(f.poll_seconds >= 600 for f in publisher.feeds), publisher.id


def test_feed_kinds_and_categories_are_valid(registry: PublisherRegistry) -> None:
    allowed = {
        "markets", "equities", "economy", "business", "central_bank", "rates", "regulation",
        "crypto", "technology", "world", "commodities", "earnings", "social",
    }
    for _publisher, feed in registry.feeds():
        assert feed.kind in ("rss", "atom"), feed.id
        assert set(feed.categories) <= allowed, f"{feed.id}: {set(feed.categories) - allowed}"
        assert feed.poll_seconds >= 60, feed.id


def test_publisher_tiers_are_in_range(registry: PublisherRegistry) -> None:
    for publisher in registry.all():
        assert 1 <= publisher.tier <= 5, publisher.id


def test_google_news_origins_resolve_to_registered_publishers(registry: PublisherRegistry) -> None:
    for domain, expected in [
        ("https://www.reuters.com", "reuters"),
        ("https://www.cnbc.com", "cnbc"),
        ("https://www.wsj.com", "wsj"),
        ("https://www.thestreet.com", None),
        ("https://finance.yahoo.com", "yahoo_finance"),
    ]:
        pub = registry.by_domain(domain)
        assert (pub.id if pub else None) == expected, domain
