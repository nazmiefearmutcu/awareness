"""Core analytics engine: term frequency, spikes, breakdowns, co-occurrence.

:class:`TermFrequencyEngine` wraps a :class:`~awareness.storage.duckdb_index.DuckDbIndex`
and answers analytical questions about the captured corpus with bounded,
parameterized DuckDB SQL:

* ``term_frequency_over_time`` — doc counts per time bucket for a term
* ``detect_spikes`` — z-score anomaly detection on per-day term counts
* ``top_terms`` / ``entity_term_counts`` — corpus and co-occurrence vocab
* ``domain_breakdown`` / ``language_breakdown`` — corpus facets

Design notes:

* All queries are parameterized (``$name`` binds) and row-capped, so a giant
  corpus cannot blow up latency or memory on a single analytics call.
* Time buckets are computed in Python via
  :func:`awareness.util.timeutil.floor_to_day` (plus week/month flooring) so
  the engine never depends on DuckDB ``date_trunc`` part-name quirks.
* An empty corpus returns empty lists, never an exception.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from awareness.analytics.models import (
    DomainCount,
    LanguageCount,
    Spike,
    TermCount,
    TimeBucket,
)
from awareness.obs.logging import get_logger
from awareness.storage.duckdb_index import DuckDbIndex
from awareness.util.lang import PRIMARY_LANGUAGE_SQL
from awareness.util.timeutil import floor_to_day, inclusive_end, to_utc

logger = get_logger("analytics.engine")

# Accepted granularities for time bucketing.
GRANULARITIES: tuple[str, ...] = ("day", "week", "month")

# Accepted term-matching modes (title / text / both).
TERM_MODES: tuple[str, ...] = ("title", "text", "title_text")

# Hard caps that keep every query bounded (overload guards). The captures
# view itself dedups by capture_id, so row caps are doc caps.
_MAX_MATCHING_ROWS = 100_000  # materialized fetch_ts rows for time series
_MAX_TOKENIZE_DOCS = 50_000  # docs scanned for top-terms / co-occurrence

_MAX_TERM_LEN = 200
_MAX_WINDOW_DAYS = 365
_MAX_LIMIT = 500
_MAX_MIN_COUNT = 1000

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Small English stopword set (common words add no signal to term rankings).
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "about", "above", "after", "again", "against", "all", "also", "am",
        "an", "and", "any", "are", "as", "at", "be", "because", "been", "before",
        "being", "below", "between", "both", "but", "by", "can", "could", "did",
        "do", "does", "doing", "down", "during", "each", "few", "for", "from",
        "further", "had", "has", "have", "having", "he", "her", "here", "hers",
        "herself", "him", "himself", "his", "how", "i", "if", "in", "into", "is",
        "it", "its", "itself", "just", "may", "me", "might", "more", "most",
        "must", "my", "myself", "no", "nor", "not", "now", "of", "off", "on",
        "once", "only", "or", "other", "our", "ours", "ourselves", "out", "over",
        "own", "same", "say", "she", "should", "so", "some", "such", "than",
        "that", "the", "their", "theirs", "them", "themselves", "then", "there",
        "these", "they", "this", "those", "through", "to", "too", "under",
        "until", "up", "upon", "us", "very", "was", "we", "were", "what", "when",
        "where", "which", "while", "who", "whom", "why", "will", "with", "would",
        "you", "your", "yours", "yourself", "yourselves",
    }
)


def tokenize(text: str | None) -> list[str]:
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


def _clamp(value: int, lo: int, hi: int) -> int:
    return min(max(int(value), lo), hi)


def _term_pattern(term: str) -> str:
    """Word-boundary, case-insensitive regex pattern for a term.

    The pattern itself is bound as a query parameter (never interpolated into
    SQL) and ``re.escape`` protects regex metacharacters in the term.
    """
    return "(?i)\\b" + re.escape(term) + "\\b"


def _match_expr(mode: str) -> str:
    """SQL match expression for *mode* over ``$pat`` (whitelisted constants)."""
    if mode == "title":
        return "COALESCE(regexp_matches(title, $pat), false)"
    if mode == "text":
        return "COALESCE(regexp_matches(text, $pat), false)"
    if mode == "title_text":
        return (
            "COALESCE(regexp_matches(title, $pat), false)"
            " OR COALESCE(regexp_matches(text, $pat), false)"
        )
    raise ValueError(f"invalid mode: {mode!r}")


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
            year, month = current.year, current.month + 1
            if month > 12:
                year, month = year + 1, 1
            current = current.replace(year=year, month=month)
        else:
            current += timedelta(days=1 if granularity == "day" else 7)


def _coerce_bound(value: Any, name: str) -> datetime | None:
    """Coerce a window bound to UTC datetime; raise :class:`ValueError` when bad."""
    if value is None:
        return None
    dt = to_utc(value)
    if dt is None:
        raise ValueError(f"invalid {name} timestamp: {value!r}")
    return dt


class TermFrequencyEngine:
    """Analytics over the DuckDbIndex ``captures`` view.

    All time windows use ``fetch_ts`` (UTC). When ``start``/``end`` are
    omitted, the window defaults to the corpus's own tail: ``end`` = latest
    ``fetch_ts`` in the index, ``start`` = ``end - window_days``. A
    user-supplied ``end`` at exactly midnight is expanded to end-of-day
    (``inclusive_end``), matching the repo's date-range convention.
    """

    def __init__(self, index: DuckDbIndex) -> None:
        self._index = index

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

    def _resolve_window(
        self,
        start: Any,
        end: Any,
        *,
        window_days: int | None,
        default_start: datetime,
        default_end: datetime,
    ) -> tuple[datetime, datetime]:
        """Resolve a ``(start, end)`` UTC window; raise :class:`ValueError` when bad."""
        end_dt = inclusive_end(_coerce_bound(end, "end"))
        if end_dt is None:
            end_dt = default_end
        if window_days is not None:
            start_dt = _coerce_bound(start, "start")
            if start_dt is None:
                start_dt = end_dt - timedelta(days=window_days)
        else:
            start_dt = _coerce_bound(start, "start")
            if start_dt is None:
                start_dt = default_start
        if start_dt > end_dt:
            raise ValueError("start must not be after end")
        return start_dt, end_dt

    def _matching_rows(
        self,
        term: str,
        mode: str,
        start_dt: datetime,
        end_dt: datetime,
        *,
        select: tuple[str, ...] = ("fetch_ts",),
    ) -> list[dict[str, Any]]:
        """Rows whose title/text contains *term*, within the window (bounded)."""
        return self._index.execute(
            " ".join(
                [
                    "SELECT " + ", ".join(select),
                    "FROM captures",
                    "WHERE",
                    _match_expr(mode),
                    "AND fetch_ts >= $start AND fetch_ts <= $end",
                    # Newest-first: when the cap truncates, we keep the
                    # recent buckets (where spike detection matters) rather
                    # than silently dropping them.
                    "ORDER BY fetch_ts DESC",
                    "LIMIT $max_rows",
                ]
            ),
            {
                "pat": _term_pattern(term),
                "start": start_dt,
                "end": end_dt,
                "max_rows": _MAX_MATCHING_ROWS,
            },
        )

    # ── public analytics surface ─────────────────────────────────────────

    def term_frequency_over_time(
        self,
        term: str,
        *,
        window_days: int = 7,
        granularity: str = "day",
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        mode: str = "title_text",
    ) -> list[TimeBucket]:
        """Count docs containing *term* per time bucket.

        Buckets span ``[floor(start), floor(end)]`` inclusive and are
        zero-filled, so the series is chart-ready. Granularity is one of
        ``day``/``week``/``month``; *mode* is ``title``/``text``/``title_text``.
        """
        cleaned = _validate_term(term)
        days = _validate_window_days(window_days)
        if granularity not in GRANULARITIES:
            raise ValueError(f"invalid granularity: {granularity!r}")
        if mode not in TERM_MODES:
            raise ValueError(f"invalid mode: {mode!r}")
        lo, hi = self._corpus_bounds()
        if lo is None or hi is None:
            return []
        start_dt, end_dt = self._resolve_window(
            start, end, window_days=days, default_start=lo, default_end=hi
        )
        rows = self._matching_rows(cleaned, mode, start_dt, end_dt)
        counts: Counter[datetime] = Counter()
        for row in rows:
            ts = row.get("fetch_ts")
            if ts is not None:
                counts[_floor_bucket(ts, granularity)] += 1
        buckets = list(_iter_buckets(start_dt, end_dt, granularity))
        return [TimeBucket(ts=bucket, count=counts.get(bucket, 0)) for bucket in buckets]

    def top_terms(
        self,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        limit: int = 50,
        min_count: int = 2,
    ) -> list[TermCount]:
        """Most frequent tokens across the corpus (stopwords excluded).

        Tokenization lowercases and strips punctuation; results are ranked by
        descending count, ties by ascending term. The scan is capped at
        ``_MAX_TOKENIZE_DOCS`` most recent docs in the window.
        """
        limit = _clamp(limit, 1, _MAX_LIMIT)
        min_count = _clamp(min_count, 1, _MAX_MIN_COUNT)
        lo, hi = self._corpus_bounds()
        if lo is None or hi is None:
            return []
        start_dt, end_dt = self._resolve_window(
            start, end, window_days=None, default_start=lo, default_end=hi
        )
        rows = self._index.execute(
            " ".join(
                [
                    "SELECT text",
                    "FROM captures",
                    "WHERE fetch_ts >= $start AND fetch_ts <= $end",
                    "ORDER BY fetch_ts DESC",
                    "LIMIT $max_rows",
                ]
            ),
            {"start": start_dt, "end": end_dt, "max_rows": _MAX_TOKENIZE_DOCS},
        )
        counter: Counter[str] = Counter()
        for row in rows:
            for token in tokenize(row.get("text")):
                if len(token) >= 2 and token not in _STOPWORDS:
                    counter[token] += 1
        ranked = sorted(
            ((t, c) for t, c in counter.items() if c >= min_count),
            key=lambda item: (-item[1], item[0]),
        )
        return [TermCount(term=t, count=c) for t, c in ranked[:limit]]

    def detect_spikes(
        self,
        term: str,
        window_days: int = 14,
        zscore_threshold: float = 2.5,
        min_absolute: int = 3,
    ) -> list[Spike]:
        """Flag days where *term* volume bursts above the window baseline.

        Per-day counts are z-scored against the window series mean/std
        (numpy, ``ddof=1``); a day is a spike when its z-score is at least
        *zscore_threshold* AND its raw count is at least *min_absolute*.
        """
        cleaned = _validate_term(term)
        days = _validate_window_days(window_days)
        threshold = float(zscore_threshold)
        if not np.isfinite(threshold) or threshold <= 0.0:
            raise ValueError("zscore_threshold must be a positive finite number")
        min_abs = max(1, int(min_absolute))
        lo, hi = self._corpus_bounds()
        if lo is None or hi is None:
            return []
        start_dt, end_dt = self._resolve_window(
            None, None, window_days=days, default_start=lo, default_end=hi
        )
        rows = self._matching_rows(cleaned, "title_text", start_dt, end_dt)
        counts: Counter[datetime] = Counter()
        for row in rows:
            ts = row.get("fetch_ts")
            if ts is not None:
                counts[floor_to_day(ts)] += 1
        buckets = list(_iter_buckets(start_dt, end_dt, "day"))
        series = np.array([counts.get(bucket, 0) for bucket in buckets], dtype=np.float64)
        if series.size < 3:
            return []
        mean = float(series.mean())
        std = float(series.std(ddof=1)) if series.size > 1 else 0.0
        if not np.isfinite(mean) or not np.isfinite(std) or std == 0.0:
            return []
        zscores = (series - mean) / std
        spikes: list[Spike] = []
        for bucket, count, zscore in zip(buckets, series, zscores, strict=True):
            count_int = int(count)
            if count_int >= min_abs and float(zscore) >= threshold:
                spikes.append(
                    Spike(
                        bucket=bucket,
                        count=count_int,
                        zscore=float(zscore),
                        mean=mean,
                        std=std,
                        vs_mean=count_int - mean,
                    )
                )
        return spikes

    def domain_breakdown(
        self,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        limit: int = 20,
    ) -> list[DomainCount]:
        """Capture counts grouped by domain (top *limit*, most recent first)."""
        limit = _clamp(limit, 1, _MAX_LIMIT)
        lo, hi = self._corpus_bounds()
        if lo is None or hi is None:
            return []
        start_dt, end_dt = self._resolve_window(
            start, end, window_days=None, default_start=lo, default_end=hi
        )
        rows = self._index.execute(
            " ".join(
                [
                    "SELECT domain AS domain, count(*) AS n",
                    "FROM captures",
                    "WHERE fetch_ts >= $start AND fetch_ts <= $end",
                    "AND domain IS NOT NULL AND TRIM(CAST(domain AS VARCHAR)) != ''",
                    "GROUP BY domain",
                    "ORDER BY n DESC, domain ASC",
                    "LIMIT $limit",
                ]
            ),
            {"start": start_dt, "end": end_dt, "limit": limit},
        )
        return [DomainCount(domain=str(r["domain"]), count=int(r["n"])) for r in rows]

    def language_breakdown(
        self,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        limit: int = 20,
    ) -> list[LanguageCount]:
        """Capture counts grouped by BCP-47 primary language tag.

        Regional subtags roll up (``en-US`` → ``en``); captures with no
        detected language land in the ``None`` bucket.
        """
        limit = _clamp(limit, 1, _MAX_LIMIT)
        lo, hi = self._corpus_bounds()
        if lo is None or hi is None:
            return []
        start_dt, end_dt = self._resolve_window(
            start, end, window_days=None, default_start=lo, default_end=hi
        )
        rows = self._index.execute(
            " ".join(
                [
                    "SELECT",
                    PRIMARY_LANGUAGE_SQL,
                    "AS language, count(*) AS n",
                    "FROM captures",
                    "WHERE fetch_ts >= $start AND fetch_ts <= $end",
                    "GROUP BY 1",
                    "ORDER BY n DESC, language ASC NULLS LAST",
                    "LIMIT $limit",
                ]
            ),
            {"start": start_dt, "end": end_dt, "limit": limit},
        )
        result: list[LanguageCount] = []
        for r in rows:
            lang = r.get("language")
            if lang is not None and str(lang).strip():
                lang = str(lang).strip()
            else:
                lang = None
            result.append(LanguageCount(language=lang, count=int(r["n"])))
        return result

    def entity_term_counts(
        self,
        term: str,
        *,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        limit: int = 50,
        min_count: int = 2,
    ) -> list[TermCount]:
        """Top tokens co-occurring in docs that contain *term*.

        The term itself and stopwords are excluded; tokens below *min_count*
        are dropped. The scan is capped at ``_MAX_TOKENIZE_DOCS`` matching
        docs.
        """
        cleaned = _validate_term(term)
        limit = _clamp(limit, 1, _MAX_LIMIT)
        min_count = _clamp(min_count, 1, _MAX_MIN_COUNT)
        lo, hi = self._corpus_bounds()
        if lo is None or hi is None:
            return []
        start_dt, end_dt = self._resolve_window(
            start, end, window_days=None, default_start=lo, default_end=hi
        )
        rows = self._matching_rows(
            cleaned,
            "title_text",
            start_dt,
            end_dt,
            select=("text",),
        )
        if not rows:
            return []
        counter: Counter[str] = Counter()
        term_lower = cleaned.lower()
        for row in rows:
            for token in tokenize(row.get("text")):
                if token != term_lower and len(token) >= 2 and token not in _STOPWORDS:
                    counter[token] += 1
        ranked = sorted(
            ((t, c) for t, c in counter.items() if c >= min_count),
            key=lambda item: (-item[1], item[0]),
        )
        return [TermCount(term=t, count=c) for t, c in ranked[:limit]]
