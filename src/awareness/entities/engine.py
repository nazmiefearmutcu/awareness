"""Corpus-level entity analysis over the DuckDB captures index.

:class:`EntityEngine` wraps a :class:`awareness.storage.duckdb_index.DuckDbIndex`
and runs bounded, parameterized queries to aggregate entities, find
co-occurrences, trace entity trends, and measure term correlation (with a
simple lead-lag scan).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from awareness.entities.extract import extract_entities, normalize_entity
from awareness.entities.models import (
    CoOccurrence,
    CorrelationResult,
    ExtractedEntity,
    TimeBucket,
)
from awareness.util.timeutil import floor_to_day, inclusive_end, utcnow

_MAX_DOCS_SCAN = 500
_MAX_COOCCUR_DOCS = 100
_MAX_TEXT_CHARS = 2000


def _word_pattern(term: str) -> str:
    """Word-boundary regex pattern for a term (case-insensitive)."""
    return rf"(?i)\b{re.escape(term)}\b"


class EntityEngine:
    """Entity extraction / co-occurrence / correlation over the corpus."""

    def __init__(self, index: Any) -> None:
        self._index = index

    # -- helpers ------------------------------------------------------------

    def _ready(self) -> bool:
        snap = self._index.health_snapshot()
        return bool(snap.get("ready"))

    def _docs_containing(
        self,
        term: str,
        *,
        limit: int,
        start: datetime | None = None,
        end: datetime | None = None,
        columns: str = "url, title, text, fetch_ts",
    ) -> list[dict[str, Any]]:
        where: list[str] = ["regexp_matches(title || chr(10) || text, $pat)"]
        params: dict[str, Any] = {"pat": _word_pattern(term)}
        if start is not None:
            where.append("fetch_ts >= $start")
            params["start"] = start.isoformat()
        if end is not None:
            where.append("fetch_ts <= $end")
            params["end"] = end.isoformat()
        sql = (
            f"SELECT {columns} FROM captures "  # noqa: S608 - columns is code-owned
            f"WHERE {' AND '.join(where)} "
            f"ORDER BY fetch_ts DESC LIMIT {int(limit)}"
        )
        try:
            return self._index.execute(sql, params)
        except Exception:
            return []

    def _term_count(
        self, term: str, *, start: datetime, end: datetime
    ) -> int:
        rows = self._index.execute(
            "SELECT COUNT(*) AS n FROM captures "
            "WHERE regexp_matches(title || chr(10) || text, $pat) "
            "AND fetch_ts >= $start AND fetch_ts <= $end",
            {
                "pat": _word_pattern(term),
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )
        return int(rows[0]["n"]) if rows else 0

    # -- public API ---------------------------------------------------------

    def extract_from_corpus(
        self,
        limit_docs: int = 500,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[ExtractedEntity]:
        """Aggregate entities over the most recent documents."""
        limit_docs = max(1, min(int(limit_docs), 2000))
        docs = self._docs_containing(
            "", limit=limit_docs, start=start, end=end, columns="title, text"
        )
        counts: dict[tuple[str, str], int] = {}
        for doc in docs:
            title = doc.get("title") or ""
            text = (doc.get("text") or "")[:_MAX_TEXT_CHARS]
            for entity, kind in extract_entities(f"{title}\n{text}"):
                key = (entity, kind)
                counts[key] = counts.get(key, 0) + 1
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0][0]))
        return [
            ExtractedEntity(text=name, kind=kind, count=count)
            for (name, kind), count in ranked[:100]
        ]

    def co_occurrence(
        self,
        entity: str,
        kind: str | None = None,
        window_days: int = 30,
        limit: int = 50,
    ) -> list[CoOccurrence]:
        """Entities appearing in the same docs as *entity*."""
        entity = normalize_entity(entity)
        limit = max(1, min(int(limit), 200))
        window_days = max(1, min(int(window_days), 365))
        end = utcnow()
        start = end - timedelta(days=window_days)
        docs = self._docs_containing(
            entity, limit=_MAX_COOCCUR_DOCS, start=start, end=end,
            columns="title, text",
        )
        counts: dict[tuple[str, str], int] = {}
        for doc in docs:
            title = doc.get("title") or ""
            text = (doc.get("text") or "")[:_MAX_TEXT_CHARS]
            for ent, ent_kind in extract_entities(f"{title}\n{text}"):
                if ent == entity or (kind and ent_kind != kind):
                    continue
                key = (ent, ent_kind)
                counts[key] = counts.get(key, 0) + 1
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0][0]))
        return [
            CoOccurrence(entity=name, kind=k, count=c)
            for (name, k), c in ranked[:limit]
        ]

    def entity_trend(
        self,
        entity: str,
        window_days: int = 14,
        granularity: str = "day",
    ) -> list[TimeBucket]:
        """Daily/weekly/monthly doc counts containing *entity*."""
        entity = normalize_entity(entity)
        window_days = max(1, min(int(window_days), 365))
        if granularity not in ("day", "week", "month"):
            granularity = "day"
        end = inclusive_end(utcnow())
        start = end - timedelta(days=window_days)
        rows = self._index.execute(
            "SELECT fetch_ts AS ts FROM captures "
            "WHERE regexp_matches(title || chr(10) || text, $pat) "
            "AND fetch_ts >= $start AND fetch_ts <= $end "
            "ORDER BY fetch_ts ASC",
            {
                "pat": _word_pattern(entity),
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )
        counts: dict[datetime, int] = {}
        for row in rows:
            ts = row.get("ts")
            if ts is None:
                continue
            bucket = floor_to_day(ts)
            if granularity == "week":
                bucket = bucket - timedelta(days=bucket.weekday())
            elif granularity == "month":
                bucket = bucket.replace(day=1)
            counts[bucket] = counts.get(bucket, 0) + 1
        if not counts:
            return []
        step: timedelta
        if granularity == "week":
            step = timedelta(days=7)
        elif granularity == "month":
            step = timedelta(days=31)
        else:
            step = timedelta(days=1)
        first = min(counts)
        last = max(counts)
        series: list[TimeBucket] = []
        cursor = first
        while cursor <= last:
            series.append(TimeBucket(ts=cursor, count=counts.get(cursor, 0)))
            if granularity == "month":
                # Calendar-month arithmetic: day+31 drifts (Jun 1 + 31d = Jul 2).
                if cursor.month == 12:
                    cursor = cursor.replace(year=cursor.year + 1, month=1)
                else:
                    cursor = cursor.replace(month=cursor.month + 1)
            else:
                cursor += step
        return series

    def correlation(
        self, term_a: str, term_b: str, window_days: int = 30
    ) -> CorrelationResult:
        """Pearson r between two terms' daily counts, plus best lead-lag."""
        window_days = max(1, min(int(window_days), 365))
        end = inclusive_end(utcnow())
        start = end - timedelta(days=window_days)
        days = (end.date() - start.date()).days + 1
        series_a: list[int] = []
        series_b: list[int] = []
        series_ts: list[datetime] = []
        cursor = floor_to_day(start)
        for _ in range(days):
            day_end = cursor + timedelta(days=1)
            series_a.append(self._term_count(term_a, start=cursor, end=day_end))
            series_b.append(self._term_count(term_b, start=cursor, end=day_end))
            series_ts.append(cursor)
            cursor += timedelta(days=1)

        arr_a = np.asarray(series_a, dtype=float)
        arr_b = np.asarray(series_b, dtype=float)
        n = len(arr_a)
        r = _pearson(arr_a, arr_b)

        best_lag = 0
        best_r = r
        for lag in range(-3, 4):
            if lag == 0:
                continue
            if lag > 0:
                a_shift = arr_a[lag:]
                b_shift = arr_b[: len(arr_b) - lag]
            else:
                a_shift = arr_a[: len(arr_a) + lag]
                b_shift = arr_b[-lag:]
            if len(a_shift) < 3:
                continue
            rr = _pearson(a_shift, b_shift)
            if abs(rr) > abs(best_r):
                best_r = rr
                best_lag = lag

        return CorrelationResult(
            a=term_a,
            b=term_b,
            r=float(r),
            n=n,
            best_lag=best_lag,
            best_r=float(best_r),
            series=[
                TimeBucket(ts=ts, count=count)
                for ts, count in zip(series_ts, series_a, strict=True)
            ],
        )


def _pearson(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) < 2:
        return 0.0
    sx = float(np.std(x))
    sy = float(np.std(y))
    if sx == 0.0 or sy == 0.0:
        return 0.0
    return float(np.corrcoef(x, y)[0, 1])
