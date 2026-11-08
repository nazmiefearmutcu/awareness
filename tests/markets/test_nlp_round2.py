"""Fix round 2 regression tests: topic phrase families and person-implied company links."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from awareness.markets.models import EntityRef
from awareness.markets.nlp.entities import EntityLinker
from awareness.markets.nlp.taxonomy import TaxonomyClassifier
from awareness.markets.ref.sec_tickers import parse_company_tickers

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def linker() -> EntityLinker:
    payload = json.loads((FIXTURES / "sec_company_tickers.json").read_text(encoding="utf-8"))
    return EntityLinker(parse_company_tickers(payload))


@pytest.fixture(scope="module")
def clf() -> TaxonomyClassifier:
    return TaxonomyClassifier()


def _get(refs: list[EntityRef], etype: str, eid: str) -> EntityRef | None:
    return next((r for r in refs if r.type == etype and r.id == eid), None)


# ── F-1: buyback and profit phrasing ─────────────────────────────────────

LIVE_BERKSHIRE_BUYBACK = (
    "Berkshire Hathaway buys back $9.6 billion of its own stock but its profits declined"
)


def test_live_berkshire_buyback_and_profit_decline_both_fire(clf: TaxonomyClassifier) -> None:
    result = clf.classify(LIVE_BERKSHIRE_BUYBACK, None, [])
    assert "dividends_buybacks" in result.topics
    assert "earnings" in result.topics


@pytest.mark.parametrize(
    "title",
    [
        "Berkshire Hathaway buys back $9.6 billion of its own stock",
        "Berkshire Hathaway buy back program expands",
        "Berkshire is buying back shares at a record pace",
        "Berkshire bought back $5 billion of stock in the quarter",
        "Berkshire Hathaway repurchase of shares rises",
        "Berkshire repurchased $4 billion of its shares",
        "Berkshire Doubles Stock Buyback",
        "Berkshire Hathaway speeds up stock buybacks, though profit falls",
        "Berkshire Hathaway launches a share buyback",
    ],
)
def test_buyback_family_fires_dividends_buybacks(clf: TaxonomyClassifier, title: str) -> None:
    assert "dividends_buybacks" in clf.classify(title, None, []).topics, title


@pytest.mark.parametrize(
    "title",
    [
        "Net profit falls 20% at Berkshire Hathaway",
        "Profits decline at Berkshire",
        "Berkshire profit declined in the quarter",
        "Quarterly profits fall at Acme",
        "Acme profits drop as costs rise",
        "Acme profit slips on weaker demand",
        "Acme profits rise to a record",
        "Acme profit jumps 30%",
        "Acme profits surge on cloud sales",
        "Acme earnings fell sharply",
        "Acme operating earnings decline",
        "Acme kâr düştü",
        "Acme kâr artt\u0131",
    ],
)
def test_profit_family_fires_earnings(clf: TaxonomyClassifier, title: str) -> None:
    assert "earnings" in clf.classify(title, None, []).topics, title


def test_bare_profit_does_not_fire_earnings(clf: TaxonomyClassifier) -> None:
    assert "earnings" not in clf.classify("Profit is the goal of every startup", None, []).topics


# ── F-2: person-implied company links ───────────────────────────────────


def test_musk_spacex_headline_has_no_tesla(linker: EntityLinker) -> None:
    refs = linker.link(
        'Elon Musk Says SpaceX Sales Are "Exploding" and Forecasts Revenue Twice Wall Street\'s Estimate'
    )
    assert _get(refs, "equity", "TSLA") is None
    assert _get(refs, "person", "MUSK") is not None


def test_musk_cos_group_reference_has_no_tesla(linker: EntityLinker) -> None:
    refs = linker.link("Democrats prepare corporate probe wave targeting Apple, Meta, Musk cos.: report")
    assert _get(refs, "equity", "TSLA") is None


def test_person_implied_link_is_capped_and_confident(linker: EntityLinker) -> None:
    refs = linker.link("Elon Musk appears to signal the end of the road for Cybertruck")
    tsla = _get(refs, "equity", "TSLA")
    assert tsla is not None
    assert tsla.confidence == pytest.approx(0.65)
    assert tsla.salience <= 0.5


@pytest.mark.parametrize(
    ("title", "eid", "conf"),
    [
        ("Jensen Huang unveils a new chip roadmap", "NVDA", 0.65),
        ("Tim Cook hints at new products this fall", "AAPL", 0.65),
        ("Warren Buffett says patience still pays in stock picking", "BRK-B", 0.65),
    ],
)
def test_other_person_links_use_cap(linker: EntityLinker, title: str, eid: str, conf: float) -> None:
    ref = _get(linker.link(title), "equity", eid)
    assert ref is not None, title
    assert ref.confidence == pytest.approx(conf)
    assert ref.salience <= 0.5


def test_person_link_dropped_when_item_names_other_company(linker: EntityLinker) -> None:
    refs = linker.link("Jensen Huang says Intel chips lag as Intel shares fall")
    assert _get(refs, "equity", "NVDA") is None
    assert _get(refs, "equity", "INTC") is not None


# ── M-3: bare Berkshire and Meta in company lists ──────────────────────


def test_bare_berkshire_links_brk_b(linker: EntityLinker) -> None:
    ref = _get(linker.link("Berkshire doubles its stock buyback"), "equity", "BRK-B")
    assert ref is not None
    assert ref.confidence == pytest.approx(0.85)


def test_meta_in_company_list_links_meta(linker: EntityLinker) -> None:
    refs = linker.link("Lawmakers target Apple, Meta and Google over data rules")
    assert _get(refs, "equity", "META") is not None


def test_meta_alone_is_not_linked(linker: EntityLinker) -> None:
    assert _get(linker.link("Meta is a word used in the research paper"), "equity", "META") is None
