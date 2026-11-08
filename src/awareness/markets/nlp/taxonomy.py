"""Rule-based finance taxonomy: asset classes, topics, relevance, content type, horizon.

All vocabulary lives in ``data/taxonomy.yaml``; this module only scores it. Text is folded
(:func:`awareness.markets.nlp.text.fold_text`) and matched with word-boundary regexes, so
English and Turkish aliases behave the same way regardless of case or accents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from awareness.markets.config import PACKAGE_DATA_DIR
from awareness.markets.models import EntityRef, Publisher
from awareness.markets.nlp.text import fold_text

TITLE_WEIGHT = 2.0
BODY_WEIGHT = 1.0
TOPIC_KEEP = 1.5
TOPIC_KEEP_IN_TITLE = 1.0
TOPIC_LIMIT = 4
ENTITY_TOPIC_HINT = 1.5
HINT_MIN_CONFIDENCE = 0.8
FINANCE_ENTITY_TYPES = frozenset({"equity", "crypto", "fx", "commodity", "index", "central_bank"})
ENTITY_EVIDENCE_CONF = 0.8
ENTITY_EVIDENCE_SALIENCE = 0.6  # body-only mentions below this do not count as finance evidence
ENTITY_EVIDENCE_BONUS = 0.4
TOPIC_EVIDENCE_STEP = 0.15
TOPIC_EVIDENCE_CAP = 0.45
PUBLISHER_BONUS = 0.2
PUBLISHER_PENALTY = -0.1
FEED_BONUS = 0.15
PRIOR_CAP = 0.1  # publisher and feed priors together move relevance by at most this much
NEGATIVE_TITLE = -0.5
NEGATIVE_BODY = -0.2


@dataclass
class TaxonomyResult:
    """Output of :meth:`TaxonomyClassifier.classify`."""

    asset_classes: list[str]
    topics: list[str]
    finance_relevance: float
    content_type_hint: str | None
    horizon: str | None
    topic_scores: dict[str, float] = field(default_factory=dict)
    negative_hit: bool = False  # a non-finance class (sports, celebrity, lifestyle …) matched


@dataclass(frozen=True, slots=True)
class _Pattern:
    name: str
    regex: re.Pattern[str]
    weight: float = 1.0
    strip: re.Pattern[str] | None = None  # topic-specific false phrases, removed first


def _compile(aliases: list[str]) -> re.Pattern[str] | None:
    """One word-boundary alternation over folded aliases (longest first)."""
    folded = sorted({fold_text(a).strip() for a in aliases if a and a.strip()}, key=len, reverse=True)
    if not folded:
        return None
    body = "|".join(re.escape(a).replace(r"\ ", " ") for a in folded)
    return re.compile(rf"(?<![^\W_])(?:{body})(?![^\W_])")


def _matches(pattern: re.Pattern[str] | None, text: str) -> set[str]:
    if pattern is None or not text:
        return set()
    return {m.group(0) for m in pattern.finditer(text)}


def _has_finance_entity(entities: list[EntityRef]) -> bool:
    """A confident, salient market entity: it outweighs non-finance vocabulary in the same item.

    Salience must reach ``ENTITY_EVIDENCE_SALIENCE`` so that a passing body mention (a dollar
    figure, a ticker quoted in an unrelated story) cannot lift relevance or cancel a negative.
    """
    return any(
        e.type in FINANCE_ENTITY_TYPES
        and e.confidence >= ENTITY_EVIDENCE_CONF
        and e.salience >= ENTITY_EVIDENCE_SALIENCE
        for e in entities
    )


class TaxonomyClassifier:
    """Scores a headline (and optional body) against the taxonomy in ``taxonomy.yaml``."""

    def __init__(self, config_path: Path | None = None) -> None:
        path = Path(config_path) if config_path is not None else PACKAGE_DATA_DIR / "taxonomy.yaml"
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        self._asset: list[tuple[str, frozenset[str], _Pattern | None]] = []
        for name, raw in (data.get("asset_classes") or {}).items():
            spec = raw or {}
            pattern = _compile(list(spec.get("aliases") or []))
            self._asset.append(
                (name, frozenset(spec.get("entity_types") or []), _Pattern(name, pattern))
            )
        self._hints: dict[str, str] = dict(data.get("entity_topic_hints") or {})
        exclusions = data.get("topic_exclusions") or {}
        self._topics: dict[str, _Pattern] = {}
        self._topic_weight: dict[str, float] = {}
        for name, raw in (data.get("topics") or {}).items():
            spec = raw or {}
            weight = float(spec.get("weight", 1.0))
            self._topic_weight[name] = weight
            pattern = _compile(list(spec.get("aliases") or [])) or re.compile(r"(?!x)x")
            strip = _compile(list(exclusions.get(name) or []))
            self._topics[name] = _Pattern(name, pattern, weight, strip)
        self._negative: dict[str, _Pattern] = {}
        for name, raw in (data.get("negative") or {}).items():
            pattern = _compile(list((raw or {}).get("aliases") or []))
            if pattern is not None:
                self._negative[name] = _Pattern(name, pattern)
        horizon = data.get("horizon") or {}
        self._intraday = _compile(list(horizon.get("intraday") or []))
        self._one_week_topics = frozenset(horizon.get("one_week_topics") or [])
        self._one_week_aliases = _compile(list(horizon.get("one_week_aliases") or []))
        self._structural_topics = frozenset(horizon.get("structural_topics") or [])
        self._macro_topics = frozenset(data.get("macro_topics") or [])
        self._macro_alias = next((p for n, _, p in self._asset if n == "macro"), None)
        publisher = data.get("publisher_relevance") or {}
        self._pub_boost = frozenset(publisher.get("boost") or [])
        self._pub_penalty = frozenset(publisher.get("penalty") or [])
        self._feed_categories = frozenset(str(c).lower() for c in data.get("feed_relevance_categories") or [])
        content = data.get("content_type") or {}
        self._opinion = re.compile(
            r"^(?:" + "|".join(re.escape(fold_text(p)) for p in content.get("opinion_prefixes") or [])
            + r")\s*[:\uff1a]"
        )
        self._analysis_prefix = re.compile(
            r"^(?:" + "|".join(re.escape(fold_text(p)) for p in content.get("analysis_prefixes") or [])
            + r")\b"
        )
        self._analysis_patterns = [re.compile(p) for p in content.get("analysis_patterns") or []]

    # ── public API ────────────────────────────────────────────────────────

    def classify(
        self,
        title: str,
        text: str | None,
        entities: list[EntityRef],
        *,
        publisher: Publisher | None = None,
        feed_categories: list[str] | None = None,
    ) -> TaxonomyResult:
        """Classify one item. Never raises on empty input; unknown evidence simply scores 0."""
        ft = fold_text(title or "")
        fb = fold_text(text or "")
        topic_scores, topic_in_title = self._score_topics(ft, fb, entities)
        topics = self._select_topics(topic_scores, topic_in_title)
        asset_classes = self._asset_classes(ft, fb, entities, topics)
        negatives = 0.0 if _has_finance_entity(entities) else self._negative_hits(ft, fb)
        relevance = self._relevance(entities, topics, negatives, publisher, feed_categories)
        return TaxonomyResult(
            asset_classes=asset_classes,
            topics=topics,
            finance_relevance=round(relevance, 3),
            content_type_hint=self._content_type(title or "", publisher),
            horizon=self._horizon(ft, fb, topics),
            topic_scores={t: round(s, 3) for t, s in topic_scores.items() if s > 0},
            negative_hit=negatives < 0,
        )

    # ── scoring ───────────────────────────────────────────────────────────

    def _score_topics(
        self, ft: str, fb: str, entities: list[EntityRef],
    ) -> tuple[dict[str, float], set[str]]:
        scores: dict[str, float] = {}
        in_title: set[str] = set()
        for name, pattern in self._topics.items():
            title_text, body_text = ft, fb
            if pattern.strip is not None:
                title_text = pattern.strip.sub(" ", ft)
                body_text = pattern.strip.sub(" ", fb)
            title_hits = _matches(pattern.regex, title_text)
            body_hits = _matches(pattern.regex, body_text) - title_hits
            score = pattern.weight * (TITLE_WEIGHT * len(title_hits) + BODY_WEIGHT * len(body_hits))
            if title_hits:
                in_title.add(name)
            if score > 0:
                scores[name] = score
        for entity in entities:
            topic = self._hints.get(entity.type)
            if topic and entity.confidence >= HINT_MIN_CONFIDENCE:
                scores[topic] = scores.get(topic, 0.0) + ENTITY_TOPIC_HINT
        return scores, in_title

    def _select_topics(self, scores: dict[str, float], in_title: set[str]) -> list[str]:
        kept = [
            (name, score)
            for name, score in scores.items()
            if score >= TOPIC_KEEP or (name in in_title and score >= TOPIC_KEEP_IN_TITLE)
        ]
        kept.sort(key=lambda pair: (-pair[1], pair[0]))
        return [name for name, _ in kept[:TOPIC_LIMIT]]

    def _asset_classes(
        self, ft: str, fb: str, entities: list[EntityRef], topics: list[str],
    ) -> list[str]:
        found: set[str] = set()
        entity_types = {e.type for e in entities if e.confidence >= 0.6}
        for name, types, pattern in self._asset:
            if name == "macro":
                continue
            if types & entity_types:
                found.add(name)
            if pattern is not None and (_matches(pattern.regex, ft) or _matches(pattern.regex, fb)):
                found.add(name)
        if self._macro_topics & set(topics) or _matches(
            self._macro_alias.regex if self._macro_alias else None, ft + " " + fb,
        ):
            found.add("macro")
        order = [name for name, _, _ in self._asset]
        return [name for name in order if name in found]

    def _negative_hits(self, ft: str, fb: str) -> float:
        total = 0.0
        for pattern in self._negative.values():
            if _matches(pattern.regex, ft):
                total += NEGATIVE_TITLE
            elif _matches(pattern.regex, fb):
                total += NEGATIVE_BODY
        return total

    def _relevance(
        self,
        entities: list[EntityRef],
        topics: list[str],
        negatives: float,
        publisher: Publisher | None,
        feed_categories: list[str] | None,
    ) -> float:
        score = 0.0
        if _has_finance_entity(entities):
            score += ENTITY_EVIDENCE_BONUS
        score += min(TOPIC_EVIDENCE_CAP, TOPIC_EVIDENCE_STEP * len(topics))
        priors = 0.0
        if publisher is not None:
            if publisher.kind in self._pub_boost:
                priors += PUBLISHER_BONUS
            elif publisher.kind in self._pub_penalty:
                priors += PUBLISHER_PENALTY
        if feed_categories and any(c.lower() in self._feed_categories for c in feed_categories):
            priors += FEED_BONUS
        score += max(-PRIOR_CAP, min(PRIOR_CAP, priors))
        score += negatives
        return min(1.0, max(0.0, score))

    # ── labels ────────────────────────────────────────────────────────────

    def _content_type(self, title: str, publisher: Publisher | None) -> str | None:
        folded = fold_text(title).strip()
        if self._opinion.match(folded) or folded.startswith("my take"):
            return "opinion"
        if publisher is not None and publisher.kind == "community":
            return "social"
        if self._analysis_prefix.match(folded) or any(p.match(folded) for p in self._analysis_patterns):
            return "analysis"
        if publisher is not None:
            if publisher.kind == "press_release":
                return "press_release"
            if publisher.kind in {"official", "regulator"}:
                return "official"
        return None

    def _horizon(self, ft: str, fb: str, topics: list[str]) -> str | None:
        combined = ft + " " + fb
        if self._intraday is not None and _matches(self._intraday, ft + " " + fb):
            return "intraday"
        if set(topics) & self._one_week_topics or _matches(self._one_week_aliases, combined):
            return "one_week"
        if set(topics) & self._structural_topics:
            return "structural"
        return None
