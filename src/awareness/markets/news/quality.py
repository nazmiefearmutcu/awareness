"""News quality gate: hard rejects, an explainable 0-100 score, labels and importance.

The gate decides what is visible: default views show only ``premium`` and ``standard``.
Every score component adds a reason string (``"tier2:+38"``, ``"clickbait:-25"``) so the
UI can explain the verdict. Rejected items carry only their reject reasons and score 0.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, cast, get_args
from urllib.parse import urlsplit

import yaml

from awareness.markets.models import (
    ContentType,
    EntityRef,
    NewsItem,
    Publisher,
    QualityLabel,
    ensure_utc,
)

logger = logging.getLogger(__name__)

RULES_PATH = Path(__file__).resolve().parents[1] / "data" / "quality_rules.yaml"

_CONTENT_TYPES: frozenset[str] = frozenset(get_args(ContentType))
_OFFICIAL_KINDS = frozenset({"official", "regulator"})
_TIER_BASE: dict[int, int] = {1: 45, 2: 38, 3: 28, 4: 15, 5: 5}
_IMPORTANCE_TIER_BASE: dict[int, int] = {1: 30, 2: 24, 3: 16, 4: 8, 5: 2}
_TOPIC_WEIGHTS: dict[str, int] = {
    "central_bank": 25,
    "inflation": 20,
    "employment": 20,
    "growth_recession": 15,
    "earnings": 15,
    "guidance": 15,
    "m_and_a": 15,
    "geopolitics": 15,
    "credit_stress": 15,
    "funding_liquidity": 15,
    "sanctions": 12,
    "trade_tariffs": 12,
    "fraud_governance": 12,
    "regulation": 10,
    "energy": 10,
    "litigation": 8,
    "ipo": 8,
    "analyst_rating": 6,
    "management_change": 6,
    "product_launch": 5,
}
_DEFAULT_TOPIC_WEIGHT = 4
_MAX_CORROBORATION_POINTS = 15
_MAX_CORROBORATION_IMPORTANCE = 20
_FUTURE_TOLERANCE = timedelta(days=1)
_MIDNIGHT = time(0, 0)

_OPINION_PREFIX = re.compile(r"^(opinion|column|commentary|editorial)\s*:")
_ANALYSIS_PREFIX = re.compile(r"^(analysis|explainer)\s*:|^q&a\b")


@dataclass
class QualityVerdict:
    """Outcome of :meth:`QualityGate.evaluate`."""

    score: float
    label: QualityLabel
    reasons: list[str] = field(default_factory=list)
    content_type: ContentType = "news"
    hard_reject: bool = False


@dataclass(frozen=True)
class _Rules:
    clickbait_title: tuple[re.Pattern[str], ...]
    clickbait_summary: tuple[re.Pattern[str], ...]
    mega_caps: frozenset[str]
    key_countries: frozenset[str]


def _compile(patterns: Sequence[Any]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(str(p), re.IGNORECASE) for p in patterns)


def load_rules(path: Path) -> _Rules:
    """Parse and compile a quality rules YAML file. Raises on malformed content."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"quality rules {path} must be a mapping")
    clickbait = data.get("clickbait") or {}
    return _Rules(
        clickbait_title=_compile(clickbait.get("title") or []),
        clickbait_summary=_compile(clickbait.get("summary") or []),
        mega_caps=frozenset(str(x).upper() for x in data.get("mega_cap_equities") or []),
        key_countries=frozenset(str(x).upper() for x in data.get("key_countries") or []),
    )


def _label_for(score: float) -> QualityLabel:
    if score >= 60:
        return "premium"
    if score >= 40:
        return "standard"
    return "low"


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def _ident(entity: EntityRef) -> str:
    """Normalised entity id: upper-case, without a ``prefix:`` namespace."""
    return entity.id.upper().rsplit(":", 1)[-1]


_CONTENT_BY_KIND: dict[str, ContentType] = {
    "official": "official",
    "regulator": "official",
    "press_release": "press_release",
    "community": "social",
}


def _content_from_title_or_url(item: NewsItem) -> ContentType:
    title = item.title.strip().lower()
    if _OPINION_PREFIX.search(title) or _is_seeking_alpha_article(item):
        return "opinion"
    if _ANALYSIS_PREFIX.search(title):
        return "analysis"
    return "news"


