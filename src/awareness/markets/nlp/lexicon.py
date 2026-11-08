"""Deterministic finance lexicon scorer (``lexicon:aw-fin-1``).

The word lists live in ``nlp/finance_lexicon.yaml`` and were written for this project
(English and Turkish). Scoring rules:

* Text is lower-cased and tokenised on word characters, keeping internal hyphens and
  apostrophes (``sell-off``, ``didn't``). Punctuation (``, ; : . ! ?``) and the conjunctions
  ``but``/``ama``/``fakat`` become clause boundaries that no phrase spans.
* Phrases are matched greedily, longest first, so a phrase entry overrides its words.
  Weight-0 phrases (``by default``) are blockers: they consume tokens without a hit.
* A hit is negated when a negator occurs within the 3 tokens before it, without crossing a
  clause boundary (``fails to`` counts too). Negation multiplies the weight by -0.6. An
  intensifier within the 2 tokens before it, same clause only, multiplies the weight by 1.4.
* Known limitations: Turkish suffixal negation (``yükselmedi``, ``düşmedi``) is not
  recognised; ASCII uppercase ``I`` is not folded to Turkish dotless i, so ALL-CAPS Turkish
  headlines containing ``I`` may miss terms.
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
LEXICON_PATH = Path(__file__).resolve().parent / "finance_lexicon.yaml"

POSITIVE_THRESHOLD = 0.15
NEGATIVE_THRESHOLD = -0.15
NEGATION_WINDOW = 3
GAP_MAX = 2  # tokens allowed between the two words of a gapped rule
INTENSIFIER_WINDOW = 2
NEGATION_FACTOR = -0.6
INTENSIFIER_FACTOR = 1.4
NO_HIT_CONFIDENCE = 0.3

_TOKEN_RE = re.compile(r"[^\W_]+(?:['\-][^\W_]+)*|[,;:.!?]")
_PUNCTUATION = frozenset({",", ";", ":", ".", "!", "?"})
_CLAUSE_WORDS = frozenset({"but", "ama", "fakat"})
CLAUSE_BOUNDARY = "|"  # sentinel token; never a lexicon term
Term = tuple[str, ...]


def tokenize(text: str) -> list[str]:
    """Lower-case word tokens. Clause punctuation and clause conjunctions become CLAUSE_BOUNDARY."""
    normalized = text.replace("İ", "i").replace("’", "'").lower()
    out: list[str] = []
    for token in _TOKEN_RE.findall(normalized):
        if token in _PUNCTUATION or token in _CLAUSE_WORDS:
            out.append(CLAUSE_BOUNDARY)
        else:
            out.append(token)
    return out


def label_for_score(score: float) -> SentimentLabel:
    """Map a continuous score onto the shared label thresholds."""
    if score >= POSITIVE_THRESHOLD:
        return "positive"
    if score <= NEGATIVE_THRESHOLD:
        return "negative"
    return "neutral"


@dataclass(frozen=True)
class GappedRule:
    """A ``first`` word and a ``second`` phrase separated by at most GAP_MAX tokens."""

    first: frozenset[str]
    second: frozenset[Term]
    weight: float


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
    gapped: tuple[GappedRule, ...] = ()
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
        gapped=tuple(_parse_gapped(raw.get("gapped") or [])),
    )


def _parse_gapped(entries: list[dict[str, Any]]) -> list[GappedRule]:
    rules: list[GappedRule] = []
    for entry in entries:
        first_terms = [tuple(t) for t in (tokenize(p) for p in str(entry["first"]).split("|")) if t]
        first = frozenset(term[0] for term in first_terms if len(term) == 1)
        second = frozenset(
            tuple(t) for t in (tokenize(p) for p in str(entry["second"]).split("|")) if t
        )
        if not first or not second:
            raise ValueError(f"gapped rule needs non-empty first and second terms: {entry!r}")
        rules.append(GappedRule(first=first, second=second, weight=float(entry["weight"])))
    return rules


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
            # Priority: multi-word phrase, then gapped rule, then single word.
            matched_len, weight = self._match_at(tokens, i, multi_only=True)
            if matched_len == 0:
                matched_len, weight = self._match_gapped(tokens, i)
            if matched_len == 0:
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

    def _match_at(self, tokens: list[str], start: int, *, multi_only: bool = False) -> tuple[int, float]:
        """Longest lexicon term starting at ``start``. Returns (length, weight) or (0, 0.0).

        With ``multi_only`` only terms of two or more tokens are tried.
        """
        table = self._lex.table
        lowest = 2 if multi_only else 1
        for length in range(min(self._lex.max_len, len(tokens) - start), lowest - 1, -1):
            term = tuple(tokens[start : start + length])
            if term in table:
                return length, table[term]
        return 0, 0.0

    def _match_gapped(self, tokens: list[str], start: int) -> tuple[int, float]:
        """A gapped rule whose first word is at ``start``. Returns (span length, weight) or (0, 0.0)."""
        for rule in self._lex.gapped:
            if tokens[start] not in rule.first:
                continue
            for j in range(start + 1, min(len(tokens), start + GAP_MAX + 2)):
                if tokens[j] == CLAUSE_BOUNDARY:
                    break
                for term in rule.second:
                    if tuple(tokens[j : j + len(term)]) == term:
                        return j + len(term) - start, rule.weight
        return 0, 0.0

    def _modifier(self, tokens: list[str], index: int) -> float:
        """Combined negation and intensifier factor for the hit starting at ``index``."""
        factor = 1.0
        if self._negated(tokens, index):
            factor *= NEGATION_FACTOR
        if any(t in self._lex.intensifiers for t in _same_clause_before(tokens, index, INTENSIFIER_WINDOW)):
            factor *= INTENSIFIER_FACTOR
        return factor

    def _negated(self, tokens: list[str], index: int) -> bool:
        """True when a negator sits in the same clause, within NEGATION_WINDOW tokens before ``index``."""
        negators = self._lex.negators
        failures = self._lex.failure_words
        for k in range(index - 1, max(-1, index - NEGATION_WINDOW - 1), -1):
            word = tokens[k]
            if word == CLAUSE_BOUNDARY:
                break
            if word in negators:
                return True
            if word == "to" and k > 0 and tokens[k - 1] in failures:
                return True
        return False


def _same_clause_before(tokens: list[str], index: int, size: int) -> list[str]:
    """Up to ``size`` tokens before ``index`` that stay inside the same clause."""
    out: list[str] = []
    for k in range(index - 1, max(-1, index - size - 1), -1):
        if tokens[k] == CLAUSE_BOUNDARY:
            break
        out.append(tokens[k])
    return out
