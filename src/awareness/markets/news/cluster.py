"""Story clustering: collapse syndicated copies into one story and count corroboration.

Two items belong to the same story when their normalised titles overlap strongly
(Jaccard over word bigrams and long unigrams), or when their title+summary simhashes are
close AND they share a high-confidence entity. Matching is restricted to a time window
and to items written in the same language family.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from datetime import timedelta
from itertools import pairwise

from awareness.markets.models import NewsItem, SentimentLabel, SentimentScore, Story, stable_id
from awareness.util.hashing import hamming64, simhash64

_STOPWORDS: frozenset[str] = frozenset(
    {
        # English
        "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "by", "for", "from",
        "with", "as", "is", "are", "was", "were", "be", "been", "it", "its", "this", "that",
        "these", "those", "than", "then", "into", "onto", "over", "under", "after", "before",
        "about", "amid", "up", "down", "out", "off", "not", "no", "so", "if", "will", "would",
        "could", "should", "has", "have", "had", "his", "her", "their", "our", "your", "who",
        "what", "when", "where", "why", "how", "new", "says", "say", "said", "vs",
        # Turkish
        "ve", "ile", "bir", "bu", "şu", "da", "de", "ki", "mi", "mu", "mü", "için", "gibi",
        "daha", "çok", "ama", "veya", "ya", "ne", "olan", "olarak", "ise", "kadar", "sonra",
        "önce", "göre", "en", "hem", "ancak", "o", "şey",
        "mı",  # noqa: RUF001 (dotless i is a real Turkish question particle)
    }
)
_PUBLISHER_SEPARATOR = re.compile("\\s+[-|\u2013\u2014]\\s+")
_TOKEN_SPLIT = re.compile(r"[\W_]+", re.UNICODE)
_MAX_SUFFIX_WORDS = 3
_MIN_UNIGRAM_LEN = 4
_HIGH_CONFIDENCE = 0.8
_SENTIMENT_BAND = 0.15
_MAX_CORROBORATION_TIER = 3


def _strip_publisher_suffix(title: str) -> str:
    """Drop a trailing ``" - Reuters"`` / ``" | CNBC"`` segment of at most three words."""
    parts = _PUBLISHER_SEPARATOR.split(title.strip())
    if len(parts) > 1 and len(parts[-1].split()) <= _MAX_SUFFIX_WORDS:
        return " ".join(parts[:-1])
    return title


def title_shingles(title: str) -> set[str]:
    """Normalised title shingles: word bigrams plus unigrams of length >= 4.

    Lower-cases, strips publisher suffixes and punctuation (numbers are kept), and removes
    English and Turkish stopwords before shingling.
    """
    text = unicodedata.normalize("NFKC", _strip_publisher_suffix(title)).lower()
    tokens = [t for t in _TOKEN_SPLIT.split(text) if t and t not in _STOPWORDS]
    shingles = {f"{a} {b}" for a, b in pairwise(tokens)}
    shingles.update(t for t in tokens if len(t) >= _MIN_UNIGRAM_LEN)
    return shingles


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _language_family(item: NewsItem) -> str:
    return (item.language or "en").lower().split("-")[0]


def _sim_text(item: NewsItem) -> str:
    return f"{item.title} {item.summary or ''}"


def _high_confidence_entities(item: NewsItem) -> set[str]:
    return {e.id for e in item.entities if e.confidence >= _HIGH_CONFIDENCE}


def _label_for_score(score: float) -> SentimentLabel:
    if score >= _SENTIMENT_BAND:
        return "positive"
    if score <= -_SENTIMENT_BAND:
        return "negative"
    return "neutral"


class StoryClusterer:
    """Assigns items to stories and builds story records from clustered items."""

    def __init__(
        self,
        *,
        window_hours: int = 36,
        jaccard_threshold: float = 0.5,
        simhash_threshold: int = 10,
    ) -> None:
        self._window = timedelta(hours=window_hours)
        self._jaccard_threshold = jaccard_threshold
        self._simhash_threshold = simhash_threshold

    def find_story(self, item: NewsItem, candidates: list[NewsItem]) -> str | None:
        """Return the ``story_id`` of the best matching candidate, or ``None``.

        A candidate matches when title Jaccard >= the threshold, or when the simhash of
        title+summary is within ``simhash_threshold`` bits AND both items share an entity id
        with confidence >= 0.8. The highest-Jaccard match wins.
        """
        item_shingles = title_shingles(item.title)
        item_sig = simhash64(_sim_text(item))
        item_entities = _high_confidence_entities(item)
        best_story: str | None = None
        best_jaccard = -1.0
        for cand in candidates:
            if cand.story_id is None or cand.id == item.id:
                continue
            if _language_family(cand) != _language_family(item):
                continue
            if abs(cand.published_at - item.published_at) > self._window:
                continue
            jaccard = _jaccard(item_shingles, title_shingles(cand.title))
            matched = jaccard >= self._jaccard_threshold
            if not matched and item_entities & _high_confidence_entities(cand):
                cand_sig = simhash64(_sim_text(cand))
                matched = hamming64(item_sig, cand_sig) <= self._simhash_threshold
            if matched and jaccard > best_jaccard:
                best_story = cand.story_id
                best_jaccard = jaccard
        return best_story

    def new_story_id(self, item: NewsItem) -> str:
        """Deterministic story id for an item that starts a new story."""
        return stable_id("story", item.id)

    @staticmethod
    def corroboration(items: list[NewsItem]) -> int:
        """Distinct publishers of tier <= 3 in the group (at least 1)."""
        publishers = {i.publisher_id for i in items if i.publisher_tier <= _MAX_CORROBORATION_TIER}
        return max(1, len(publishers))

    def build_story(self, items: list[NewsItem], *, story_id: str) -> Story:
        """Build a story from its items. The lead is the best tier, then quality, then earliest."""
        if not items:
            raise ValueError("build_story needs at least one item")
        ordered = sorted(items, key=lambda i: i.published_at)
        lead = min(items, key=lambda i: (i.publisher_tier, -i.quality_score, i.published_at))

        publishers: list[str] = list(dict.fromkeys(i.publisher_id for i in ordered))
        first_seen = min(i.first_seen_at for i in items)
        last_published = max(i.published_at for i in items)

        return Story(
            id=story_id,
            title=lead.title,
            lead_item_id=lead.id,
            item_ids=[i.id for i in ordered],
            publishers=publishers,
            corroboration=self.corroboration(items),
            first_seen_at=first_seen,
            last_updated_at=max(last_published, first_seen),
            symbols=_by_frequency(i.symbols for i in ordered),
            topics=_by_frequency(i.topics for i in ordered),
            countries=_by_frequency(i.countries for i in ordered),
            sentiment=_aggregate_sentiment(ordered),
            importance=max(i.importance for i in items),
        )


def _by_frequency(groups: Iterable[list[str]]) -> list[str]:
    """Union of string lists, most frequent first; ties keep first-seen order."""
    counts: Counter[str] = Counter()
    for group in groups:
        counts.update(group)
    return [value for value, _ in counts.most_common()]


def _aggregate_sentiment(ordered: list[NewsItem]) -> SentimentScore | None:
    scored: list[SentimentScore] = [i.sentiment for i in ordered if i.sentiment is not None]
    if not scored:
        return None
    mean_score = sum(s.score for s in scored) / len(scored)
    mean_conf = sum(s.confidence for s in scored) / len(scored)
    return SentimentScore(
        label=_label_for_score(mean_score),
        score=mean_score,
        confidence=mean_conf,
        model=f"aggregate:{scored[0].model}",
    )
