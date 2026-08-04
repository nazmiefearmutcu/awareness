"""Lexicon-based analysis of tweets captured for an X scraper session.

``analyze_session`` reads every tweet a session captured and returns a plain
dict with author counts, top terms, per-tweet sentiment (reusing the
:mod:`awareness.sentiment.lexicon` word sets), a per-day timeline, and
engagement totals. An empty session yields a fully zeroed dict — never an
exception.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from awareness.sentiment.lexicon import NEGATIONS, NEGATIVE, POSITIVE
from awareness.xscraper.store import SessionStore

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "been", "by", "for",
        "from", "how", "i", "in", "into", "is", "it", "of", "on", "or",
        "over", "that", "the", "this", "to", "was", "we", "were", "what",
        "with", "you", "your",
    }
)
_NEGATION_WINDOW = 3


def _tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens of *text* (punctuation stripped)."""
    return _TOKEN_RE.findall(str(text).lower())


def _score_text(text: str) -> tuple[int, int]:
    """Return ``(pos_hits, neg_hits)`` for one tweet's text.

    A sentiment word within 3 tokens after a negation word (e.g. "not good")
    counts toward the opposite polarity, mirroring the sentiment engine.
    """
    tokens = _tokenize(text)
    pos = 0
    neg = 0
    for i, token in enumerate(tokens):
        window = tokens[max(0, i - _NEGATION_WINDOW):i]
        flipped = any(word in NEGATIONS for word in window)
        if token in POSITIVE:
            if flipped:
                neg += 1
            else:
                pos += 1
        elif token in NEGATIVE:
            if flipped:
                pos += 1
            else:
                neg += 1
    return pos, neg


async def analyze_session(store: SessionStore, session_id: str) -> dict[str, Any]:
    """Aggregate analytics for a session's captured tweets.

    Raises :class:`KeyError` when the session does not exist; a session with
    no tweets returns a zeroed dict.
    """
    session = await store.get_session(session_id)
    if session is None:
        raise KeyError(f"session {session_id!r} not found")
    tweets = await store.list_tweets(session_id)

    authors: Counter[str] = Counter()
    terms: Counter[str] = Counter()
    timeline: Counter[str] = Counter()
    sentiment = {"positive": 0, "negative": 0, "neutral": 0}
    scores: list[float] = []
    likes: list[int] = []
    total_retweets = 0

    for tweet in tweets:
        authors[tweet.username] += 1
        terms.update(token for token in _tokenize(tweet.text) if token not in _STOPWORDS)
        pos, neg = _score_text(tweet.text)
        if pos > neg:
            sentiment["positive"] += 1
        elif neg > pos:
            sentiment["negative"] += 1
        else:
            sentiment["neutral"] += 1
        scores.append((pos - neg) / (pos + neg + 1e-9))
        timeline[tweet.created_at.strftime("%Y-%m-%d")] += 1
        likes.append(tweet.metrics.get("likes", 0))
        total_retweets += tweet.metrics.get("retweets", 0)

    return {
        "session_id": session_id,
        "tweet_count": len(tweets),
        "authors": [
            {"username": username, "count": count}
            for username, count in authors.most_common(10)
        ],
        "top_terms": [
            {"term": term, "count": count}
            for term, count in terms.most_common(10)
        ],
        "sentiment": {**sentiment, "avg_score": round(sum(scores) / len(scores), 4) if scores else 0.0},
        "timeline": [
            {"date": date, "count": count}
            for date, count in sorted(timeline.items())
        ],
        "engagement": {
            "total_likes": sum(likes),
            "total_retweets": total_retweets,
            "avg_likes": round(sum(likes) / len(likes), 2) if likes else 0.0,
        },
    }
