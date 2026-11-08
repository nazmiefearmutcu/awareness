"""Story clustering: collapse syndicated and paraphrased copies into one story.

Two items belong to the same story when:

* their normalised title shingles overlap with Jaccard >= ``jaccard_threshold``; or
* they share a high-confidence entity and the Jaccard is >= ``entity_jaccard_threshold``; or
* they share a high-confidence entity and the title+summary simhashes are within
  ``simhash_threshold`` bits.

Matching is restricted to a time window and to items in the same language family, and two
items whose titles carry different numbers never match (different figures are different
events). Title normalisation maps common finance verbs and nouns to canonical forms
(``beats``/``tops`` to ``beat``, ``soars``/``surges`` to ``rise``) and applies light stemming,
so paraphrased wire headlines reach the same shingles.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from datetime import timedelta
from itertools import pairwise

from awareness.markets.models import (
    NewsItem,
    SentimentLabel,
    SentimentScore,
    Story,
    stable_id,
)
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

# Words that end in "s" but are not plurals; light stemming leaves them alone.
_NO_STEM: frozenset[str] = frozenset({"news", "bias", "crisis", "virus", "status", "bonus"})


def _synonym_map() -> dict[str, str]:
    groups: dict[str, tuple[str, ...]] = {
        "beat": ("beats", "beat", "beaten", "beating", "tops", "topped", "topping",
                 "exceeds", "exceeded", "exceeding"),
        "miss": ("misses", "missed", "missing", "miss"),
        "rise": ("soars", "soar", "soared", "soaring", "surges", "surge", "surged", "surging",
                 "jumps", "jump", "jumped", "jumping", "rallies", "rally", "rallied", "rallying"),
        "fall": ("plunges", "plunge", "plunged", "plunging", "slumps", "slump", "slumped",
                 "slumping", "tumbles", "tumble", "tumbled", "tumbling", "sinks", "sink",
                 "sank", "sinking"),
        "estimate": ("estimates", "estimate", "forecasts", "forecast", "expectations",
                     "expectation"),
    }
    return {form: canonical for canonical, forms in groups.items() for form in forms}


_SYNONYMS: dict[str, str] = _synonym_map()
_DATACENTER = re.compile(r"\bdata[\s-]*cent(?:er|re)s?\b")
_FELL_SHORT = re.compile(r"\b(?:falls?|fell|falling)\s+short\b")
_TOKEN_SPLIT = re.compile(r"[\W_]+", re.UNICODE)
_NUMBER = re.compile(r"\d+")
_PUBLISHER_SEPARATOR = re.compile("\\s+[-|\u2013\u2014]\\s+")
_MIN_UNIGRAM_LEN = 4
_HIGH_CONFIDENCE = 0.8
_SENTIMENT_BAND = 0.15
_MAX_CORROBORATION_TIER = 3


# (suffix, minimum length, replacement) tried in order; the first match wins.
_STEM_RULES: tuple[tuple[str, int, str], ...] = (
    ("ies", 4, "y"),
    ("sses", 3, "ss"),
    ("ing", 5, ""),
    ("ed", 4, ""),
    ("ches", 3, "ch"),
    ("shes", 3, "sh"),
    ("xes", 3, "x"),
    ("zes", 3, "z"),
)


def _light_stem(token: str) -> str:
    """Strip common English plural and verb endings. Deliberately conservative."""
    if len(token) <= 3 or not token.isalpha() or token in _NO_STEM:
        return token
    for suffix, min_len, replacement in _STEM_RULES:
        if token.endswith(suffix) and len(token) > min_len:
            return token[: -len(suffix)] + replacement
    if token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def _normalise_tokens(text: str) -> list[str]:
    """Lower-case, fold phrases and synonyms, split on punctuation, stem, drop stopwords."""
    folded = unicodedata.normalize("NFKC", text).replace("\u0130", "i").lower()
    folded = _DATACENTER.sub(" datacenter ", folded)
    folded = _FELL_SHORT.sub(" miss ", folded)
    tokens: list[str] = []
    for raw in _TOKEN_SPLIT.split(folded):
        if not raw or raw in _STOPWORDS:
            continue
        canonical = _SYNONYMS.get(raw) or _light_stem(raw)
        if canonical and canonical not in _STOPWORDS:
            tokens.append(canonical)
    return tokens


def _strip_publisher_suffix(title: str, publisher_names: frozenset[str]) -> str:
    """Drop a trailing ``" - Reuters"`` segment only when it names a known publisher."""
    if not publisher_names:
        return title
    parts = _PUBLISHER_SEPARATOR.split(title.strip())
    if len(parts) > 1 and parts[-1].strip().lower() in publisher_names:
        return " ".join(parts[:-1])
    return title


def _names(publisher_names: Iterable[str] | None) -> frozenset[str]:
    return frozenset(n.strip().lower() for n in (publisher_names or ()) if n.strip())


def title_shingles(title: str, publisher_names: Iterable[str] | None = None) -> set[str]:
    """Normalised title shingles: word bigrams plus unigrams of length >= 4.

    Publisher suffixes are removed only when they match one of ``publisher_names`` (the
    registry's names and aliases). Numbers are kept. Synonyms, light stemming and English and
    Turkish stopword removal are applied before shingling.
    """
    tokens = _normalise_tokens(_strip_publisher_suffix(title, _names(publisher_names)))
    shingles = {f"{a} {b}" for a, b in pairwise(tokens)}
    shingles.update(t for t in tokens if len(t) >= _MIN_UNIGRAM_LEN)
    return shingles


def _numbers(title: str) -> frozenset[str]:
    return frozenset(_NUMBER.findall(title))


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
        entity_jaccard_threshold: float = 0.3,
        publisher_names: Iterable[str] | None = None,
    ) -> None:
        self._window = timedelta(hours=window_hours)
        self._jaccard_threshold = jaccard_threshold
        self._simhash_threshold = simhash_threshold
        self._entity_jaccard_threshold = entity_jaccard_threshold
        self._publisher_names = _names(publisher_names)

    def find_story(self, item: NewsItem, candidates: list[NewsItem]) -> str | None:
        """Return the ``story_id`` of the best matching candidate, or ``None``.

        A candidate matches when its title Jaccard is >= ``jaccard_threshold``; or when it
        shares a high-confidence entity and the Jaccard is >= ``entity_jaccard_threshold``; or
        when it shares a high-confidence entity and the title+summary simhash is within
        ``simhash_threshold`` bits. Candidates whose titles contain different numbers never
        match. The highest-Jaccard match wins.
        """
        item_shingles = title_shingles(item.title, self._publisher_names)
        item_numbers = _numbers(item.title)
        item_entities = _high_confidence_entities(item)
        item_sig: int | None = None  # computed lazily: only needed for entity-sharing pairs
        best_story: str | None = None
        best_jaccard = -1.0
        for cand in candidates:
            if cand.story_id is None or cand.id == item.id:
                continue
            if _language_family(cand) != _language_family(item):
                continue
            if abs(cand.published_at - item.published_at) > self._window:
                continue
            cand_numbers = _numbers(cand.title)
            if item_numbers and cand_numbers and item_numbers != cand_numbers:
                continue
            jaccard = _jaccard(item_shingles, title_shingles(cand.title, self._publisher_names))
            shared = item_entities & _high_confidence_entities(cand)
            matched = jaccard >= self._jaccard_threshold or (
                bool(shared) and jaccard >= self._entity_jaccard_threshold
            )
            if not matched and shared:
                if item_sig is None:
                    item_sig = simhash64(_sim_text(item))
                matched = hamming64(item_sig, simhash64(_sim_text(cand))) <= self._simhash_threshold
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
