"""Financial sentiment scorer: FinBERT (``ProsusAI/finbert``) with a deterministic lexicon fallback.

``SentimentScorer(backend)`` picks its backend at construction:

* ``"lexicon"`` always uses :class:`~awareness.markets.nlp.lexicon.LexiconScorer`.
* ``"finbert"`` loads the transformer or raises :class:`RuntimeError`. Inference errors raise too.
* ``"auto"`` tries FinBERT at load time and falls back to the lexicon, logging one warning.
  An inference error falls back to the lexicon for that call. After
  ``MAX_INFERENCE_FAILURES`` consecutive inference failures the scorer switches to the lexicon
  permanently.

The ``model`` property names the backend that is active now, for example
``finbert:ProsusAI/finbert`` or ``lexicon:aw-fin-1``. Every returned score carries the model
name that actually produced it.

``transformers`` and ``torch`` are imported lazily, so importing this module stays cheap.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any, Literal

from awareness.markets.models import EntityRef, SentimentLabel, SentimentScore
from awareness.markets.nlp.lexicon import LEXICON_MODEL, LexiconScorer, label_for_score

log = logging.getLogger(__name__)

DEFAULT_FINBERT_MODEL = "ProsusAI/finbert"
MIN_BODY_SENTENCE_WORDS = 4
MAX_INFERENCE_FAILURES = 3

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+|\n+")
# Connectors that split the text between two entity mentions (see _regions).
_CONNECTOR_RE = re.compile(
    r"[,;:]|\b(?:as|while|after|but|and|whereas|although|though|however|meanwhile|because|since"
    r"|amid|yet|ama|iken|ve|fakat)\b",
    re.IGNORECASE,
)

Backend = Literal["auto", "finbert", "lexicon"]

# Strong negatives that FinBERT tends to misread as positive ("by default" is neutral).
_STRONG_NEGATIVE_RE = re.compile(
    r"(?<!by )\b(?:defaults?|defaulted|defaulting|bankruptcy|bankrupt|fraud|delisting|delisted"
    r"|insolvency|iflas|temerr\u00fct|temerrut)\b",
    re.IGNORECASE,
)
_CLAUSE_SPLIT_RE = re.compile(r"[,;:.!?]|\bbut\b|\bama\b|\bfakat\b", re.IGNORECASE)
ENGLISH_CODES = frozenset({"en", ""})
# Negators the veto checks itself: the lexicon has no bare "no" entry, so "no fraud" needs this.
VETO_NEGATORS = frozenset({"no", "not", "without", "never", "nor", "zero", "free"})
_WORD_RE = re.compile(r"[\w']+")

_FINBERT_CACHE: dict[str, tuple[Any, Any]] = {}
_FINBERT_CACHE_LOCK = threading.Lock()


def _load_finbert_model(model_name: str, *, local_only: bool = False) -> tuple[Any, Any]:
    """Load ``(tokenizer, model)``. Tries the local Hugging Face cache first.

    Raises ``ImportError`` when transformers/torch are missing and ``OSError`` when the weights
    are not cached and ``local_only`` is set (or the download also fails).
    """
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    try:
        tokenizer = AutoTokenizer.from_pretrained(model_name, local_files_only=True)
        model = AutoModelForSequenceClassification.from_pretrained(model_name, local_files_only=True)
    except OSError:
        if local_only:
            raise
        tokenizer = AutoTokenizer.from_pretrained(model_name)
        model = AutoModelForSequenceClassification.from_pretrained(model_name)
    model.eval()
    torch.set_num_threads(min(4, os.cpu_count() or 1))
    return tokenizer, model


def _get_finbert_model(model_name: str) -> tuple[Any, Any]:
    """Process-wide cache so repeated scorers do not reload the weights."""
    with _FINBERT_CACHE_LOCK:
        cached = _FINBERT_CACHE.get(model_name)
        if cached is None:
            cached = _load_finbert_model(model_name, local_only=False)
            _FINBERT_CACHE[model_name] = cached
        return cached


def clear_finbert_cache() -> None:
    """Drop cached FinBERT weights (used by tests)."""
    with _FINBERT_CACHE_LOCK:
        _FINBERT_CACHE.clear()


def _clamp(value: float) -> float:
    return max(-1.0, min(1.0, value))


def is_english(language: str | None) -> bool:
    """True for English or an unknown language (None): only English uses FinBERT."""
    if language is None:
        return True
    return language.strip().lower().replace("_", "-").split("-")[0] in ENGLISH_CODES


def _as_label(name: str) -> SentimentLabel:
    if name == "positive":
        return "positive"
    if name == "negative":
        return "negative"
    return "neutral"


def _split_sentences(title: str | None, text: str | None) -> list[str]:
    """Title first (always kept), then body sentences with at least four words."""
    sentences: list[str] = []
    if title and title.strip():
        sentences.append(title.strip())
    for part in _SENTENCE_SPLIT_RE.split(text or ""):
        cleaned = part.strip()
        if len(cleaned.split()) >= MIN_BODY_SENTENCE_WORDS:
            sentences.append(cleaned)
    return sentences


def _surface_pattern(entity: EntityRef) -> re.Pattern[str] | None:
    """Mention pattern for an entity.

    Names match case-insensitively on word boundaries. Ids (tickers, codes such as ``ON``,
    ``IT``, ``US``) match case-sensitively, so ordinary words like "on" or "it" never count as
    mentions. An optional ``$`` prefix is accepted.
    """
    name = entity.name.strip() if entity.name else ""
    ticker = entity.id.strip() if entity.id else ""
    parts: list[str] = []
    if len(name) >= 2:
        parts.append("(?i:" + re.escape(name) + ")")
    if len(ticker) >= 2:
        parts.append("(?-i:" + re.escape(ticker) + ")")
    if not parts:
        return None
    return re.compile(r"(?<![\w$])\$?(?:" + "|".join(parts) + r")(?!\w)")


def _mentions(sentence: str, patterns: dict[str, re.Pattern[str] | None]) -> list[tuple[int, int, str]]:
    """Non-overlapping entity mentions in a sentence, sorted by position."""
    found: list[tuple[int, int, str]] = []
    for eid, pattern in patterns.items():
        if pattern is None:
            continue
        found.extend((m.start(), m.end(), eid) for m in pattern.finditer(sentence))
    found.sort()
    kept: list[tuple[int, int, str]] = []
    for start, end, eid in found:
        if kept and start < kept[-1][1]:
            continue
        kept.append((start, end, eid))
    return kept


def _regions(sentence: str, mentions: list[tuple[int, int, str]]) -> list[tuple[str, str]]:
    """Partition a multi-entity sentence so every word belongs to exactly one mention.

    The text between two adjacent mentions is split at its last connector (a comma, ``;``,
    ``:``, ``as``, ``while``, ``after``, ``but``, ``and``, ...). Text before the split belongs to
    the left mention, text after it to the right mention. With no connector, the whole gap goes
    to the nearest preceding mention. Text before the first mention goes to the first mention,
    and text after the last mention goes to the last one.
    """
    count = len(mentions)
    cuts: list[tuple[int, int]] = [(0, 0)] * count  # cuts[i] sits between mention i-1 and i
    for i in range(1, count):
        gap_start, gap_end = mentions[i - 1][1], mentions[i][0]
        connectors = list(_CONNECTOR_RE.finditer(sentence, gap_start, gap_end))
        cuts[i] = (connectors[-1].start(), connectors[-1].end()) if connectors else (gap_end, gap_end)
    regions: list[tuple[str, str]] = []
    for i, (_, _, eid) in enumerate(mentions):
        lo = 0 if i == 0 else cuts[i][1]
        hi = len(sentence) if i == count - 1 else cuts[i + 1][0]
        regions.append((eid, sentence[lo:hi].strip()))
    return regions


def _sentence_pieces(sentence: str, patterns: dict[str, re.Pattern[str] | None]) -> dict[str, str]:
    """Text attributed to each entity mentioned in one sentence.

    A sentence that mentions only one entity is attributed whole. Otherwise the sentence is
    partitioned by ``_regions``, so no word counts for two entities.
    """
    mentions = _mentions(sentence, patterns)
    if not mentions:
        return {}
    if len({eid for _, _, eid in mentions}) == 1:
        return {mentions[0][2]: sentence}
    pieces: dict[str, list[str]] = defaultdict(list)
    for eid, text in _regions(sentence, mentions):
        if text:
            pieces[eid].append(text)
    return {eid: " ".join(parts) for eid, parts in pieces.items()}


def _combine(items: Sequence[SentimentScore]) -> SentimentScore:
    """Confidence-weighted mean of the sentence scores for one entity.

    All items come from one ``score_many`` call, so they share a model name.
    """
    model = items[0].model
    total_weight = sum(s.confidence for s in items)
    if total_weight > 0:
        score = sum(s.score * s.confidence for s in items) / total_weight
    else:
        score = sum(s.score for s in items) / len(items)
    confidence = total_weight / len(items)
    return SentimentScore(
        label=label_for_score(_clamp(score)),
        score=_clamp(score),
        confidence=max(0.0, min(1.0, confidence)),
        model=model,
    )


class _FinBertRuntime:
    """Holds a loaded FinBERT model and runs batched inference under a lock."""

    def __init__(self, tokenizer: Any, model: Any, *, max_length: int, batch_size: int) -> None:
        self._tokenizer = tokenizer
        self._model = model
        self._max_length = max_length
        self._batch_size = batch_size
        self._labels = {int(k): str(v).lower() for k, v in model.config.id2label.items()}
        self._lock = threading.Lock()

    def score(self, texts: Sequence[str], model_name: str) -> list[SentimentScore]:
        """Score texts in batches. Raises whatever the model raises (RuntimeError, ...)."""
        import torch

        results: list[SentimentScore] = []
        for start in range(0, len(texts), self._batch_size):
            chunk = list(texts[start : start + self._batch_size])
            encoded = self._tokenizer(
                chunk,
                padding=True,
                truncation=True,
                max_length=self._max_length,
                return_tensors="pt",
            )
            with self._lock, torch.no_grad():
                logits = self._model(**encoded).logits
            probabilities = torch.softmax(logits, dim=-1).tolist()
            results.extend(self._from_probabilities(row, model_name) for row in probabilities)
        return results

    def _from_probabilities(self, row: Sequence[float], model_name: str) -> SentimentScore:
        by_label = {self._labels.get(i, f"label{i}"): float(p) for i, p in enumerate(row)}
        positive = by_label.get("positive", 0.0)
        negative = by_label.get("negative", 0.0)
        top_index = max(range(len(row)), key=lambda i: row[i])
        return SentimentScore(
            label=_as_label(self._labels.get(top_index, "neutral")),
            score=_clamp(positive - negative),
            confidence=max(0.0, min(1.0, float(row[top_index]))),
            model=model_name,
        )


class SentimentScorer:
    """Financial sentiment with FinBERT when available, otherwise the finance lexicon."""

    def __init__(
        self,
        backend: Backend = "auto",
        *,
        model_name: str = DEFAULT_FINBERT_MODEL,
        max_length: int = 256,
        batch_size: int = 16,
    ) -> None:
        if backend not in ("auto", "finbert", "lexicon"):
            raise ValueError(f"unknown sentiment backend {backend!r}")
        self._requested = backend
        self._model_name = model_name
        self._lexicon = LexiconScorer()
        self._finbert: _FinBertRuntime | None = None
        self._inference_failures = 0
        self._warned_inference = False
        if backend != "lexicon":
            try:
                tokenizer, model = _get_finbert_model(model_name)
                self._finbert = _FinBertRuntime(
                    tokenizer, model, max_length=max_length, batch_size=max(1, batch_size)
                )
            except (ImportError, OSError, RuntimeError) as exc:
                if backend == "finbert":
                    raise RuntimeError(f"FinBERT backend unavailable: {exc}") from exc
                log.warning("FinBERT unavailable (%s); using lexicon backend %s", exc, LEXICON_MODEL)

    @property
    def model(self) -> str:
        """Active backend name, e.g. ``finbert:ProsusAI/finbert`` or ``lexicon:aw-fin-1``."""
        if self._finbert is not None:
            return f"finbert:{self._model_name}"
        return LEXICON_MODEL

    @property
    def backend_name(self) -> Literal["finbert", "lexicon"]:
        return "finbert" if self._finbert is not None else "lexicon"

    def score(self, text: str) -> SentimentScore:
        """Score one text."""
        return self.score_many([text])[0]

    def score_many(
        self, texts: Sequence[str], languages: list[str] | None = None,
    ) -> list[SentimentScore]:
        """Score many texts in order. Empty texts are neutral and never reach a model.

        ``languages`` gives one language code per text (``None`` entries and a ``None`` list mean
        English). Non-English texts are scored by the finance lexicon, which carries Turkish terms,
        and report the lexicon model; English texts use FinBERT when it is active.
        """
        if languages is not None and len(languages) != len(texts):
            raise ValueError("languages must have one entry per text")
        results: list[SentimentScore | None] = [None] * len(texts)
        pending_index: list[int] = []
        pending_text: list[str] = []
        pending_lang: list[str | None] = []
        for index, text in enumerate(texts):
            if not text or not text.strip():
                results[index] = SentimentScore(label="neutral", score=0.0, confidence=0.0, model=self.model)
            else:
                pending_index.append(index)
                pending_text.append(text)
                pending_lang.append(languages[index] if languages is not None else None)
        if pending_text:
            for index, result in zip(pending_index, self._score_pending(pending_text, pending_lang), strict=True):
                results[index] = result
        return [r for r in results if r is not None]

    def _score_pending(self, texts: list[str], languages: list[str | None]) -> list[SentimentScore]:
        """Route non-English texts to the lexicon and the rest to :meth:`_score_english`."""
        results: list[SentimentScore | None] = [None] * len(texts)
        english: list[int] = []
        for index, (text, language) in enumerate(zip(texts, languages, strict=True)):
            if is_english(language):
                english.append(index)
            else:
                results[index] = self._lexicon.score(text)
        if english:
            scored = self._score_english([texts[i] for i in english])
            for index, result in zip(english, scored, strict=True):
                results[index] = result
        return [r for r in results if r is not None]

    def _lexicon_veto(self, text: str, score: SentimentScore) -> SentimentScore:
        """Override a FinBERT positive when a strong negative is not negated in its clause.

        The clause ending at each strong-negative word is scored by the lexicon (which handles
        negation); the strongest negative clause replaces the FinBERT verdict.
        """
        if score.label != "positive":
            return score
        best: SentimentScore | None = None
        for match in _STRONG_NEGATIVE_RE.finditer(text):
            clause_start = max(
                (m.end() for m in _CLAUSE_SPLIT_RE.finditer(text, 0, match.start())), default=0,
            )
            before = _WORD_RE.findall(text[clause_start : match.start()].lower())[-3:]
            if any(word in VETO_NEGATORS for word in before):
                continue
            clause = self._lexicon.score(text[clause_start : match.end()])
            if clause.label == "negative" and (best is None or clause.score < best.score):
                best = clause
        if best is None:
            return score
        return SentimentScore(
            label="negative",
            score=best.score,
            confidence=best.confidence,
            model=f"finbert+lexicon-veto:{self._model_name}",
        )

    def _score_english(self, texts: list[str]) -> list[SentimentScore]:
        """Run FinBERT when active. In auto mode an inference error falls back for this call."""
        if self._finbert is None:
            return [self._lexicon.score(t) for t in texts]
        try:
            scored = self._finbert.score(texts, self.model)
        except (ImportError, OSError, RuntimeError) as exc:
            if self._requested == "finbert":
                raise
            self._inference_failures += 1
            if not self._warned_inference:
                log.warning("FinBERT inference failed (%s); using lexicon for this call", exc)
                self._warned_inference = True
            else:
                log.debug("FinBERT inference failed again (%s); using lexicon", exc)
            if self._inference_failures >= MAX_INFERENCE_FAILURES:
                log.warning(
                    "FinBERT failed %d times in a row; switching to lexicon permanently",
                    self._inference_failures,
                )
                self._finbert = None
            return [self._lexicon.score(t) for t in texts]
        self._inference_failures = 0
        return [self._lexicon_veto(t, s) for t, s in zip(texts, scored, strict=True)]

    def entity_sentiment(
        self,
        title: str,
        text: str | None,
        entities: Iterable[EntityRef],
        *,
        max_sentences_per_entity: int = 4,
        language: str | None = None,
    ) -> dict[str, SentimentScore]:
        """Per-entity sentiment from the sentences (title first) that mention each entity.

        Returns ``{entity.id: SentimentScore}`` only for entities with at least one sentence.
        ``language`` routes the snippets like :meth:`score_many` (non-English uses the lexicon).
        """
        unique: dict[str, EntityRef] = {}
        for entity in entities:
            unique.setdefault(entity.id, entity)
        patterns = {eid: _surface_pattern(e) for eid, e in unique.items()}
        sentences = _split_sentences(title, text)
        if not sentences or not unique:
            return {}

        selected: list[tuple[str, str]] = []
        picked: dict[str, int] = defaultdict(int)
        for sentence in sentences:
            for eid, piece in _sentence_pieces(sentence, patterns).items():
                if picked[eid] >= max_sentences_per_entity:
                    continue
                picked[eid] += 1
                selected.append((eid, piece))
        if not selected:
            return {}

        scores = self.score_many(
            [snippet for _, snippet in selected],
            [language] * len(selected) if language is not None else None,
        )
        grouped: dict[str, list[SentimentScore]] = defaultdict(list)
        for (eid, _), result in zip(selected, scores, strict=True):
            grouped[eid].append(result)
        return {eid: _combine(items) for eid, items in grouped.items()}
