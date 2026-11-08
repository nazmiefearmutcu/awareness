"""Financial sentiment scorer: FinBERT (``ProsusAI/finbert``) with a deterministic lexicon fallback.

``SentimentScorer(backend)`` resolves its backend once, at construction:

* ``"lexicon"`` always uses :class:`~awareness.markets.nlp.lexicon.LexiconScorer`.
* ``"finbert"`` loads the transformer or raises :class:`RuntimeError`.
* ``"auto"`` tries FinBERT and falls back to the lexicon, logging one warning.

The ``model`` property always names the backend that is actually active, for example
``finbert:ProsusAI/finbert`` or ``lexicon:aw-fin-1``.

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

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+|\n+")
_CLAUSE_SPLIT_RE = re.compile(
    r",|;|:|\bwhile\b|\bwhereas\b|\bbut\b|\balthough\b|\bthough\b|\bhowever\b|\bmeanwhile\b",
    re.IGNORECASE,
)

Backend = Literal["auto", "finbert", "lexicon"]


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


def _clamp(value: float) -> float:
    return max(-1.0, min(1.0, value))


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
    """Case-insensitive word-boundary pattern for the entity name, id and (for equities) $TICKER."""
    forms = {f.strip() for f in (entity.name, entity.id) if f and len(f.strip()) >= 2}
    if not forms:
        return None
    alternatives = "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True))
    return re.compile(rf"(?<![\w$])\$?(?:{alternatives})(?!\w)", re.IGNORECASE)


def _clause_for(sentence: str, pattern: re.Pattern[str], others: Sequence[re.Pattern[str]]) -> str:
    """Narrow a sentence to the clauses that mention the entity when other entities co-occur."""
    if not any(other.search(sentence) for other in others):
        return sentence
    clauses = [c.strip() for c in _CLAUSE_SPLIT_RE.split(sentence) if c and c.strip()]
    kept = [c for c in clauses if pattern.search(c)]
    return " ".join(kept) if kept else sentence


def _combine(items: Sequence[SentimentScore], model: str) -> SentimentScore:
    """Confidence-weighted mean of sentence scores for one entity."""
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
        top_label = self._labels.get(top_index, "neutral")
        label: SentimentLabel = top_label if top_label in ("positive", "negative", "neutral") else "neutral"  # type: ignore[assignment]
        return SentimentScore(
            label=label,
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
        self._model_name = model_name
        self._lexicon = LexiconScorer()
        self._finbert: _FinBertRuntime | None = None
        if backend != "lexicon":
            try:
                tokenizer, model = _load_finbert_model(model_name, local_only=False)
                self._finbert = _FinBertRuntime(
                    tokenizer, model, max_length=max_length, batch_size=max(1, batch_size)
                )
            except (ImportError, OSError, RuntimeError) as exc:
                if backend == "finbert":
                    raise RuntimeError(f"FinBERT backend unavailable: {exc}") from exc
                log.warning("FinBERT unavailable (%s); using lexicon backend %s", exc, LEXICON_MODEL)

    @property
    def model(self) -> str:
        """Name of the active backend, e.g. ``finbert:ProsusAI/finbert`` or ``lexicon:aw-fin-1``."""
        if self._finbert is not None:
            return f"finbert:{self._model_name}"
        return LEXICON_MODEL

    @property
    def backend_name(self) -> Literal["finbert", "lexicon"]:
        return "finbert" if self._finbert is not None else "lexicon"

    def score(self, text: str) -> SentimentScore:
        """Score one text."""
        return self.score_many([text])[0]

    def score_many(self, texts: Sequence[str]) -> list[SentimentScore]:
        """Score many texts in order. Empty texts are neutral and never reach the model."""
        results: list[SentimentScore | None] = [None] * len(texts)
        pending_index: list[int] = []
        pending_text: list[str] = []
        for index, text in enumerate(texts):
            if not text or not text.strip():
                results[index] = SentimentScore(label="neutral", score=0.0, confidence=0.0, model=self.model)
            else:
                pending_index.append(index)
                pending_text.append(text)
        if pending_text:
            if self._finbert is not None:
                scored = self._finbert.score(pending_text, self.model)
            else:
                scored = [self._lexicon.score(t) for t in pending_text]
            for index, result in zip(pending_index, scored, strict=True):
                results[index] = result
        return [r for r in results if r is not None]

    def entity_sentiment(
        self,
        title: str,
        text: str | None,
        entities: Iterable[EntityRef],
        *,
        max_sentences_per_entity: int = 4,
    ) -> dict[str, SentimentScore]:
        """Per-entity sentiment from the sentences (title first) that mention each entity.

        Returns ``{entity.id: SentimentScore}`` only for entities with at least one sentence.
        """
        unique: dict[str, EntityRef] = {}
        for entity in entities:
            unique.setdefault(entity.id, entity)
        patterns = {eid: _surface_pattern(e) for eid, e in unique.items()}
        sentences = _split_sentences(title, text)
        if not sentences or not unique:
            return {}

        selected: list[tuple[str, str]] = []
        for eid, pattern in patterns.items():
            if pattern is None:
                continue
            others = [p for oid, p in patterns.items() if oid != eid and p is not None]
            picked = 0
            for sentence in sentences:
                if picked >= max_sentences_per_entity:
                    break
                if pattern.search(sentence):
                    selected.append((eid, _clause_for(sentence, pattern, others)))
                    picked += 1
        if not selected:
            return {}

        scores = self.score_many([snippet for _, snippet in selected])
        grouped: dict[str, list[SentimentScore]] = defaultdict(list)
        for (eid, _), result in zip(selected, scores, strict=True):
            grouped[eid].append(result)
        return {eid: _combine(items, self.model) for eid, items in grouped.items()}
