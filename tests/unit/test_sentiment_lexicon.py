"""Lexicon sanity checks for the financial-news sentiment lexicon.

Guards the two properties the engine depends on: both polarity sets are
large enough to be useful, and no token can ever be scored in two
directions (POSITIVE and NEGATIVE are disjoint, and the modifier sets do
not collide with polarity words).
"""

from __future__ import annotations

from awareness.sentiment.lexicon import (
    INTENSIFIERS,
    NEGATIONS,
    NEGATIVE,
    POSITIVE,
)


def test_polarity_sets_are_large_enough() -> None:
    assert len(POSITIVE) >= 120
    assert len(NEGATIVE) >= 120


def test_no_overlap_between_positive_and_negative() -> None:
    assert POSITIVE & NEGATIVE == frozenset()


def test_modifier_sets_are_disjoint_from_polarity_words() -> None:
    assert NEGATIONS & POSITIVE == frozenset()
    assert NEGATIONS & NEGATIVE == frozenset()
    assert INTENSIFIERS & POSITIVE == frozenset()
    assert INTENSIFIERS & NEGATIVE == frozenset()
    assert INTENSIFIERS & NEGATIONS == frozenset()


def test_words_are_lowercase_single_tokens() -> None:
    for word in POSITIVE | NEGATIVE | NEGATIONS | INTENSIFIERS:
        assert word == word.lower()
        assert " " not in word


def test_finance_flavored_anchors_present() -> None:
    for word in ("rally", "surge", "beat", "record", "bullish", "gains", "boost"):
        assert word in POSITIVE
    for word in ("slump", "crash", "plunge", "miss", "bearish", "losses", "layoff"):
        assert word in NEGATIVE
    assert "not" in NEGATIONS
    assert "very" in INTENSIFIERS
