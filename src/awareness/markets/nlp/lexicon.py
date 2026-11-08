"""Deterministic finance lexicon scorer (``lexicon:aw-fin-1``).

The word lists live in ``awareness/markets/data/finance_lexicon.yaml`` and were written
for this project (English and Turkish). Scoring rules:

* Text is lower-cased (Turkish dotted capital I handled) and tokenised on word characters,
  keeping internal hyphens and apostrophes (``sell-off``, ``didn't``).
* Phrases are matched greedily, longest first, so a phrase entry overrides its words.
  Weight-0 phrases (``by default``) are blockers: they consume tokens without a hit.
* A hit is negated when a negator occurs within the 3 tokens before it (``fails to``
  counts too). Negation multiplies the weight by -0.6. An intensifier within the 2 tokens
  before it multiplies the weight by 1.4.
* Document score = ``tanh(sum(weights) / sqrt(hits + 1))`` clamped to [-1, 1].
  Label: positive if >= 0.15, negative if <= -0.15, otherwise neutral.
  Confidence = ``min(0.9, 0.35 + 0.1 * hits)``; with no hits it is 0.3 and the label neutral.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from awareness.markets.models import SentimentLabel, SentimentScore

LEXICON_MODEL = "lexicon:aw-fin-1"
LEXICON_PATH = Path(__file__).resolve().parents[1] / "data" / "finance_lexicon.yaml"

POSITIVE_THRESHOLD = 0.15
NEGATIVE_THRESHOLD = -0.15
NEGATION_WINDOW = 3
INTENSIFIER_WINDOW = 2
NEGATION_FACTOR = -0.6
INTENSIFIER_FACTOR = 1.4
NO_HIT_CONFIDENCE = 0.3

_TOKEN_RE = re.compile(r"[^\W_]+(?:['\-][^\W_]+)*")
Term = tuple[str, ...]


def tokenize(text: str) -> list[str]:
    """Lower-case word tokens. Handles the Turkish dotted capital I and curly apostrophes."""
    normalized = text.replace("İ", "i").replace("’", "'").lower()  # noqa: RUF001
    return _TOKEN_RE.findall(normalized)


def label_for_score(score: float) -> SentimentLabel:
    """Map a continuous score onto the shared label thresholds."""
    if score >= POSITIVE_THRESHOLD:
        return "positive"
    if score <= NEGATIVE_THRESHOLD:
        return "negative"
    return "neutral"


@dataclass(frozen=True)
class Lexicon:
    """Compiled lexicon. Keys are token tuples; values are weights."""

    version: str
    positive: dict[Term, float]
    negative: dict[Term, float]
    blockers: frozenset[Term]
    negators: frozenset[str]
    failure_words: frozenset[str]
    intensifiers: frozenset[str]
    table: dict[Term, float] = field(init=False, repr=False)
    max_len: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        table: dict[Term, float] = {**self.positive, **self.negative}
        table.update({term: 0.0 for term in self.blockers})
        object.__setattr__(self, "table", table)
        object.__setattr__(self, "max_len", max((len(t) for t in table), default=1))


def _expand_section(
    section: dict[str, Any], seen: set[Term], *, sign_check: str | None = None
) -> dict[Term, float]:
    out: dict[Term, float] = {}
    for key, raw_weight in section.items():
        weight = float(raw_weight)
        if sign_check == "positive" and weight <= 0:
            raise ValueError(f"positive lexicon entry {key!r} has non-positive weight {weight}")
        if sign_check == "negative" and weight >= 0:
            raise ValueError(f"negative lexicon entry {key!r} has non-negative weight {weight}")
        for alternative in str(key).split("|"):
            term = tuple(tokenize(alternative))
            if not term:
                raise ValueError(f"empty lexicon term in key {key!r}")
            if term in seen:
                raise ValueError(f"duplicate lexicon term {' '.join(term)!r}")
            seen.add(term)
            out[term] = weight
    return out


@lru_cache(maxsize=4)
def load_lexicon(path: Path = LEXICON_PATH) -> Lexicon:
    """Parse and validate the YAML lexicon once per path."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"lexicon file {path} must contain a mapping")
    seen: set[Term] = set()
    positive = _expand_section(raw.get("positive") or {}, seen, sign_check="positive")
    negative = _expand_section(raw.get("negative") or {}, seen, sign_check="negative")
    blockers = frozenset(_expand_section(raw.get("neutral_phrases") or {}, seen))
    return Lexicon(
        version=str(raw.get("version", "aw-fin-1")),
        positive=positive,
        negative=negative,
        blockers=blockers,
        negators=frozenset(str(w).lower() for w in raw.get("negators") or []),
        failure_words=frozenset(str(w).lower() for w in raw.get("failure_words") or []),
        intensifiers=frozenset(str(w).lower() for w in raw.get("intensifiers") or []),
    )


class LexiconScorer:
    """Deterministic, dependency-free financial sentiment scorer."""

    name = LEXICON_MODEL

    def __init__(self, lexicon: Lexicon | None = None) -> None:
        self._lex = lexicon if lexicon is not None else load_lexicon()

    def score(self, text: str) -> SentimentScore:
        """Score one text. Empty or whitespace-only text is neutral with zero confidence."""
        if not text or not text.strip():
            return SentimentScore(label="neutral", score=0.0, confidence=0.0, model=self.name)
        tokens = tokenize(text)
        total = 0.0
        hits = 0
        i = 0
        while i < len(tokens):
            matched_len, weight = self._match_at(tokens, i)
            if matched_len == 0:
                i += 1
                continue
            if weight != 0.0:
                total += weight * self._modifier(tokens, i)
                hits += 1
            i += matched_len
        if hits == 0:
            return SentimentScore(label="neutral", score=0.0, confidence=NO_HIT_CONFIDENCE, model=self.name)
        score = max(-1.0, min(1.0, math.tanh(total / math.sqrt(hits + 1))))
        return SentimentScore(
            label=label_for_score(score),
            score=score,
            confidence=min(0.9, 0.35 + 0.1 * hits),
            model=self.name,
        )

    def _match_at(self, tokens: list[str], start: int) -> tuple[int, float]:
        """Longest lexicon term starting at ``start``. Returns (length, weight) or (0, 0.0)."""
        table = self._lex.table
        for length in range(min(self._lex.max_len, len(tokens) - start), 0, -1):
            term = tuple(tokens[start : start + length])
            if term in table:
                return length, table[term]
        return 0, 0.0

    def _modifier(self, tokens: list[str], index: int) -> float:
        """Combined negation and intensifier factor for the hit starting at ``index``."""
        factor = 1.0
        if self._negated(tokens, max(0, index - NEGATION_WINDOW), index):
            factor *= NEGATION_FACTOR
        if any(t in self._lex.intensifiers for t in tokens[max(0, index - INTENSIFIER_WINDOW) : index]):
            factor *= INTENSIFIER_FACTOR
        return factor

    def _negated(self, tokens: list[str], start: int, end: int) -> bool:
        negators = self._lex.negators
        failures = self._lex.failure_words
        for k in range(start, end):
            if tokens[k] in negators:
                return True
            if tokens[k] == "to" and k > 0 and tokens[k - 1] in failures:
                return True
        return False
