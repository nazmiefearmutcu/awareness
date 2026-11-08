"""Story clustering: title shingles, find_story, build_story, corroboration (news/cluster.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from awareness.markets.models import EntityRef, NewsItem, Publisher, SentimentScore
from awareness.markets.news.cluster import StoryClusterer, publisher_names_from, title_shingles

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
NVDA = EntityRef(type="equity", id="NVDA", name="NVIDIA", confidence=0.9, mentions=4, salience=1.0)
TSLA = EntityRef(type="equity", id="TSLA", name="Tesla", confidence=0.9, mentions=2, salience=1.0)
NVDA_WEAK = EntityRef(type="equity", id="NVDA", name="NVIDIA", confidence=0.5, mentions=1, salience=0.2)
SUMMARY = (
    "Nvidia reported quarterly results above analyst expectations as data center sales from its "
    "AI accelerators climbed sharply, and management lifted its guidance for the coming quarter."
)


def make_news(
    url: str,
    title: str,
    *,
    publisher_id: str = "cnbc",
    tier: int = 2,
    published_at: datetime = NOW - timedelta(hours=3),
    summary: str | None = SUMMARY,
    entities: list[EntityRef] | None = None,
    language: str = "en",
    story_id: str | None = None,
    quality_score: float = 60.0,
    importance: float = 0.0,
    sentiment=None,  # type: ignore[no-untyped-def]
    topics: list[str] | None = None,
    symbols: list[str] | None = None,
    countries: list[str] | None = None,
    first_seen_at: datetime = NOW,
) -> NewsItem:
    return NewsItem(
        id=NewsItem.id_for_url(url),
        url=url,
        title=title,
        summary=summary,
        publisher_id=publisher_id,
        publisher_name=publisher_id.upper(),
        publisher_domain=f"{publisher_id}.example",
        publisher_tier=tier,
        published_at=published_at,
        first_seen_at=first_seen_at,
        feed_id=f"{publisher_id}_feed",
        language=language,
        entities=entities if entities is not None else [NVDA],
        topics=topics if topics is not None else ["earnings"],
        symbols=symbols or [],
        countries=countries or [],
        story_id=story_id,
        quality_score=quality_score,
        quality_label="standard",
        importance=importance,
        sentiment=sentiment,
    )


CNBC_TITLE = "Nvidia beats estimates as data center revenue soars"
WSJ_TITLE = "Nvidia tops estimates on data-center revenue surge"


# ── title normalisation ───────────────────────────────────────────────────


def test_publisher_suffix_stripped_only_for_known_publishers() -> None:
    assert title_shingles("Nvidia beats estimates - Reuters", ["Reuters"]) == title_shingles(
        "Nvidia beats estimates"
    )
    assert title_shingles("Fed holds rates | CNBC", ["CNBC"]) == title_shingles("Fed holds rates")
    # Without a registry name, a trailing " - X" segment is content, not a publisher.
    assert title_shingles("Tesla recall - 2 million vehicles") != title_shingles("Tesla recall")


def test_stopwords_english_and_turkish_removed() -> None:
    shingles = title_shingles("The Fed holds rates as inflation eases")
    assert "the" not in shingles and "as" not in shingles
    assert "fed hold" in shingles  # bigram of kept tokens (holds -> hold)
    tr = title_shingles("Merkez Bankası faiz kararını ve enflasyonu açıkladı")  # noqa: RUF001
    assert "ve" not in tr


def test_shingles_keep_numbers_and_long_unigrams_only() -> None:
    shingles = title_shingles("Apple 2026 guidance cut")
    assert "2026" in shingles  # numbers kept (length >= 4)
    assert "cut" not in shingles  # unigram shorter than 4 chars is dropped
    assert "2026 guidance" in shingles


def test_identical_titles_produce_identical_shingles() -> None:
    assert title_shingles(CNBC_TITLE) == title_shingles(CNBC_TITLE)


# ── find_story ─────────────────────────────────────────────────────────────


def test_paraphrased_syndicated_headlines_cluster() -> None:
    """Regression: the brief's exact pair, with realistic different summaries.

    Synonym folding (beats/tops -> beat, surge/soars -> rise), data-center folding and light
    stemming make the two titles identical after normalisation, so they match on title
    Jaccard alone, whatever the summaries say.
    """
    cnbc = make_news("https://cnbc.example/a", CNBC_TITLE, story_id="story-1",
                     summary="CNBC: Nvidia reported quarterly results topping Wall Street's forecasts.")
    wsj = make_news("https://wsj.example/b", WSJ_TITLE, publisher_id="wsj", tier=1,
                    published_at=NOW - timedelta(hours=2),
                    summary="Shares rose after hours as the chipmaker guided higher for the next period.")
    assert title_shingles(CNBC_TITLE) == title_shingles(WSJ_TITLE)
    assert StoryClusterer().find_story(wsj, [cnbc]) == "story-1"


def test_unrelated_nvidia_stories_same_day_do_not_merge() -> None:
    """Same entity, same day, different angles: earnings vs regulation must stay apart."""
    beats = make_news("https://cnbc.example/a", CNBC_TITLE, story_id="story-1", topics=["earnings"],
                      summary="Nvidia reported quarterly results topping Wall Street's forecasts.")
    probe = make_news("https://reuters.example/c", "Nvidia faces EU antitrust probe over chips",
                      publisher_id="reuters", tier=1, published_at=NOW - timedelta(hours=1),
                      topics=["regulation", "litigation"],
                      summary="Brussels opened a formal investigation into the chipmaker's AI deals.")
    assert StoryClusterer().find_story(probe, [beats]) is None


def test_round1_entity_band_no_longer_over_merges() -> None:
    """Round-1 rule (shared entity + Jaccard >= 0.3) is removed.

    Pairs sharing a salient entity merge only under rule (c) (shared topic, within 18 h) or
    rule (d) (a side without topics). Pairs with different topics stay separate.
    """
    clusterer = StoryClusterer(simhash_threshold=0)

    tesla_rally_beat = make_news("https://t.example/1", "Tesla shares rally after delivery beat",
                                 story_id="t1", topics=["earnings"], entities=[TSLA],
                                 summary="Tesla shares climbed after the delivery report.")
    tesla_robotaxi = make_news("https://t.example/2", "Tesla shares rally on robotaxi hopes",
                               publisher_id="wsj", tier=1, topics=["product_launch"],
                               entities=[TSLA], summary="Investors bet on autonomous rides.")
    assert clusterer.find_story(tesla_robotaxi, [tesla_rally_beat]) is None

    nvda_saudi = make_news("https://n.example/3", "Nvidia stock rallies on Saudi AI deal",
                           story_id="n3", topics=["geopolitics"], summary="A Gulf AI data deal.")
    nvda_beat = make_news("https://n.example/4", "Nvidia stock rallies after earnings beat",
                          story_id="n4", topics=["earnings"], summary="Quarterly beat lifted shares.")
    assert clusterer.find_story(nvda_saudi, [nvda_beat]) is None

    nvda_gaming = make_news("https://n.example/5", "Nvidia misses gaming revenue estimates",
                            story_id="n5", topics=["earnings"], summary="Gaming sales lagged.")
    nvda_dc = make_news("https://n.example/6", "Nvidia beats data center revenue estimates",
                        story_id="n6", topics=["earnings"], summary="Data center sales beat.",
                        published_at=NOW - timedelta(hours=2))
    # Same company, same day, same topic: intended to merge under rule (c).
    assert clusterer.find_story(nvda_dc, [nvda_gaming]) == "n5"


def test_different_figures_never_cluster() -> None:
    """Same wording, different numbers: a different event (e.g. 2 million vs 3 cars)."""
    tesla_recall = make_news("https://a.example/1", "Tesla recall - 2 million vehicles",
                             story_id="s1", entities=[TSLA])
    tesla_small = make_news("https://b.example/2", "Tesla recall - 3 cars", publisher_id="wsj",
                            tier=1, entities=[TSLA])
    assert StoryClusterer().find_story(tesla_small, [tesla_recall]) is None
    assert StoryClusterer().find_story(
        make_news("https://c.example/3", "Fed raises rates by 25 bps", publisher_id="wsj", tier=1,
                  entities=[NVDA]),
        [make_news("https://d.example/4", "Fed raises rates by 50 bps", story_id="s2",
                   entities=[NVDA])],
    ) is None


def test_synonym_and_stem_folding_aligns_paraphrases() -> None:
    assert title_shingles("Sales fall short of estimates") == title_shingles("Sales miss estimates")
    assert title_shingles("Chip stocks plunge after sales slump") == title_shingles(
        "Chip stock tumbles as sales sink"
    )
    assert title_shingles("Analysts cut forecasts") == title_shingles("Analyst cuts forecast")


def test_turkish_capital_dotted_i_is_normalised() -> None:
    shingles = title_shingles("İran faiz kararı")  # noqa: RUF001
    assert "iran faiz" in shingles  # not split into "i ran"
    assert "iran" in shingles


def test_unrelated_story_does_not_match() -> None:
    cnbc = make_news("https://cnbc.example/a", CNBC_TITLE, story_id="story-1")
    other = make_news("https://wsj.example/c", "Tesla recalls thousands of vehicles over brake software",
                      publisher_id="wsj", entities=[EntityRef(type="equity", id="TSLA", name="Tesla",
                                                             confidence=0.9, salience=1.0)],
                      topics=["product_launch"], summary="Tesla said a software update fixes brakes.")
    assert StoryClusterer().find_story(other, [cnbc]) is None


def test_outside_window_does_not_match() -> None:
    old = make_news("https://cnbc.example/a", CNBC_TITLE, story_id="story-1",
                    published_at=NOW - timedelta(hours=60))
    wsj = make_news("https://wsj.example/b", WSJ_TITLE, publisher_id="wsj", tier=1)
    assert StoryClusterer(window_hours=36).find_story(wsj, [old]) is None
    assert StoryClusterer(window_hours=72).find_story(wsj, [old]) == "story-1"


def test_different_language_does_not_match() -> None:
    cnbc = make_news("https://cnbc.example/a", CNBC_TITLE, story_id="story-1")
    tr = make_news("https://bloomberght.example/t", "Nvidia beklentileri aştı veri merkezi geliri",  # noqa: RUF001
                   publisher_id="bloomberght", language="tr", entities=[NVDA])
    assert StoryClusterer().find_story(tr, [cnbc]) is None


def test_candidates_without_story_id_and_self_are_ignored() -> None:
    unassigned = make_news("https://cnbc.example/a", CNBC_TITLE, story_id=None)
    wsj = make_news("https://wsj.example/b", WSJ_TITLE, publisher_id="wsj", tier=1)
    assert StoryClusterer().find_story(wsj, [unassigned]) is None
    assert StoryClusterer().find_story(wsj, [wsj.model_copy(update={"story_id": "s"})]) is None


def test_best_jaccard_candidate_wins() -> None:
    weak = make_news("https://a.example/1", "Nvidia shares move after earnings call", story_id="weak")
    strong = make_news("https://b.example/2", CNBC_TITLE, story_id="strong")
    wsj = make_news("https://wsj.example/3", WSJ_TITLE, publisher_id="wsj", tier=1)
    assert StoryClusterer(jaccard_threshold=0.3).find_story(wsj, [weak, strong]) == "strong"


def test_simhash_path_needs_shared_high_confidence_entity() -> None:
    # Titles share nothing and the topics differ, so only the simhash path (title+summary) can
    # match. Same-topic pairs with a shared key entity are covered by rule (c) instead.
    a = make_news("https://cnbc.example/a", "Chipmaker posts record revenue", story_id="s1",
                  summary=SUMMARY, topics=["earnings"])
    b_same_entity = make_news("https://wsj.example/b", "Semiconductor firm reports strong sales",
                              publisher_id="wsj", tier=1, summary=SUMMARY, topics=["product_launch"])
    b_no_entity = b_same_entity.model_copy(update={
        "id": "other", "entities": [NVDA_WEAK]})  # confidence < 0.8: no shared high-conf entity
    # The two title+summary simhashes differ by 14 bits: inside a looser threshold only.
    strict = StoryClusterer(jaccard_threshold=0.9, simhash_threshold=10)
    loose = StoryClusterer(jaccard_threshold=0.9, simhash_threshold=14)
    assert strict.find_story(b_same_entity, [a]) is None  # bit distance above threshold
    assert loose.find_story(b_same_entity, [a]) == "s1"
    assert loose.find_story(b_no_entity, [a]) is None  # no shared high-confidence entity


def test_build_story_picks_best_tier_lead_and_counts_distinct_tier_le_3() -> None:
    reuters = make_news("https://reuters.example/1", "Nvidia beats estimates", publisher_id="reuters",
                        tier=1, published_at=NOW - timedelta(hours=2), quality_score=70,
                        importance=55.0, topics=["earnings"], symbols=["NVDA"], countries=["US"])
    cnbc = make_news("https://cnbc.example/2", CNBC_TITLE, publisher_id="cnbc", tier=2,
                     published_at=NOW - timedelta(hours=3), quality_score=80, importance=40.0,
                     topics=["earnings", "ai"], symbols=["NVDA", "AMD"], countries=["US"])
    blog = make_news("https://blog.example/3", "Nvidia beats estimates again", publisher_id="blog",
                     tier=4, published_at=NOW - timedelta(hours=1), quality_score=30, importance=10.0)
    story = StoryClusterer().build_story([reuters, cnbc, blog], story_id="story-x")
    assert story.id == "story-x"
    assert story.lead_item_id == reuters.id  # lowest tier wins before quality
    assert story.title == "Nvidia beats estimates"
    assert story.item_ids == [cnbc.id, reuters.id, blog.id]  # ordered by published_at
    assert story.publishers == ["cnbc", "reuters", "blog"]
    assert story.corroboration == 2  # blog (tier 4) does not count
    assert story.importance == 55.0
    assert story.first_seen_at == NOW
    # last_updated_at = max(latest published_at, first_seen_at); first_seen is NOW here.
    assert story.last_updated_at == NOW
    assert story.symbols == ["NVDA", "AMD"]
    assert story.topics == ["earnings", "ai"]
    assert story.countries == ["US"]


def test_build_story_lead_breaks_ties_by_quality_then_time() -> None:
    early = make_news("https://a.example/1", "Nvidia beats estimates", publisher_id="a", tier=2,
                      published_at=NOW - timedelta(hours=5), quality_score=50)
    late_better = make_news("https://b.example/2", "Nvidia beats estimates", publisher_id="b", tier=2,
                            published_at=NOW - timedelta(hours=1), quality_score=70)
    assert StoryClusterer().build_story([early, late_better], story_id="s").lead_item_id == late_better.id


def test_build_story_aggregates_sentiment_and_handles_none() -> None:
    pos = SentimentScore(label="positive", score=0.6, confidence=0.8, model="lexicon")
    neg = SentimentScore(label="negative", score=-0.4, confidence=0.6, model="lexicon")
    a = make_news("https://a.example/1", "Nvidia beats estimates", publisher_id="a", sentiment=pos)
    b = make_news("https://b.example/2", "Nvidia beats estimates", publisher_id="b", sentiment=neg)
    story = StoryClusterer().build_story([a, b], story_id="s")
    assert story.sentiment is not None
    assert story.sentiment.score == pytest.approx(0.1)
    assert story.sentiment.label == "neutral"  # mean 0.1 is inside the ±0.15 band
    assert story.sentiment.model == "aggregate:lexicon"
    no_sent = make_news("https://c.example/3", "Nvidia beats estimates", publisher_id="c")
    assert StoryClusterer().build_story([no_sent], story_id="s2").sentiment is None


def test_corroboration_counts_distinct_publishers_tier_le_3_min_one() -> None:
    a = make_news("https://a.example/1", "x", publisher_id="a", tier=1)
    a2 = make_news("https://a.example/2", "y", publisher_id="a", tier=1)
    b = make_news("https://b.example/3", "z", publisher_id="b", tier=3)
    low = make_news("https://c.example/4", "w", publisher_id="c", tier=5)
    assert StoryClusterer.corroboration([a, a2, b, low]) == 2
    assert StoryClusterer.corroboration([low]) == 1
    assert StoryClusterer.corroboration([]) == 1


def test_new_story_id_is_stable() -> None:
    item = make_news("https://a.example/1", CNBC_TITLE)
    clusterer = StoryClusterer()
    assert clusterer.new_story_id(item) == clusterer.new_story_id(item)
    assert clusterer.new_story_id(item) != clusterer.new_story_id(make_news("https://b.example/2", CNBC_TITLE))


BRK = EntityRef(type="equity", id="BRK-B", name="Berkshire Hathaway", confidence=0.9, mentions=3,
                salience=1.0)
BERKSHIRE_HEADLINES = [
    ("https://bloomberg.example/brk", "Berkshire Hathaway's Operating Earnings Fall, Buybacks Rise",
     "bloomberg", 1, ["earnings", "dividends_buybacks"]),
    ("https://cnbc.example/brk",
     "Berkshire Hathaway more than doubles buybacks in third quarter as earnings decline",
     "cnbc", 2, ["dividends_buybacks"]),
    ("https://wsj.example/brk", "Berkshire Hathaway's Quarterly Profit Falls After Investment Gains Shrink",
     "wsj", 1, ["earnings"]),
    ("https://reuters.example/brk", "Berkshire Hathaway speeds up stock buybacks, though profit falls",
     "reuters", 1, ["earnings", "dividends_buybacks"]),
    ("https://ap.example/brk",
     "Berkshire Hathaway buys back $9.6 billion of its own stock but its profits decline",
     "ap", 1, []),  # no topics: exercises rule (d)
    ("https://ft.example/brk", "Berkshire Hathaway steps up share buybacks with $9.6bn of purchases",
     "ft", 1, ["dividends_buybacks"]),
]


def cluster_in_order(items: list[NewsItem]) -> list[str]:
    """Assign story ids the way the pipeline does: find_story against earlier items, else new."""
    clusterer = StoryClusterer(publisher_names=["Reuters", "CNBC", "Bloomberg", "AP", "WSJ", "FT"])
    assigned: list[NewsItem] = []
    story_ids: list[str] = []
    for item in items:
        story_id = clusterer.find_story(item, assigned) or clusterer.new_story_id(item)
        assigned.append(item.model_copy(update={"story_id": story_id}))
        story_ids.append(story_id)
    return story_ids


def test_berkshire_six_outlets_cluster_into_one_story() -> None:
    items = []
    for offset, (url, title, publisher, tier, topics) in enumerate(BERKSHIRE_HEADLINES):
        items.append(make_news(url, title, publisher_id=publisher, tier=tier, topics=topics,
                               entities=[BRK], published_at=NOW - timedelta(hours=6 - offset),
                               summary=f"Berkshire Hathaway quarterly update from {publisher}."))
    story_ids = cluster_in_order(items)
    assert len(set(story_ids)) == 1, story_ids


def test_microsoft_oracle_deal_headlines_cluster() -> None:
    msft = EntityRef(type="equity", id="MSFT", name="Microsoft", confidence=0.9, mentions=2, salience=1.0)
    orcl = EntityRef(type="equity", id="ORCL", name="Oracle", confidence=0.9, mentions=2, salience=1.0)
    reuters = make_news("https://reuters.example/msft",
                        "Microsoft is winding down Oracle deal to rent AI cloud capacity, Bloomberg News reports",
                        publisher_id="reuters", tier=1, topics=["m_and_a"], entities=[msft, orcl],
                        published_at=NOW - timedelta(hours=5))
    barrons = make_news("https://barrons.example/msft",
                        "Microsoft Won't Renew Deal for Renting AI Cloud Capacity From Oracle: Report",
                        publisher_id="barrons", tier=3, topics=["m_and_a"], entities=[msft, orcl],
                        published_at=NOW - timedelta(hours=3))
    assert len(set(cluster_in_order([reuters, barrons]))) == 1


def test_find_story_strips_publisher_suffix_only_for_known_names() -> None:
    """Suffix restriction at the find_story level, with no digits and no shared entity."""
    cand = make_news("https://a.example/1", "Oil slips", story_id="s1", entities=[], topics=[])
    item = make_news("https://b.example/2", "Oil slips - Wall Street Journal", entities=[], topics=[])
    assert StoryClusterer().find_story(item, [cand]) is None
    named = StoryClusterer(publisher_names=["Wall Street Journal"])
    assert named.find_story(item, [cand]) == "s1"


def test_publisher_names_from_registry_publishers() -> None:
    wsj = Publisher(id="wsj", name="Wall Street Journal", domain="wsj.com", tier=1,
                    kind="financial_press", aliases=["WSJ"])
    names = publisher_names_from([wsj])
    assert "Wall Street Journal" in names and "WSJ" in names


def test_amount_and_synonym_folding() -> None:
    base = title_shingles("Berkshire buys back $9.6 billion of stock")
    assert base == title_shingles("Berkshire buys back $9.6bn of stock")
    assert base == title_shingles("Berkshire buys back $9.6B of stock")
    assert title_shingles("Nvidia estimated revenue") == title_shingles("Nvidia estimate revenue")
    assert title_shingles("Firm repurchases stock") == title_shingles("Firm buyback stock")
    assert title_shingles("Company profits fall") == title_shingles("Company earnings fall")
    assert title_shingles("Fed hikes by 25 bps") != title_shingles("Fed hikes by 50 bps")
