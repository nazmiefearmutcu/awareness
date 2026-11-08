"""Fix round 3 regression tests: person-cap scope, bare Berkshire, Turkish petrol, false phrases."""

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


# (1) the person-implied cap applies only to entities created through a person link

@pytest.mark.parametrize(
    ("title", "eid"),
    [
        ("Tesla shares jump as Elon Musk says robotaxi launches soon", "TSLA"),
        ("Musk says Tesla will end Cybertruck production in 2027", "TSLA"),
        ("Apple shares rise as Tim Cook unveils new product", "AAPL"),
        ("Nvidia shares rise as Jensen Huang speaks", "NVDA"),
        ("Warren Buffett isn't running Berkshire anymore", "BRK-B"),
    ],
)
def test_explicit_company_mention_keeps_title_salience(linker: EntityLinker, title: str, eid: str) -> None:
    ref = _get(linker.link(title), "equity", eid)
    assert ref is not None, title
    assert ref.salience == pytest.approx(1.0), title


def test_person_only_link_keeps_the_cap(linker: EntityLinker) -> None:
    ref = _get(linker.link("Elon Musk appears to signal the end of the road for Cybertruck"), "equity", "TSLA")
    assert ref is not None
    assert ref.salience <= 0.5
    assert ref.confidence == pytest.approx(0.65)


# (2) bare "Berkshire" only when not followed by a capitalised non-Hathaway word

@pytest.mark.parametrize(
    "title",
    [
        "Berkshire Hills Bancorp reports quarterly profit",
        "Berkshire Grey robotics unit wins contract",
        "Berkshire Partners buys stake in retailer",
    ],
)
def test_berkshire_companies_are_not_brk_b(linker: EntityLinker, title: str) -> None:
    assert _get(linker.link(title), "equity", "BRK-B") is None, title


@pytest.mark.parametrize(
    "title",
    [
        "Berkshire Hathaway buys back $9.6 billion of its own stock",
        "Berkshire shares rise after the annual meeting",
        "Berkshire, the conglomerate, adds to its stake",
    ],
)
def test_berkshire_stock_mentions_still_link(linker: EntityLinker, title: str) -> None:
    ref = _get(linker.link(title), "equity", "BRK-B")
    assert ref is not None, title
    assert ref.salience == pytest.approx(1.0)


# (3) Turkish "petrol": generic oil, Brent only with price context, never company names

def test_tpao_petrol_arama_has_no_wti_or_brent(linker: EntityLinker, clf: TaxonomyClassifier) -> None:
    title = "TPAO'nun petrol arama ruhsat\u0131 2028'e uzat\u0131ld\u0131"
    refs = linker.link(title)
    assert _get(refs, "commodity", "WTI") is None
    assert _get(refs, "commodity", "BRENT") is None
    assert "energy" not in clf.classify(title, None, refs).topics


def test_petrol_with_price_context_is_brent(linker: EntityLinker) -> None:
    ref = _get(linker.link("Petrol fiyatlar\u0131 varil baş\u0131na yükseldi"), "commodity", "BRENT")
    assert ref is not None


@pytest.mark.parametrize(
    "title",
    [
        "Petrol Ofisi raises fuel margins",
        "Türkiye Petrolleri reports quarterly profit",
    ],
)
def test_petrol_company_names_are_not_commodities(linker: EntityLinker, title: str) -> None:
    refs = linker.link(title)
    assert _get(refs, "commodity", "BRENT") is None
    assert _get(refs, "commodity", "WTI") is None


def test_ham_petrol_is_not_wti(linker: EntityLinker) -> None:
    assert _get(linker.link("Ham petrol stoklar\u0131 artt\u0131"), "commodity", "WTI") is None


# (4) "Corporate America" is not the US; repo is not a buyback

def test_corporate_america_is_not_the_us(linker: EntityLinker) -> None:
    refs = linker.link("Inflation Fears Return as Corporate America Counts the Cost")
    assert _get(refs, "country", "US") is None


def test_real_america_still_maps_to_us(linker: EntityLinker) -> None:
    assert _get(linker.link("America adds jobs as factories hire"), "country", "US") is not None


@pytest.mark.parametrize(
    "title",
    [
        "Company repurchase agreements rise at Fed",
        "Repo market strains ease after Fed injection",
        "Repurchase agreements jump as banks borrow",
    ],
)
def test_repo_is_not_dividends_buybacks(clf: TaxonomyClassifier, title: str) -> None:
    assert "dividends_buybacks" not in clf.classify(title, None, []).topics, title


def test_genuine_buyback_still_fires_next_to_repo(clf: TaxonomyClassifier) -> None:
    result = clf.classify("Repo rates ease as Berkshire repurchase of shares rises", None, [])
    assert "dividends_buybacks" in result.topics
