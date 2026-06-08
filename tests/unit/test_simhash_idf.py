from __future__ import annotations

from collections.abc import Callable

from awareness.util.hashing import simhash128


def test_idf_none_is_backward_compatible() -> None:
    text = "the quick brown fox jumps over the lazy dog the the the"
    assert simhash128(text) == simhash128(text, idf=None)


def test_idf_changes_the_signature() -> None:
    text = "the quick brown fox jumps over the lazy dog the the the"

    def downweight_the(shingle: str) -> float:
        return 0.01 if "the" in shingle.split() else 1.0

    assert simhash128(text, idf=downweight_the) != simhash128(text)


def test_idf_accepts_callable_type() -> None:
    idf: Callable[[str], float] = lambda s: 1.0  # noqa: E731
    text = "alpha beta gamma delta epsilon"
    assert simhash128(text, idf=idf) == simhash128(text)
