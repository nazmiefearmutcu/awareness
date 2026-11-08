"""Story clustering: title shingles, find_story, build_story, corroboration (news/cluster.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from awareness.markets.models import EntityRef, NewsItem, SentimentScore
from awareness.markets.news.cluster import StoryClusterer, title_shingles

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
    """Same entity, same day, different events: must stay two stories."""
    beats = make_news("https://cnbc.example/a", CNBC_TITLE, story_id="story-1",
                      summary="Nvidia reported quarterly results topping Wall Street's forecasts.")
    probe = make_news("https://reuters.example/c", "Nvidia faces EU antitrust probe over chips",
                      publisher_id="reuters", tier=1, published_at=NOW - timedelta(hours=1),
                      summary="Brussels opened a formal investigation into the chipmaker's AI deals.")
    assert StoryClusterer().find_story(probe, [beats]) is None


def test_entity_sharing_pair_with_moderate_jaccard_clusters() -> None:
    """Jaccard in [0.3, 0.5) plus a shared high-confidence entity is enough."""
    base = make_news("https://cnbc.example/a", CNBC_TITLE, story_id="story-1",
                     summary="Nvidia reported quarterly results topping Wall Street's forecasts.")
    other = make_news("https://marketwatch.example/b",
                      "Nvidia shares rally on data center revenue beat", publisher_id="marketwatch",
                      tier=3, published_at=NOW - timedelta(hours=1),
                      summary="Investors cheered the chipmaker's latest quarter in early trading.")
    clusterer = StoryClusterer(simhash_threshold=0)  # isolate the entity + Jaccard path
    assert clusterer.find_story(other, [base]) == "story-1"
    no_entity = other.model_copy(update={"id": "no-ent", "entities": []})
    assert clusterer.find_story(no_entity, [base]) is None


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
