"""Quality gate, content-type classification and importance score (news/quality.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from awareness.markets.models import EntityRef, NewsItem, Publisher, stable_id
from awareness.markets.news.quality import QualityGate, compute_importance

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
PUBLISHED = datetime(2026, 11, 8, 9, 30, tzinfo=UTC)  # non-midnight feed time
TITLE = "Nvidia beats earnings estimates as data center revenue soars"
SUMMARY_200 = (
    "Nvidia reported quarterly results that topped Wall Street expectations as data center "
    "sales from its AI accelerators climbed sharply, and management raised its outlook."
)
NVDA = EntityRef(type="equity", id="NVDA", name="NVIDIA", confidence=0.9, mentions=3, salience=1.0)


def make_publisher(**overrides: object) -> Publisher:
    data: dict[str, object] = dict(
        id="cnbc", name="CNBC", domain="cnbc.com", tier=2, kind="financial_press"
    )
    data.update(overrides)
    return Publisher(**data)  # type: ignore[arg-type]


def make_item(**overrides: object) -> NewsItem:
    url = str(overrides.pop("url", "https://www.cnbc.com/2026/11/08/nvda-earnings.html"))
    data: dict[str, object] = dict(
        id=NewsItem.id_for_url(url),
        url=url,
        title=TITLE,
        summary=SUMMARY_200,
        publisher_id="cnbc",
        publisher_name="CNBC",
        publisher_domain="cnbc.com",
        publisher_tier=2,
        published_at=PUBLISHED,
        first_seen_at=NOW,
        feed_id="cnbc_top",
        entities=[NVDA],
        topics=["earnings"],
        quality_label="standard",  # enriched item: already gated (NewsItem default is "low")
    )
    data.update(overrides)
    return NewsItem(**data)  # type: ignore[arg-type]


def evaluate(item: NewsItem, publisher: Publisher | None, **kwargs: object):  # type: ignore[no-untyped-def]
    params: dict[str, object] = dict(now=NOW, finance_relevance=0.9)
    params.update(kwargs)
    return QualityGate().evaluate(item, publisher, **params)  # type: ignore[arg-type]


# ── hard rejects ──────────────────────────────────────────────────────────


def test_unknown_publisher_is_hard_rejected() -> None:
    verdict = evaluate(make_item(), None)
    assert verdict.hard_reject is True
    assert verdict.label == "rejected"
    assert verdict.score == 0.0
    assert "unknown_publisher" in verdict.reasons


def test_missing_url_is_rejected() -> None:
    verdict = evaluate(make_item(url="ftp://example.com/x", id=stable_id("n", "ftp")), make_publisher())
    assert verdict.hard_reject
    assert "missing_url" in verdict.reasons


def test_missing_title_is_rejected() -> None:
    verdict = evaluate(make_item(title="   "), make_publisher())
    assert "missing_title" in verdict.reasons
    assert "title_too_short" in verdict.reasons


def test_fetched_timestamp_is_rejected() -> None:
    verdict = evaluate(make_item(), make_publisher(), timestamp_source="fetched")
    assert verdict.hard_reject
    assert "missing_timestamp" in verdict.reasons


def test_stale_item_is_rejected() -> None:
    item = make_item(published_at=NOW - timedelta(days=8))
    verdict = evaluate(item, make_publisher())
    assert "stale" in verdict.reasons


def test_stale_window_is_configurable() -> None:
    item = make_item(published_at=NOW - timedelta(days=8))
    verdict = QualityGate(max_age_days=30).evaluate(
        item, make_publisher(), now=NOW, finance_relevance=0.9
    )
    assert "stale" not in verdict.reasons


def test_future_timestamp_is_rejected() -> None:
    item = make_item(published_at=NOW + timedelta(days=2))
    verdict = evaluate(item, make_publisher())
    assert "future_timestamp" in verdict.reasons


def test_short_title_is_rejected() -> None:
    verdict = evaluate(make_item(title="Short headline"), make_publisher())
    assert "title_too_short" in verdict.reasons


def test_non_finance_is_rejected_for_general_press() -> None:
    verdict = evaluate(make_item(), make_publisher(), finance_relevance=0.1)
    assert "non_finance" in verdict.reasons


def test_official_publisher_is_exempt_from_non_finance() -> None:
    publisher = make_publisher(id="fed", name="Federal Reserve", domain="federalreserve.gov",
                               tier=1, kind="official")
    verdict = evaluate(make_item(), publisher, finance_relevance=0.1)
    assert "non_finance" not in verdict.reasons


def test_community_low_signal_is_rejected() -> None:
    publisher = make_publisher(id="reddit", name="Reddit", domain="reddit.com", tier=5,
                               kind="community")
    verdict = evaluate(make_item(), publisher, finance_relevance=0.3)
    assert "community_low_signal" in verdict.reasons
    # item.content_type is "news" (the pipeline sets it from the verdict), so non_finance applies too
    verdict_low = evaluate(make_item(), publisher, finance_relevance=0.1)
    assert {"non_finance", "community_low_signal"} <= set(verdict_low.reasons)


def test_social_items_are_exempt_from_non_finance() -> None:
    publisher = make_publisher(id="reddit", name="Reddit", domain="reddit.com", tier=5,
                               kind="community")
    verdict = evaluate(make_item(content_type="social"), publisher, finance_relevance=0.1)
    assert "non_finance" not in verdict.reasons
    assert "community_low_signal" in verdict.reasons


def test_multiple_reject_reasons_accumulate() -> None:
    item = make_item(title="Too short", published_at=NOW - timedelta(days=30))
    verdict = evaluate(item, None)
    assert {"unknown_publisher", "stale", "title_too_short"} <= set(verdict.reasons)
    assert verdict.score == 0.0


# ── score and labels ──────────────────────────────────────────────────────


def test_tier2_cnbc_with_entity_and_topic_is_premium_with_reasons() -> None:
    verdict = evaluate(make_item(), make_publisher())
    assert verdict.hard_reject is False
    assert verdict.label == "premium"
    assert verdict.score == pytest.approx(79.0)  # 61 + relevance 0.9 x 20
    assert {"tier2:+38", "relevance:+18", "depth_summary140:+8", "time_precise:+3",
            "entity_high_conf:+7", "topic:+5"} <= set(verdict.reasons)


def test_clickbait_tier4_is_low_even_with_entities() -> None:
    item = make_item(
        title="3 Stocks to Buy Before They Soar 300%",
        summary="A short market note about three names that analysts keep discussing today.",
        topics=["earnings"],
    )
    verdict = evaluate(item, make_publisher(id="zacks", name="Zacks", domain="zacks.com",
                                            tier=4, kind="aggregator"))
    assert verdict.label == "low"
    assert "clickbait:-25" in verdict.reasons
    assert "tier4:+15" in verdict.reasons


def test_official_release_with_short_summary_is_at_least_standard() -> None:
    publisher = make_publisher(id="fed", name="Federal Reserve", domain="federalreserve.gov",
                               tier=1, kind="official")
    item = make_item(
        title="FEDERAL RESERVE ISSUES FOMC STATEMENT",
        summary="Statement.",
        publisher_id="fed", publisher_name="Federal Reserve",
        publisher_domain="federalreserve.gov", publisher_tier=1,
        entities=[], topics=[], content_type="official",
        published_at=datetime(2026, 11, 8, 0, 0, tzinfo=UTC),
    )
    verdict = evaluate(item, publisher, timestamp_source="feed", finance_relevance=0.3)
    assert verdict.score < 40  # raw score would be low ...
    assert verdict.label == "standard"  # ... but official publishers are floored
    assert "all_caps_title:-10" in verdict.reasons
    assert "official_floor" in verdict.reasons


def test_paywalled_publisher_without_summary_is_penalised() -> None:
    publisher = make_publisher(id="bloomberg", name="Bloomberg", domain="bloomberg.com",
                               tier=2, kind="financial_press", paywalled=True)
    verdict = evaluate(make_item(summary=None), publisher)
    assert "paywalled_no_summary:-10" in verdict.reasons


def test_press_release_is_penalised_unless_issuer_equity_matches() -> None:
    publisher = make_publisher(id="prn", name="PR Newswire", domain="prnewswire.com",
                               tier=4, kind="press_release")
    no_issuer = evaluate(make_item(entities=[], topics=[], title="Acme Corp announces record quarterly revenue"),
                         publisher)
    assert "press_release:-10" in no_issuer.reasons
    with_issuer = evaluate(make_item(title="Acme Corp announces record quarterly revenue"), publisher)
    assert "press_release:-10" not in with_issuer.reasons


def test_opinion_is_penalised() -> None:
    verdict = evaluate(make_item(title="Opinion: Why the central bank keeps getting rates wrong"),
                       make_publisher())
    assert verdict.content_type == "opinion"
    assert "opinion:-5" in verdict.reasons


def test_question_title_without_entity_is_penalised() -> None:
    verdict = evaluate(make_item(title="Is the central bank about to cut rates again?", entities=[]),
                       make_publisher())
    assert "question_no_entity:-5" in verdict.reasons


def test_boilerplate_summary_is_penalised() -> None:
    verdict = evaluate(make_item(summary="Read more"), make_publisher())
    assert "boilerplate_summary:-3" in verdict.reasons


def test_corroboration_bonus_is_capped() -> None:
    assert "corroboration:+10" in evaluate(make_item(), make_publisher(), corroboration=3).reasons
    assert "corroboration:+15" in evaluate(make_item(), make_publisher(), corroboration=10).reasons


def test_author_adds_attribution() -> None:
    verdict = evaluate(make_item(author="Jane Doe"), make_publisher())
    assert "author:+5" in verdict.reasons


def test_custom_rules_file_is_used(tmp_path: Path) -> None:
    rules = tmp_path / "rules.yaml"
    rules.write_text(
        'clickbait:\n  title:\n    - "zzz-special"\n  summary: []\n'
        "mega_cap_equities: []\nkey_countries: []\n",
        encoding="utf-8",
    )
    gate = QualityGate(rules_path=rules)
    item = make_item(title="zzz-special report lists Nvidia among the top names of the day")
    verdict = gate.evaluate(item, make_publisher(), now=NOW, finance_relevance=0.9)
    assert "clickbait:-25" in verdict.reasons


def test_default_rules_load() -> None:
    QualityGate()  # must not raise


# ── content type classification ───────────────────────────────────────────


def test_classify_official_and_press_and_social() -> None:
    gate = QualityGate()
    item = make_item()
    fed = make_publisher(id="fed", domain="federalreserve.gov", tier=1, kind="official")
    assert gate.classify_content_type(item, fed) == "official"
    prn = make_publisher(id="prn", domain="prnewswire.com", tier=4, kind="press_release")
    assert gate.classify_content_type(item, prn) == "press_release"
    reddit = make_publisher(id="reddit", domain="reddit.com", tier=5, kind="community")
    assert gate.classify_content_type(item, reddit) == "social"


def test_classify_title_prefixes() -> None:
    gate = QualityGate()
    pub = make_publisher()
    assert gate.classify_content_type(make_item(title="Editorial: The bank must act now"), pub) == "opinion"
    assert gate.classify_content_type(make_item(title="Column: Markets brace for the print"), pub) == "opinion"
    assert gate.classify_content_type(make_item(title="Explainer: how bond yields move stocks"), pub) == "analysis"
    assert gate.classify_content_type(make_item(title="Analysis: Nvidia's margin squeeze"), pub) == "analysis"


def test_classify_seeking_alpha_article_is_opinion() -> None:
    sa = make_publisher(id="seeking_alpha", name="Seeking Alpha", domain="seekingalpha.com",
                        tier=4, kind="specialist")
    item = make_item(url="https://seekingalpha.com/article/4800000-nvidia-looks-cheap",
                     title="Nvidia looks cheap after the selloff")
    assert QualityGate().classify_content_type(item, sa) == "opinion"
    news = make_item(url="https://seekingalpha.com/news/4800001-nvidia", title="Nvidia looks cheap after the selloff")
    assert QualityGate().classify_content_type(news, sa) == "news"


def test_content_type_hint_wins_only_when_valid() -> None:
    gate = QualityGate()
    pub = make_publisher()
    assert gate.classify_content_type(make_item(), pub, "analysis") == "analysis"
    assert gate.classify_content_type(make_item(), pub, "bogus") == "news"


def test_evaluate_returns_content_type_used() -> None:
    verdict = evaluate(make_item(), make_publisher(), content_type_hint="analysis")
    assert verdict.content_type == "analysis"


# ── importance ────────────────────────────────────────────────────────────


def _fed_item() -> NewsItem:
    fed_entity = EntityRef(type="central_bank", id="FED", name="Federal Reserve", confidence=1.0,
                           salience=1.0)
    return make_item(
        title="Fed holds policy rate steady and signals patience on cuts",
        publisher_id="fed", publisher_name="Federal Reserve", publisher_domain="federalreserve.gov",
        publisher_tier=1, content_type="official", entities=[fed_entity], topics=["central_bank"],
    )


def _celebrity_item() -> NewsItem:
    return make_item(
        title="Famous actor spotted at a Hollywood premiere this weekend",
        publisher_id="gossip", publisher_name="Gossip Daily", publisher_domain="gossip.example",
        publisher_tier=3, entities=[], topics=["other_topic"], content_type="news",
    )


def test_fed_rate_decision_outranks_celebrity_item() -> None:
    assert compute_importance(_fed_item()) == pytest.approx(80.0)
    assert compute_importance(_fed_item()) > compute_importance(_celebrity_item())
    assert compute_importance(_celebrity_item()) == pytest.approx(20.0)


def test_corroboration_raises_importance() -> None:
    base = compute_importance(_fed_item(), corroboration=1)
    assert compute_importance(_fed_item(), corroboration=2) == pytest.approx(base + 5)
    assert compute_importance(_fed_item(), corroboration=10) == pytest.approx(base + 20)


def test_low_label_scales_importance_down() -> None:
    item = _fed_item().model_copy(update={"quality_label": "low"})
    assert compute_importance(item) == pytest.approx(80.0 * 0.6)


def test_rejected_item_has_zero_importance() -> None:
    item = _fed_item().model_copy(update={"quality_label": "rejected"})
    assert compute_importance(item) == 0.0


def test_importance_is_clamped_to_100() -> None:
    item = _fed_item().model_copy(update={"quality_label": "premium",
                                          "topics": ["central_bank", "inflation", "employment"]})
    # tier 30 + official 15 + top-two topics 45 + anchor 10 + corroboration 20 = 120 -> 100
    assert compute_importance(item, corroboration=50) == 100.0


def test_malformed_url_is_hard_rejected_without_exception() -> None:
    verdict = evaluate(make_item(url="https://[bad/x", id=stable_id("n", "bad")), make_publisher())
    assert verdict.hard_reject is True
    assert "missing_url" in verdict.reasons


def test_official_floor_applies_only_to_tier_1() -> None:
    tier2_official = make_publisher(id="agency", name="Agency", domain="agency.example", tier=2,
                                    kind="official")
    item = make_item(title="FEDERAL RESERVE ISSUES FOMC STATEMENT", summary="Statement.",
                     entities=[], topics=[], publisher_id="agency", publisher_name="Agency",
                     publisher_domain="agency.example", publisher_tier=2,
                     published_at=datetime(2026, 11, 8, 0, 0, tzinfo=UTC))
    verdict = evaluate(item, tier2_official, timestamp_source="feed", finance_relevance=0.2)
    assert verdict.label == "low"
    assert "official_floor" not in verdict.reasons


@pytest.mark.parametrize(
    "title",
    [
        "Tariff impact now showing up in CPI data",
        "Regulators weigh contact now-or-never deadline for merger",
    ],
)
def test_ordinary_now_headlines_are_not_clickbait(title: str) -> None:
    verdict = evaluate(make_item(title=title), make_publisher())
    assert not any(r.startswith("clickbait") for r in verdict.reasons)


def test_buy_now_pay_later_news_is_not_clickbait() -> None:
    verdict = evaluate(make_item(title="Buy now pay later lenders face new consumer rules"),
                       make_publisher())
    assert not any(r.startswith("clickbait") for r in verdict.reasons)


@pytest.mark.parametrize(
    "title",
    [
        "Buy now, pay later lenders face new consumer rules",
        "Buy now pay later lenders face new consumer rules",
    ],
)
def test_buy_now_pay_later_news_is_not_clickbait_with_comma(title: str) -> None:
    verdict = evaluate(make_item(title=title), make_publisher())
    assert not any(r.startswith("clickbait") for r in verdict.reasons)



# ── relevance term and label caps (final review wave B) ───────────────────

TRUMP = EntityRef(type="person", id="TRUMP", name="Trump", confidence=0.9, mentions=1, salience=1.0)
US_COUNTRY = EntityRef(type="country", id="US", name="US", confidence=0.85, mentions=1, salience=1.0)
META_BODY = EntityRef(type="equity", id="META", name="Meta", confidence=0.95, mentions=2, salience=0.45)
USD_BODY = EntityRef(type="fx", id="USD", name="USD", confidence=0.9, mentions=1, salience=0.45)


def test_relevance_term_adds_twenty_points_scaled() -> None:
    verdict = evaluate(make_item(), make_publisher(), finance_relevance=0.5)
    assert "relevance:+10" in verdict.reasons


def test_offtopic_trump_press_secretary_story_is_not_premium() -> None:
    item = make_item(
        url="https://example.com/trump-dana-white",
        title="Trump offers UFC chief Dana White press secretary job",
        summary="The president said he would like the mixed martial arts boss to brief reporters.",
        entities=[TRUMP, US_COUNTRY], topics=[],
    )
    verdict = evaluate(item, make_publisher(), finance_relevance=0.3)
    assert verdict.label == "low"
    assert "low_relevance_cap" in verdict.reasons


def test_offtopic_dumpster_diver_story_is_low_or_rejected() -> None:
    item = make_item(
        url="https://example.com/dumpster-diver",
        title="31-year-old dumpster diver brought in $140,000 from online sales",
        summary="A man who recovers discarded goods says his side business now earns well.",
        entities=[META_BODY, USD_BODY], topics=[],
    )
    # relevance 0.1 (body-only META/USD do not count): non_finance hard reject for general press
    verdict = evaluate(item, make_publisher(), finance_relevance=0.1)
    assert verdict.label in {"low", "rejected"}
    assert verdict.label != "premium"


def test_body_only_entities_do_not_satisfy_premium_without_topic() -> None:
    item = make_item(entities=[META_BODY], topics=[])
    verdict = evaluate(item, make_publisher(), finance_relevance=0.9)
    assert "entity_high_conf:+7" not in verdict.reasons
    assert verdict.label == "standard"
    assert "premium_cap" in verdict.reasons


def test_premium_requires_relevance_of_point_six() -> None:
    verdict = evaluate(make_item(), make_publisher(), finance_relevance=0.5)
    assert verdict.label == "standard"
    assert "premium_cap" in verdict.reasons


def test_real_earnings_story_stays_premium() -> None:
    verdict = evaluate(make_item(), make_publisher(), finance_relevance=0.85)
    assert verdict.label == "premium"
    assert "premium_cap" not in verdict.reasons
    assert "low_relevance_cap" not in verdict.reasons


def test_low_relevance_caps_label_even_for_high_tier() -> None:
    publisher = make_publisher(id="reuters", name="Reuters", domain="reuters.com", tier=1,
                               kind="wire")
    verdict = evaluate(make_item(), publisher, finance_relevance=0.4)
    assert verdict.label == "low"
    assert "low_relevance_cap" in verdict.reasons


def test_official_floor_needs_relevance_of_point_three() -> None:
    fed = make_publisher(id="fed", name="Federal Reserve", domain="federalreserve.gov", tier=1,
                         kind="official")
    item = make_item(title="FEDERAL RESERVE ISSUES FOMC STATEMENT", summary="Statement.",
                     entities=[], topics=[], content_type="official",
                     publisher_id="fed", publisher_name="Federal Reserve",
                     publisher_domain="federalreserve.gov", publisher_tier=1,
                     published_at=datetime(2026, 11, 8, 0, 0, tzinfo=UTC))
    floored = evaluate(item, fed, timestamp_source="feed", finance_relevance=0.3)
    assert floored.label == "standard"
    assert "official_floor" in floored.reasons
    unfloored = evaluate(item, fed, timestamp_source="feed", finance_relevance=0.2)
    assert unfloored.label == "low"
    assert "official_floor" not in unfloored.reasons


def test_shipped_clickbait_rules_contain_no_backspace_characters() -> None:
    """Double-quoted YAML turns "\b" into a backspace; every rule must reach the regex engine intact."""
    import yaml

    from awareness.markets.config import PACKAGE_DATA_DIR

    rules = yaml.safe_load((PACKAGE_DATA_DIR / "quality_rules.yaml").read_text(encoding="utf-8"))
    flat: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, str):
            flat.append(node)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(rules)
    assert not [r for r in flat if "\x08" in r]
