"""Lexicon-based analysis of tweets captured for an X scraper session.

``analyze_session`` reads every tweet a session captured and returns a plain
dict with author counts, top terms, per-tweet sentiment (reusing the
:mod:`awareness.sentiment.lexicon` word sets), a per-day sentiment trend and
timeline, and engagement totals. An empty session yields a fully zeroed dict —
never an exception. ``export_tweets_csv`` writes a session's tweets out as
UTF-8 CSV.
"""

from __future__ import annotations

import csv
import os
import re
from collections import Counter
from pathlib import Path
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


def _score_tweet(text: str) -> tuple[int, int]:
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
    trend: dict[str, dict[str, Any]] = {}
    scores: list[float] = []
    likes: list[int] = []
    total_retweets = 0

    for tweet in tweets:
        authors[tweet.username] += 1
        terms.update(token for token in _tokenize(tweet.text) if token not in _STOPWORDS)
        date = tweet.created_at.strftime("%Y-%m-%d")
        pos, neg = _score_tweet(tweet.text)
        day = trend.setdefault(date, {"pos": 0, "neg": 0, "scores": []})
        if pos > neg:
            sentiment["positive"] += 1
            day["pos"] += 1
        elif neg > pos:
            sentiment["negative"] += 1
            day["neg"] += 1
        else:
            sentiment["neutral"] += 1
        score = (pos - neg) / (pos + neg + 1e-9)
        scores.append(score)
        day["scores"].append(score)
        timeline[date] += 1
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
        "sentiment_trend": [
            {
                "date": date,
                "pos": day["pos"],
                "neg": day["neg"],
                "avg_score": round(sum(day["scores"]) / len(day["scores"]), 4) if day["scores"] else 0.0,
            }
            for date, day in sorted(trend.items())
        ],
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


async def export_tweets_csv(
    store: SessionStore,
    session_id: str,
    out_path: str | Path,
    limit: int = 500,
) -> int:
    """Write a session's tweets to *out_path* as UTF-8 CSV; return row count.

    Rows carry (tweet_id, created_at, username, text, likes, retweets, lang,
    source). The file is written to a sibling ``.tmp`` path first and
    atomically replaced, so an interrupted export never leaves a half-written
    file. Raises :class:`KeyError` for an unknown session; an empty session
    writes a header-only file and returns 0.
    """
    session = await store.get_session(session_id)
    if session is None:
        raise KeyError(f"session {session_id!r} not found")
    tweets = await store.list_tweets(session_id, limit=limit)
    destination = Path(out_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = destination.with_name(destination.name + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                ["tweet_id", "created_at", "username", "text", "likes", "retweets", "lang", "source"]
            )
            for tweet in tweets:
                writer.writerow(
                    [
                        tweet.tweet_id,
                        tweet.created_at.isoformat(),
                        tweet.username,
                        tweet.text,
                        tweet.metrics.get("likes", 0),
                        tweet.metrics.get("retweets", 0),
                        tweet.lang or "",
                        tweet.source,
                    ]
                )
        os.replace(tmp_path, destination)
    except Exception:
        # W1 finding 5: never leave a half-written .tmp behind on failure.
        tmp_path.unlink(missing_ok=True)
        raise
    return len(tweets)
