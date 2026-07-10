"""Gopher/C4 content-quality heuristics (pure, language-agnostic)."""

from __future__ import annotations

from awareness.normalize.quality import gopher_quality


def _clean_paragraph() -> str:
    # ~64 words, stopword-rich, normal word lengths, no symbols/bullets.
    return (
        "The committee reviewed the annual report and approved the budget "
        "with broad support from the members that attended the meeting. "
    ) * 4


def test_accepts_clean_prose() -> None:
    v = gopher_quality(_clean_paragraph())
    assert v.ok is True
    assert v.reason is None


def test_rejects_too_few_words() -> None:
    v = gopher_quality("hello world")
    assert v.ok is False
    assert v.reason == "too_few_words"


def test_rejects_no_stopwords() -> None:
    # 64 content words, none in the stop set, normal lengths, all alpha.
    text = (
        "elephant giraffe mountain river forest planet harbor lantern "
        "compass blanket biscuit garden window meadow anchor saddle "
    ) * 4
    v = gopher_quality(text)
    assert v.ok is False
    assert v.reason == "no_stopwords"


def test_rejects_symbol_spam() -> None:
    # Clean prose + many '#'-bearing tokens: exceeds the symbol/word ratio
    # without tripping the word-count or mean-length gates.
    text = _clean_paragraph() + " " + " ".join(f"item#{i}" for i in range(40))
    v = gopher_quality(text)
    assert v.ok is False
    assert v.reason == "symbol_to_word_ratio"


def test_rejects_bullet_list() -> None:
    text = "\n".join(f"• item number {i} with the value and that note" for i in range(60))
    v = gopher_quality(text)
    assert v.ok is False
    assert v.reason == "bullet_lines"
