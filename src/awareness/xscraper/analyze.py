"""Lexicon-based analysis of tweets captured for an X scraper session.

``analyze_session`` reads every tweet a session captured and returns a plain
dict with author counts, top terms, per-tweet sentiment (reusing the
:mod:`awareness.sentiment.lexicon` word sets), a per-day sentiment trend and
timeline, and engagement totals. An empty session yields a fully zeroed dict —
never an exception. ``export_tweets_csv`` writes a session's tweets out as
UTF-8 CSV, and ``export_timeline_csv`` writes a per-day sentiment timeline.

The per-tweet scoring (``_score_tweet``/``_classify``) and day bucketing
(``_bucket_day``/``session_timeline``) are shared by the analysis aggregation
and the timeline export, so the two surfaces can never disagree.
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


def _classify(pos: int, neg: int) -> str:
    """Bucket ``(pos, neg)`` hit counts into a sentiment class."""
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return "neutral"


def _bucket_day(day: dict[str, Any], text: str) -> tuple[str, float]:
    """Classify one tweet's text and fold it into a per-day bucket.

    *day* carries ``positive``/``negative``/``neutral`` counts and a
    ``scores`` list; the class count is bumped and the continuous score
    appended. Returns the tweet's ``(label, score)`` so callers can keep
    their own aggregate counters in lockstep.
    """
    pos, neg = _score_tweet(text)
    label = _classify(pos, neg)
    day[label] += 1
    score = (pos - neg) / (pos + neg + 1e-9)
    day["scores"].append(score)
    return label, score


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
        day = trend.setdefault(date, {"positive": 0, "negative": 0, "neutral": 0, "scores": []})
        label, score = _bucket_day(day, tweet.text)
        sentiment[label] += 1
        scores.append(score)
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
                "pos": day["positive"],
                "neg": day["negative"],
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


async def session_timeline(
    store: SessionStore,
    session_id: str,
    limit: int = 500,
) -> dict[str, dict[str, Any]]:
    """Return per-day sentiment buckets for a session's most recent tweets.

    Buckets are keyed by ``YYYY-MM-DD`` and carry ``positive``/``negative``/
    ``neutral`` counts plus a ``scores`` list, folded with the same per-tweet
    scoring as :func:`analyze_session` (the shared ``_bucket_day`` helper).
    Raises :class:`KeyError` for an unknown session; an empty session yields
    an empty dict.
    """
    session = await store.get_session(session_id)
    if session is None:
        raise KeyError(f"session {session_id!r} not found")
    tweets = await store.list_tweets(session_id, limit=limit)
    days: dict[str, dict[str, Any]] = {}
    for tweet in tweets:
        date = tweet.created_at.strftime("%Y-%m-%d")
        day = days.setdefault(date, {"positive": 0, "negative": 0, "neutral": 0, "scores": []})
        _bucket_day(day, tweet.text)
    return days


def _write_csv_atomic(destination: Path, header: list[str], rows: list[list[Any]]) -> None:
    """Write *header* + *rows* to *destination* via a sibling ``.tmp`` file.

    The ``.tmp`` file is atomically renamed over the destination on success
    and removed on failure, so an interrupted write never leaves a
    half-written file behind.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = destination.with_name(destination.name + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(header)
            writer.writerows(rows)
        os.replace(tmp_path, destination)
    except Exception:
        # Never leave a half-written .tmp behind on failure.
        tmp_path.unlink(missing_ok=True)
        raise


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
    header = ["tweet_id", "created_at", "username", "text", "likes", "retweets", "lang", "source"]
    rows = [
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
        for tweet in tweets
    ]
    _write_csv_atomic(Path(out_path), header, rows)
    return len(tweets)


async def export_timeline_csv(
    store: SessionStore,
    session_id: str,
    out_path: str | Path,
    limit: int = 500,
) -> int:
    """Write a session's per-day sentiment timeline to *out_path*; return day count.

    Rows carry (date, tweet_count, pos, neg, neutral, avg_score) derived from
    the SAME per-tweet scoring as :func:`analyze_session` (shared
    :func:`session_timeline` bucketing), so per-day counts sum to the
    analysis's aggregate sentiment. Written atomically via a sibling ``.tmp``
    path like :func:`export_tweets_csv`. Raises :class:`KeyError` for an
    unknown session; an empty session writes a header-only file and returns 0.
    """
    days = await session_timeline(store, session_id, limit=limit)
    header = ["date", "tweet_count", "pos", "neg", "neutral", "avg_score"]
    rows = [
        [
            date,
            len(day["scores"]),
            day["positive"],
            day["negative"],
            day["neutral"],
            round(sum(day["scores"]) / len(day["scores"]), 4),
        ]
        for date, day in sorted(days.items())
    ]
    _write_csv_atomic(Path(out_path), header, rows)
    return len(days)
