"""TaxonomyClassifier tests — English and Turkish headlines, no network."""

from __future__ import annotations

import pytest

from awareness.markets.models import EntityRef, Publisher
from awareness.markets.nlp.taxonomy import TaxonomyClassifier, TaxonomyResult


@pytest.fixture(scope="module")
def clf() -> TaxonomyClassifier:
    return TaxonomyClassifier()


def _ent(etype: str, eid: str, conf: float = 0.95, salience: float = 1.0) -> EntityRef:
    return EntityRef(type=etype, id=eid, name=eid, confidence=conf, salience=salience)  # type: ignore[arg-type]


def _publisher(kind: str) -> Publisher:
    return Publisher(id="p", name="P", domain="p.example", tier=2, kind=kind)  # type: ignore[arg-type]


def test_earnings_guidance_headline(clf: TaxonomyClassifier) -> None:
    result = clf.classify(
        "Nvidia shares jump after earnings beat; (NASDAQ: NVDA) guidance raised",
        None,
        [_ent("equity", "NVDA", 0.99)],
    )
    assert isinstance(result, TaxonomyResult)
    assert "earnings" in result.topics
    assert "guidance" in result.topics
    assert "equities" in result.asset_classes
    assert result.finance_relevance >= 0.7


def test_fed_inflation_headline_is_rates_and_macro(clf: TaxonomyClassifier) -> None:
    result = clf.classify(
        "Fed's Powell signals rate cut as inflation cools",
        None,
        [_ent("central_bank", "FED"), _ent("person", "POWELL"), _ent("country", "US", 0.7)],
    )
    assert "central_bank" in result.topics
    assert "inflation" in result.topics
    assert "rates" in result.asset_classes
    assert "macro" in result.asset_classes


def test_brent_headline_is_energy_and_commodities(clf: TaxonomyClassifier) -> None:
    result = clf.classify(
        "Brent crude rises on OPEC+ cut",
        None,
        [_ent("commodity", "BRENT", 0.9)],
    )
    assert "energy" in result.topics
    assert "commodities" in result.asset_classes


def test_turkish_central_bank_headline(clf: TaxonomyClassifier) -> None:
    result = clf.classify(
        "Merkez Bankas\u0131 faizi sabit tuttu, BIST 100 yükseldi; THYAO hisseleri %3 artt\u0131",
        None,
        [_ent("central_bank", "TCMB"), _ent("index", "XU100"), _ent("equity", "THYAO.IS")],
    )
    assert "central_bank" in result.topics
    assert "equities" in result.asset_classes


def test_celebrity_and_sports_headlines_are_low_relevance(clf: TaxonomyClassifier) -> None:
    celeb = clf.classify("Taylor Swift steps out on red carpet at the Oscars", None, [])
    sport = clf.classify("Real Madrid wins league match 3-1 at home", None, [])
    assert celeb.finance_relevance < 0.3
    assert sport.finance_relevance < 0.3


def test_content_type_opinion_social_and_press_release(clf: TaxonomyClassifier) -> None:
    assert clf.classify("Opinion: The Fed is behind the curve", None, []).content_type_hint == "opinion"
    assert (
        clf.classify("Stocks rally", None, [], publisher=_publisher("community")).content_type_hint
        == "social"
    )
    assert (
        clf.classify("Company announces buyback", None, [], publisher=_publisher("press_release"))
        .content_type_hint
        == "press_release"
    )
    assert (
        clf.classify("Rate decision published", None, [], publisher=_publisher("official"))
        .content_type_hint
        == "official"
    )
    assert clf.classify("Analysis: why bonds sold off", None, []).content_type_hint == "analysis"
    assert clf.classify("Plain headline about shares", None, []).content_type_hint is None


def test_horizon_intraday_one_week_structural(clf: TaxonomyClassifier) -> None:
    assert clf.classify("Flash: Stocks plunge after trading halt", None, []).horizon == "intraday"
    assert clf.classify("Company reports quarterly earnings beat", None, []).horizon == "one_week"
    assert clf.classify("EU adopts new data privacy regulation", None, []).horizon == "structural"


def test_topics_are_capped_at_four_and_ordered(clf: TaxonomyClassifier) -> None:
    result = clf.classify(
        "Earnings beat, guidance raised, dividend hiked, buyback expanded, IPO planned, "
        "merger talks and analyst upgrade",
        None,
        [],
    )
    assert len(result.topics) <= 4
    scores = [result.topic_scores[t] for t in result.topics]
    assert scores == sorted(scores, reverse=True)