def _is_seeking_alpha_article(item: NewsItem) -> bool:
    parts = urlsplit(item.url)
    host = (parts.hostname or "").lower()
    return host.endswith("seekingalpha.com") and "/article/" in parts.path


class QualityGate:
    """Hard-rejects unusable items and scores the rest on a 0-100 scale."""

    def __init__(self, *, max_age_days: int = 7, rules_path: Path | None = None) -> None:
        self._max_age = timedelta(days=max_age_days)
        self._rules = load_rules(rules_path or RULES_PATH)

    # ── content type ────────────────────────────────────────────────────

    def classify_content_type(
        self, item: NewsItem, publisher: Publisher | None, hint: str | None = None
    ) -> ContentType:
        """Resolve the content type: a valid hint wins, then publisher kind, then title/URL."""
        if hint is not None and hint in _CONTENT_TYPES:
            return cast(ContentType, hint)
        kind = publisher.kind if publisher is not None else None
        by_kind = _CONTENT_BY_KIND.get(kind) if kind is not None else None
        if by_kind is not None:
            return by_kind
        return _content_from_title_or_url(item)

    # ── verdict ─────────────────────────────────────────────────────────

    def evaluate(
        self,
        item: NewsItem,
        publisher: Publisher | None,
        *,
        now: datetime,
        finance_relevance: float,
        corroboration: int = 1,
        timestamp_source: Literal["feed", "article", "fetched"] = "feed",
        content_type_hint: str | None = None,
    ) -> QualityVerdict:
        """Gate one enriched item. ``item`` must already carry publisher fields and entities."""
        now_utc = ensure_utc(now)
        assert now_utc is not None
        content_type = self.classify_content_type(item, publisher, content_type_hint)

        rejects = self._hard_rejects(
            item, publisher, now_utc, finance_relevance, timestamp_source, content_type
        )
        if rejects:
            return QualityVerdict(
                score=0.0, label="rejected", reasons=rejects,
                content_type=content_type, hard_reject=True,
            )

        assert publisher is not None  # guaranteed by the unknown_publisher reject
        components = self._score_components(
            item, publisher, finance_relevance, corroboration, timestamp_source, content_type
        )
        reasons = [name for name, _ in components]
        score = _clamp(sum(delta for _, delta in components))
        label = _label_for(score)
        if publisher.kind in _OFFICIAL_KINDS and label == "low":
            label = "standard"
            reasons.append("official_floor")
        return QualityVerdict(
            score=score, label=label, reasons=reasons,
            content_type=content_type, hard_reject=False,
        )

    # ── hard rejects ────────────────────────────────────────────────────

    def _hard_rejects(
        self,
        item: NewsItem,
        publisher: Publisher | None,
        now: datetime,
        finance_relevance: float,
        timestamp_source: str,
        content_type: str,
    ) -> list[str]:
        reasons: list[str] = []
        if publisher is None:
            reasons.append("unknown_publisher")
        parts = urlsplit(item.url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            reasons.append("missing_url")
        title = item.title.strip()
        if not title:
            reasons.append("missing_title")
        if timestamp_source == "fetched":
            reasons.append("missing_timestamp")
        published = ensure_utc(item.published_at)
        assert published is not None
        if published < now - self._max_age:
            reasons.append("stale")
        if published > now + _FUTURE_TOLERANCE:
            reasons.append("future_timestamp")
        if len(title) < 20:
            reasons.append("title_too_short")
        kind = publisher.kind if publisher is not None else None
        if (
            finance_relevance < 0.25
            and kind not in _OFFICIAL_KINDS
            and content_type != "social"
        ):
            reasons.append("non_finance")
        if kind == "community" and finance_relevance < 0.5:
            reasons.append("community_low_signal")
        return reasons

    # ── score components ────────────────────────────────────────────────

    def _score_components(
        self,
        item: NewsItem,
        publisher: Publisher,
        finance_relevance: float,
        corroboration: int,
        timestamp_source: str,
        content_type: str,
    ) -> list[tuple[str, float]]:
        out: list[tuple[str, float]] = []
        base = _TIER_BASE[publisher.tier]
        out.append((f"tier{publisher.tier}:+{base}", base))

        summary = (item.summary or "").strip()
        out.extend(self._depth(item, summary))

        if item.author and item.author.strip():
            out.append(("author:+5", 5))
        published = ensure_utc(item.published_at)
        assert published is not None
        if timestamp_source in {"feed", "article"} and published.time() != _MIDNIGHT:
            out.append(("time_precise:+3", 3))

        if any(e.confidence >= 0.8 for e in item.entities):
            out.append(("entity_high_conf:+7", 7))
        if item.topics:
            out.append(("topic:+5", 5))

        extra = max(0, corroboration - 1)
        corro_pts = min(_MAX_CORROBORATION_POINTS, 5 * extra)
        if corro_pts > 0:
            out.append((f"corroboration:+{corro_pts}", corro_pts))

        out.extend(self._penalties(item, publisher, summary, content_type))
        return out

    @staticmethod
    def _depth(item: NewsItem, summary: str) -> list[tuple[str, float]]:
        if item.word_count >= 300:
            return [("depth_body300:+20", 20)]
        if item.word_count >= 120:
            return [("depth_body120:+12", 12)]
        if len(summary) >= 140:
            return [("depth_summary140:+8", 8)]
        if len(summary) >= 60:
            return [("depth_summary60:+4", 4)]
        return []

    def _penalties(
        self, item: NewsItem, publisher: Publisher, summary: str, content_type: str
    ) -> list[tuple[str, float]]:
        out: list[tuple[str, float]] = []
        title = item.title.strip()

        clickbait = any(p.search(title) for p in self._rules.clickbait_title) or any(
            p.search(summary) for p in self._rules.clickbait_summary
        )
        if clickbait:
            out.append(("clickbait:-25", -25))

        letters = [c for c in title if c.isalpha()]
        if len(letters) >= 12 and sum(c.isupper() for c in letters) / len(letters) >= 0.7:
            out.append(("all_caps_title:-10", -10))

        if content_type == "opinion":
            out.append(("opinion:-5", -5))

        has_issuer_anchor = any(
            e.type == "equity" and e.salience == 1.0 for e in item.entities
        )
        if (publisher.kind == "press_release" or content_type == "press_release") and not has_issuer_anchor:
            out.append(("press_release:-10", -10))

        if publisher.paywalled and not summary:
            out.append(("paywalled_no_summary:-10", -10))

        if title.endswith("?") and not item.entities:
            out.append(("question_no_entity:-5", -5))

        if item.summary is not None and (summary == title or len(summary) < 25):
            out.append(("boilerplate_summary:-3", -3))
        return out


def _topic_weight(topic: str) -> int:
    return _TOPIC_WEIGHTS.get(topic.lower(), _DEFAULT_TOPIC_WEIGHT)


def compute_importance(item: NewsItem, *, corroboration: int = 1) -> float:
    """Deterministic 0-100 importance of a gated item.

    Call after the quality label is set: ``rejected`` yields 0 and ``low`` is scaled by 0.6.
    """
    if item.quality_label == "rejected":
        return 0.0
    rules = _default_rules()

    score = float(_IMPORTANCE_TIER_BASE.get(item.publisher_tier, 2))
    if item.content_type == "official":
        score += 15
    weights = sorted((_topic_weight(t) for t in item.topics), reverse=True)[:2]
    score += sum(weights)
    if any(_is_salient_anchor(e, rules) for e in item.entities):
        score += 10
    extra = max(0, corroboration - 1)
    score += min(_MAX_CORROBORATION_IMPORTANCE, 5 * extra)

    score = _clamp(score)
    if item.quality_label == "low":
        score *= 0.6
    return score


def _is_salient_anchor(entity: EntityRef, rules: _Rules) -> bool:
    if entity.salience != 1.0:
        return False
    if entity.type in {"index", "central_bank"}:
        return True
    if entity.type == "country":
        return _ident(entity) in rules.key_countries
    if entity.type == "equity":
        return _ident(entity) in rules.mega_caps
    return False


@lru_cache(maxsize=1)
def _default_rules() -> _Rules:
    """Packaged rules, loaded once. Importance always uses the packaged rules."""
    return load_rules(RULES_PATH)


__all__ = ["RULES_PATH", "QualityGate", "QualityVerdict", "compute_importance", "load_rules"]
