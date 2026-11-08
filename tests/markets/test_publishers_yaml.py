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
            assert all(f.poll_seconds >= 900 for f in publisher.feeds), publisher.id


@pytest.mark.parametrize(
    ("publisher_id", "tier"),
    [
        # tier 1: official sources and top wires
        ("federal_reserve", 1), ("ecb", 1), ("bank_of_england", 1), ("bank_of_japan", 1),
        ("bis", 1), ("sec", 1), ("bea", 1), ("us_treasury", 1), ("reuters", 1), ("ap", 1),
        # tier 2: major financial press
        ("barrons", 2), ("wsj", 2), ("ft", 2), ("cnbc", 2),
        # tier 3: general, regional and specialist
        ("axios", 3), ("fortune", 3), ("business_insider", 3), ("coindesk", 3),
        # tier 4: aggregators, PR wires, contributor and promo-prone platforms
        ("forbes", 4), ("motley_fool", 4), ("investorplace", 4), ("benzinga", 4),
        ("mining_com", 4), ("oilprice", 4), ("yahoo_finance", 4), ("prnewswire", 4),
    ],
)
def test_publisher_tier_policy(registry: PublisherRegistry, publisher_id: str, tier: int) -> None:
    publisher = registry.get(publisher_id)
    assert publisher is not None, publisher_id
    assert publisher.tier == tier


def test_no_promo_or_community_source_is_in_the_high_tiers(registry: PublisherRegistry) -> None:
    for publisher in registry.all():
        if publisher.kind in ("community", "press_release"):
            assert publisher.tier >= 4, publisher.id


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
