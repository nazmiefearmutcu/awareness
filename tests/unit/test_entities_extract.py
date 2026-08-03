"""Unit tests for the heuristic entity extractor."""

from __future__ import annotations

from awareness.entities.extract import extract_entities, normalize_entity


def test_org_with_inc() -> None:
    out = extract_entities("Apple Inc announced a new ETF backed by BlackRock in New York")
    kinds = dict(out)
    assert kinds.get("Apple Inc") == "ORG"
    assert kinds.get("New York") == "PLACE"


def test_place_city() -> None:
    out = extract_entities("The summit was held in New York last week")
    assert any(e == "New York" and k == "PLACE" for e, k in out)


def test_ticker_dollar_and_bare() -> None:
    out = extract_entities("$BTC rose 5% while ETH dipped")
    assert ("BTC", "TICKER") in out
    assert ("ETH", "TICKER") in out


def test_ticker_not_in_word() -> None:
    out = extract_entities("the btcx token and applebee restaurants")
    assert not any(k == "TICKER" for _, k in out)


def test_noise_sentence_yields_nothing_solid() -> None:
    out = extract_entities("The quick brown fox jumps over the lazy dog")
    # "The quick" is PERSON_STOP-guarded; nothing org/place/ticker should appear.
    assert not any(k in ("ORG", "PLACE", "TICKER") for _, k in out)


def test_person_conservative() -> None:
    out = extract_entities("Warren Buffett bought more Apple shares")
    assert any(e == "Warren Buffett" and k == "PERSON" for e, k in out)


def test_deduplication_within_doc() -> None:
    out = extract_entities("Apple Inc is great. Apple Inc again.")
    assert out.count(("Apple Inc", "ORG")) == 1


def test_normalize_plural_and_case() -> None:
    assert normalize_entity("Bitcoin") == "Bitcoin"
    assert normalize_entity("bitcoin") == "Bitcoin"
    # Plural forms are preserved: stripping corrupts known entities
    # ("United States" -> "United State") and breaks query round-trips.
    assert normalize_entity("Banks") == "Banks"
    assert normalize_entity("United States") == "United States"
    assert normalize_entity("Los Angeles") == "Los Angeles"


def test_empty_and_short() -> None:
    assert extract_entities("") == []
    assert extract_entities("   ") == []
    assert extract_entities("a") == []


def test_org_suffix_group() -> None:
    out = extract_entities("The Federal Reserve kept rates unchanged, per the European Central Bank")
    kinds = dict(out)
    assert kinds.get("Federal Reserve") == "ORG"
    assert kinds.get("European Central Bank") == "ORG"