def test_relevance_is_clamped_and_publisher_adjusts(clf: TaxonomyClassifier) -> None:
    base = clf.classify("Company posts earnings", None, [_ent("equity", "XYZ", 0.9)])
    official = clf.classify(
        "Company posts earnings", None, [_ent("equity", "XYZ", 0.9)],
        publisher=_publisher("financial_press"),
    )
    community = clf.classify(
        "Company posts earnings", None, [_ent("equity", "XYZ", 0.9)],
        publisher=_publisher("community"),
    )
    assert official.finance_relevance >= base.finance_relevance >= community.finance_relevance
    assert 0.0 <= community.finance_relevance <= 1.0
    assert 0.0 <= official.finance_relevance <= 1.0


def test_feed_category_adds_relevance(clf: TaxonomyClassifier) -> None:
    plain = clf.classify("Quiet day in town", None, [])
    marked = clf.classify("Quiet day in town", None, [], feed_categories=["markets"])
    assert marked.finance_relevance > plain.finance_relevance


@pytest.mark.parametrize(
    ("headline", "etype", "eid"),
    [
        ("Apple Mac sales surge as shares rise", "equity", "AAPL"),
        ("Olympic Steel raises guidance as shares rise", "equity", "SOS"),
        ("Divorce of two funds pressures shares of Meta", "equity", "META"),
        ("Company engaged in merger talks, shares jump", "equity", "XYZ"),
    ],
)
def test_negative_words_do_not_suppress_real_finance_items(
    clf: TaxonomyClassifier, headline: str, etype: str, eid: str,
) -> None:
    result = clf.classify(headline, None, [_ent(etype, eid, 0.95)])
    assert result.finance_relevance >= 0.4, headline


def test_fed_up_is_not_a_central_bank_topic(clf: TaxonomyClassifier) -> None:
    result = clf.classify("Fed up with delays: shares fall", None, [])
    assert "central_bank" not in result.topics


def test_real_negative_still_suppresses_without_finance_entity(clf: TaxonomyClassifier) -> None:
    result = clf.classify("Taylor Swift attends the Oscars red carpet", None, [])
    assert result.finance_relevance == 0.0


# ── salient-only finance evidence (final review wave B) ───────────────────


def test_body_only_finance_mentions_do_not_lift_relevance(clf: TaxonomyClassifier) -> None:
    result = clf.classify(
        "31-year-old dumpster diver brought in $140,000 from online sales",
        "Shares of META rose this year and the dollar was strong in the background.",
        [_ent("equity", "META", 0.95, salience=0.45), _ent("fx", "USD", 0.9, salience=0.45)],
        publisher=_publisher("financial_press"),
        feed_categories=["markets"],
    )
    assert result.finance_relevance <= 0.1 + 1e-9


def test_offtopic_trump_headline_has_low_relevance(clf: TaxonomyClassifier) -> None:
    result = clf.classify(
        "Trump offers UFC chief Dana White press secretary job",
        None,
        [_ent("person", "TRUMP", 0.9), _ent("country", "US", 0.85)],
        publisher=_publisher("financial_press"),
    )
    assert result.finance_relevance < 0.45


def test_salient_finance_entity_still_counts(clf: TaxonomyClassifier) -> None:
    result = clf.classify("Company posts results", None, [_ent("equity", "XYZ", 0.95, salience=1.0)])
    assert result.finance_relevance >= 0.4


def test_publisher_and_feed_priors_add_at_most_point_one(clf: TaxonomyClassifier) -> None:
    both = clf.classify("Quiet day in town", None, [], publisher=_publisher("financial_press"),
                        feed_categories=["markets"])
    assert both.finance_relevance == pytest.approx(0.1)
    penalised = clf.classify("Quiet day in town", None, [], publisher=_publisher("community"))
    assert penalised.finance_relevance == 0.0


def test_negative_class_is_not_cancelled_by_body_only_entity(clf: TaxonomyClassifier) -> None:
    result = clf.classify(
        "Taylor Swift attends the Oscars red carpet",
        "The singer later mentioned the dollar in an interview.",
        [_ent("fx", "USD", 0.9, salience=0.45)],
    )
    assert result.finance_relevance == 0.0
