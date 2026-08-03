"""Lexicon-based sentiment analysis over the DuckDB captures index.

:class:`SentimentEngine` wraps a :class:`~awareness.storage.duckdb_index.DuckDbIndex`
and answers sentiment questions about the captured corpus with bounded,
parameterized DuckDB SQL:

* ``score_text`` — deterministic lexicon scoring of a single document
  (negation flip within 3 tokens, intensifier weight).
* ``term_sentiment_over_time`` — per-bucket doc/sentiment aggregates for a
  term, zero-filled over calendar buckets.
* ``market_heat`` — window aggregate: doc counts, ratio, volatility and a
  trailing 7-day slope.

Design notes:

* All queries are parameterized (``$name`` binds) and row-capped, so a
  giant corpus cannot blow up latency or memory on a single call.
* Time buckets are computed in Python via
  :func:`awareness.util.timeutil.floor_to_day` (plus week/month flooring)
  with calendar-month arithmetic, so the engine never depends on DuckDB
  ``date_trunc`` part-name quirks or drifting month steps.
* An empty corpus returns empty buckets / zeroed heat, never an exception.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from awareness.obs.logging import get_logger
from awareness.sentiment.lexicon import INTENSIFIERS, NEGATIONS, NEGATIVE, POSITIVE
from awareness.sentiment.models import SentimentBucket
from awareness.storage.duckdb_index import DuckDbIndex
from awareness.util.timeutil import floor_to_day

logger = get_logger("sentiment.engine")

# Accepted granularities for time bucketing.
GRANULARITIES: tuple[str, ...] = ("day", "week", "month")

# Hard caps that keep every query bounded (overload guards).
_MAX_SCAN_DOCS = 20_000  # docs scanned for the sentiment series / heat

_MAX_TERM_LEN = 200
_MAX_WINDOW_DAYS = 365

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Tokens before a sentiment word that flip its polarity / add weight.
_NEGATION_WINDOW = 3
_INTENSITY_WEIGHT = 0.5


def _tokenize(text: str | None) -> list[str]:
    """Lowercase alphanumeric tokens of *text* (punctuation stripped)."""
    if not text:
        return []
    return _TOKEN_RE.findall(str(text).lower())


def _validate_term(term: str) -> str:
    """Strip + validate a term; raise :class:`ValueError` when unusable."""
    cleaned = (term or "").strip()
    if not cleaned:
        raise ValueError("term must not be empty")
    if len(cleaned) > _MAX_TERM_LEN:
        raise ValueError(f"term must be at most {_MAX_TERM_LEN} characters")
    return cleaned


def _validate_window_days(window_days: int) -> int:
    """Validate a window length (1..365); raise :class:`ValueError` otherwise."""
    days = int(window_days)
    if not 1 <= days <= _MAX_WINDOW_DAYS:
        raise ValueError(f"window_days must be between 1 and {_MAX_WINDOW_DAYS}")
    return days


def _word_pattern(term: str) -> str:
    """Word-boundary regex pattern for a term (case-insensitive).

    The pattern itself is bound as a query parameter (never interpolated
    into SQL) and ``re.escape`` protects regex metacharacters in the term.
    """
    return rf"(?i)\b{re.escape(term)}\b"


def _floor_bucket(dt: datetime, granularity: str) -> datetime:
    """Floor *dt* (UTC) to the start of its day/week/month bucket."""
    day = floor_to_day(dt)
    if granularity == "day":
        return day
    if granularity == "week":
        return day - timedelta(days=day.weekday())  # Monday
    if granularity == "month":
        return day.replace(day=1)
    raise ValueError(f"invalid granularity: {granularity!r}")


def _iter_buckets(start: datetime, end: datetime, granularity: str) -> Iterator[datetime]:
    """Yield every bucket start in ``[floor(start), floor(end)]`` inclusive."""
    current = _floor_bucket(start, granularity)
    stop = _floor_bucket(end, granularity)
    while current <= stop:
        yield current
        if granularity == "month":
            # Calendar-month arithmetic: timedelta(days=31) drifts
            # (Jun 1 + 31d = Jul 2) — the bug that was fixed in
            # entities/engine.py entity_trend. Use month rollover instead.
            year, month = current.year, current.month + 1
            if month > 12:
                year, month = year + 1, 1
            current = current.replace(year=year, month=month)
        else:
            current += timedelta(days=1 if granularity == "day" else 7)


class SentimentEngine:
    """Sentiment analytics over the DuckDbIndex ``captures`` view.

    All time windows use ``fetch_ts`` (UTC) and default to the corpus's own
    tail: ``end`` = latest ``fetch_ts`` in the index, ``start`` =
    ``end - window_days``, so results are deterministic for a given corpus.
    """

    def __init__(self, index: DuckDbIndex) -> None:
        self._index = index

    # ── scoring ──────────────────────────────────────────────────────────

    def score_text(self, text: str | None) -> dict[str, Any]:
        """Score a single document with the lexicon.

        Tokens are lowercased and punctuation-stripped. Each sentiment word
        contributes a weight of 1.0; a negation word within 3 tokens before
        it flips its polarity; each intensifier within 3 tokens before it
        adds ``+0.5`` weight. Returns ``{pos, neg, score, tokens_scanned,
        classified}`` where ``score = (pos - neg) / (pos + neg)`` in [-1, 1]
        (0.0 when no sentiment words are present).
        """
        tokens = _tokenize(text)
        pos = 0.0
        neg = 0.0
        for i, token in enumerate(tokens):
            if token in POSITIVE:
                polarity = 1.0
            elif token in NEGATIVE:
                polarity = -1.0
            else:
                continue
            window = tokens[max(0, i - _NEGATION_WINDOW):i]
            if any(word in NEGATIONS for word in window):
                polarity = -polarity
            magnitude = 1.0 + _INTENSITY_WEIGHT * sum(
                1 for word in window if word in INTENSIFIERS
            )
            if polarity > 0.0:
                pos += magnitude
            else:
                neg += magnitude
        total = pos + neg
        score = (pos - neg) / total if total > 0.0 else 0.0
        return {
            "pos": pos,
            "neg": neg,
            "score": round(float(score), 4),
            "tokens_scanned": len(tokens),
            "classified": total > 0.0,
        }

    # ── window helpers ───────────────────────────────────────────────────

    def _corpus_bounds(self) -> tuple[datetime | None, datetime | None]:
        """Earliest/latest ``fetch_ts`` in the corpus, or ``(None, None)``."""
        rows = self._index.execute(
            " ".join(
                [
                    "SELECT min(fetch_ts) AS lo, max(fetch_ts) AS hi",
                    "FROM captures",
                ]
            )
        )
        if not rows:
            return None, None
        return rows[0].get("lo"), rows[0].get("hi")

    def _matching_rows(
        self,
        term: str,
        start_dt: datetime,
        end_dt: datetime,
    ) -> list[dict[str, Any]]:
        """Rows whose title/text contains *term*, within the window (bounded)."""
        return self._index.execute(
            " ".join(
                [
                    "SELECT fetch_ts AS ts, title AS title, text AS text",
                    "FROM captures",
                    "WHERE",
                    "regexp_matches("
                    "COALESCE(title, '') || chr(10) || COALESCE(text, ''), $pat)",
                    "AND fetch_ts >= $start AND fetch_ts <= $end",
                    # Newest-first: when the cap truncates, we keep the
                    # recent buckets rather than silently dropping them.
                    "ORDER BY fetch_ts DESC",
                    "LIMIT $max_rows",
                ]
            ),
            {
                "pat": _word_pattern(term),
                "start": start_dt,
                "end": end_dt,
                "max_rows": _MAX_SCAN_DOCS,
            },
        )

    def _doc_scores(
        self, term: str, start_dt: datetime, end_dt: datetime
    ) -> list[tuple[datetime, float]]:
        """``(fetch_ts, score)`` pairs for docs containing *term* in the window."""
        pairs: list[tuple[datetime, float]] = []
        for row in self._matching_rows(term, start_dt, end_dt):
            ts = row.get("ts")
            if ts is None:
                continue
            title = row.get("title")
            text = row.get("text")
            score = self.score_text(f"{title or ''}\n{text or ''}")["score"]
            pairs.append((ts, float(score)))
        return pairs

    # ── public sentiment surface ─────────────────────────────────────────

    def term_sentiment_over_time(
        self,
        term: str,
        window_days: int = 14,
        granularity: str = "day",
    ) -> list[SentimentBucket]:
        """Sentiment aggregates per time bucket for docs containing *term*.

        Buckets span ``[floor(start), floor(end)]`` inclusive (calendar
        month arithmetic for ``month``) and are zero-filled, so the series
        is chart-ready. ``window_days`` bounds the scan backwards from the
        latest ``fetch_ts`` in the corpus; at most
        :data:`_MAX_SCAN_DOCS` docs are scanned.
        """
        cleaned = _validate_term(term)
        days = _validate_window_days(window_days)
        if granularity not in GRANULARITIES:
            raise ValueError(f"invalid granularity: {granularity!r}")
        lo, hi = self._corpus_bounds()
        if lo is None or hi is None:
            return []
        start_dt, end_dt = hi - timedelta(days=days), hi
        per_doc: list[tuple[datetime, float]] = self._doc_scores(cleaned, start_dt, end_dt)

        pos_counts: Counter[datetime] = Counter()
        neg_counts: Counter[datetime] = Counter()
        doc_counts: Counter[datetime] = Counter()
        score_sums: Counter[datetime] = Counter()
        for ts, score in per_doc:
            bucket = _floor_bucket(ts, granularity)
            doc_counts[bucket] += 1
            score_sums[bucket] += score
            if score > 0.0:
                pos_counts[bucket] += 1
            elif score < 0.0:
                neg_counts[bucket] += 1

        buckets = list(_iter_buckets(start_dt, end_dt, granularity))
        result: list[SentimentBucket] = []
        for bucket in buckets:
            count = doc_counts.get(bucket, 0)
            avg = (score_sums[bucket] / count) if count else 0.0
            result.append(
                SentimentBucket(
                    ts=bucket,
                    doc_count=count,
                    pos_count=pos_counts.get(bucket, 0),
                    neg_count=neg_counts.get(bucket, 0),
                    avg_score=round(float(avg), 4),
                )
            )
        return result

    def market_heat(self, term: str, window_days: int = 30) -> dict[str, float | int]:
        """Window aggregate for *term*: volume, ratio, volatility, trend.

        Returns ``{total_docs, pos_docs, neg_docs, sentiment_ratio,
        volatility, last_7d_trend}``. ``sentiment_ratio`` is
        ``(pos_docs - neg_docs) / (pos_docs + neg_docs)``; ``volatility`` is
        the std of per-day average scores over the window (zero-filled
        days); ``last_7d_trend`` is the numpy polyfit slope over the last 7
        daily average scores (0.0 with fewer than 2 days of window).
        """
        cleaned = _validate_term(term)
        days = _validate_window_days(window_days)
        lo, hi = self._corpus_bounds()
        if lo is None or hi is None:
            return {
                "total_docs": 0,
                "pos_docs": 0,
                "neg_docs": 0,
                "sentiment_ratio": 0.0,
                "volatility": 0.0,
                "last_7d_trend": 0.0,
            }
        start_dt, end_dt = hi - timedelta(days=days), hi
        per_doc: list[tuple[datetime, float]] = self._doc_scores(cleaned, start_dt, end_dt)

        total_docs = len(per_doc)
        pos_docs = sum(1 for _, score in per_doc if score > 0.0)
        neg_docs = sum(1 for _, score in per_doc if score < 0.0)
        ratio = (pos_docs - neg_docs) / (pos_docs + neg_docs) if pos_docs + neg_docs else 0.0

        day_sums: Counter[datetime] = Counter()
        day_counts: Counter[datetime] = Counter()
        for ts, score in per_doc:
            day = floor_to_day(ts)
            day_sums[day] += score
            day_counts[day] += 1
        num_days = (floor_to_day(end_dt).date() - floor_to_day(start_dt).date()).days + 1
        day0 = floor_to_day(start_dt)
        avgs = [
            (day_sums[day] / day_counts[day]) if day_counts[day] else 0.0
            for day in (day0 + timedelta(days=i) for i in range(num_days))
        ]
        volatility = float(np.std(np.asarray(avgs, dtype=np.float64))) if num_days >= 2 else 0.0
        tail = avgs[-7:]
        if len(tail) >= 2:
            slope = float(np.polyfit(np.arange(len(tail)), np.asarray(tail, dtype=np.float64), 1)[0])
        else:
            slope = 0.0
        return {
            "total_docs": total_docs,
            "pos_docs": pos_docs,
            "neg_docs": neg_docs,
            "sentiment_ratio": round(ratio, 4),
            "volatility": round(volatility, 4),
            "last_7d_trend": round(slope, 4),
        }
