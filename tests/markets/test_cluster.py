"""Story clustering: title shingles, find_story, build_story, corroboration (news/cluster.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from awareness.markets.models import EntityRef, NewsItem, SentimentScore
from awareness.markets.news.cluster import StoryClusterer, title_shingles

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
NVDA = EntityRef(type="equity", id="NVDA", name="NVIDIA", confidence=0.9, mentions=4, salience=1.0)
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
        topics=topics or ["earnings"],
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


def test_publisher_suffix_is_stripped_from_shingles() -> None:
    assert title_shingles("Nvidia beats estimates - Reuters") == title_shingles("Nvidia beats estimates")
    assert title_shingles("Fed holds rates | CNBC") == title_shingles("Fed holds rates")


def test_stopwords_english_and_turkish_removed() -> None:
    shingles = title_shingles("The Fed holds rates as inflation eases")
    assert "the" not in shingles and "as" not in shingles
    assert "fed holds" in shingles  # bigram of kept tokens
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


def test_syndicated_titles_match_same_story() -> None:
    cnbc = make_news("https://cnbc.example/a", CNBC_TITLE, story_id="story-1")
    wsj = make_news("https://wsj.example/b", WSJ_TITLE, publisher_id="wsj", tier=1,
                    published_at=NOW - timedelta(hours=2))
    assert StoryClusterer().find_story(wsj, [cnbc]) == "story-1"


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
    # Titles share nothing, so only the simhash path (title+summary) can match.
    a = make_news("https://cnbc.example/a", "Chipmaker posts record revenue", story_id="s1",
                  summary=SUMMARY)
    b_same_entity = make_news("https://wsj.example/b", "Semiconductor firm reports strong sales",
                              publisher_id="wsj", tier=1, summary=SUMMARY)
    b_no_entity = b_same_entity.model_copy(update={
        "id": "other", "entities": [NVDA_WEAK]})  # confidence < 0.8: no shared high-conf entity
    # The two title+summary simhashes differ by 14 bits: inside a looser threshold only.
    strict = StoryClusterer(jaccard_threshold=0.9, simhash_threshold=10)
    loose = StoryClusterer(jaccard_threshold=0.9, simhash_threshold=14)
    assert strict.find_story(b_same_entity, [a]) is None  # bit distance above threshold
    assert loose.find_story(b_same_entity, [a]) == "s1"
    assert loose.find_story(b_no_entity, [a]) is None  # no shared high-confidence entity


# ── build_story and corroboration ─────────────────────────────────────────


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
