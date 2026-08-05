"""Topic-lifecycle + source-impact engine over the captures lake.

:class:`TopicEngine` wraps a :class:`~awareness.storage.duckdb_index.DuckDbIndex`
and answers topic-intelligence questions about the captured corpus with
bounded, parameterized DuckDB SQL:

* ``lifecycle`` — per-day counts for a term plus a phase classification
  (EMERGING / EXPANDING / PEAKING / DECLINING / DORMANT / STABLE), the
  trailing-7-day polyfit slope, peak stats and first/last-seen days.
* ``compare_lifecycles`` — the same lifecycle for up to 10 terms.
* ``top_emerging`` — corpus-wide terms first seen in the trailing days,
  ranked by volume, with domain coverage.
* ``source_impact`` — per-origin-domain impact derived from the sourceintel
  replication map: how many copies each origin's output spawns, normalized
  against its own capture footprint, plus average replica lead time.
* ``topic_dominance`` — per-domain share of the docs mentioning a term,
  with average sentiment.

Design notes:

* Word-boundary, case-insensitive matching reuses the analytics engine's
  ``(?i)\\b<term>\\b`` helpers (``re.escape``d, always bound as a query
  parameter).
* Time windows use ``fetch_ts`` (UTC) and default to the corpus tail:
  ``start = max(fetch_ts) - window_days``, so results are deterministic for
  a given corpus. Replication edges use ``observed_ts`` (the sourceintel
  convention).
* Phase precedence (first match wins): PEAKING → EXPANDING → EMERGING →
  DECLINING → DORMANT → STABLE. EMERGING requires the term to be new
  (first seen within 3 days) AND reach the volume floor (>= 3 docs);
  EXPANDING additionally requires activity on >= 2 distinct days so a
  single-day burst classifies as EMERGING, not EXPANDING; DECLINING fires
  on a negative trailing-7-day slope or when a previously material term
  (peak >= 3) has gone completely quiet in the last 7 days.
* An empty corpus returns zeroed results, never an exception.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from math import log1p
from typing import Any

import numpy as np

from awareness.analytics.engine import (
    _STOPWORDS,
    _iter_buckets,
    _match_expr,
    _term_pattern,
    tokenize,
)
from awareness.analytics.models import TimeBucket
from awareness.obs.logging import get_logger
from awareness.sentiment.engine import SentimentEngine
from awareness.sourceintel.engine import SourceIntelEngine
from awareness.storage.duckdb_index import DuckDbIndex
from awareness.topicx.models import (
    EmergingTopic,
    SourceImpact,
    TopicDominance,
    TopicLifecycle,
)
from awareness.util.timeutil import floor_to_day, utcnow

logger = get_logger("topicx.engine")

# Hard caps that keep every query bounded (overload guards).
_MAX_TERM_LEN = 200
_MAX_WINDOW_DAYS = 365
_MAX_LIMIT = 500
_MAX_SCAN_DOCS = 50_000  # matching rows materialized for time series / scans
_MAX_COMPARE_TERMS = 10  # compare_lifecycles bound
_MAX_DOMINANCE_DOCS = 20_000  # docs scored for topic_dominance

# Lifecycle thresholds (doc counts) used by the phase classifier.
_EMERGING_FLOOR = 3  # minimum total docs for a term to "matter"
_EMERGING_WINDOW_DAYS = 3  # first mention must fall within this many days
_DECAY_PEAK_FLOOR = 3  # a term that peaked at >= 3 docs can be "declining"

_EMERGING = "EMERGING"
_EXPANDING = "EXPANDING"
_PEAKING = "PEAKING"
_DECLINING = "DECLINING"
_DORMANT = "DORMANT"
_STABLE = "STABLE"


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


def _clamp_limit(limit: int, default: int) -> int:
    """Clamp a result limit into 1.._MAX_LIMIT (default when falsy)."""
    return max(1, min(int(limit or default), _MAX_LIMIT))


def _polyfit_slope(values: list[float]) -> float:
    """Polyfit slope over *values*; 0.0 for < 2 points or zero variance."""
    series = np.asarray(values, dtype=np.float64)
    if series.size < 2 or float(np.ptp(series)) == 0.0:
        return 0.0
    return float(np.polyfit(np.arange(series.size), series, 1)[0])


class TopicEngine:
    """Topic-lifecycle / source-impact analytics over ``captures``.

    All queries are parameterized (``$name`` binds) and row-capped, so a
    giant corpus cannot blow up latency or memory on a single call.
    """

    def __init__(self, index: DuckDbIndex) -> None:
        self._index = index
        self._sentiment = SentimentEngine(index)
        self._sourceintel = SourceIntelEngine(index)

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

    def _matching_days(
        self, term: str, start_dt: datetime, end_dt: datetime
    ) -> Counter[datetime]:
        """Per-day (fetch_ts bucket) counts of docs containing *term*."""
        rows = self._index.execute(
            " ".join(
                [
                    "SELECT fetch_ts",
                    "FROM captures",
                    "WHERE",
                    _match_expr("title_text"),
                    "AND fetch_ts >= $start AND fetch_ts <= $end",
                    # Newest-first: when the cap truncates, we keep the
                    # recent buckets (where the phases are decided) rather
                    # than silently dropping them.
                    "ORDER BY fetch_ts DESC",
                    "LIMIT $max_rows",
                ]
            ),
            {
                "pat": _term_pattern(term),
                "start": start_dt,
                "end": end_dt,
                "max_rows": _MAX_SCAN_DOCS,
            },
        )
        counts: Counter[datetime] = Counter()
        for row in rows:
            ts = row.get("fetch_ts")
            if ts is not None:
                counts[floor_to_day(ts)] += 1
        return counts

    # ── lifecycle ────────────────────────────────────────────────────────

    def lifecycle(self, term: str, window_days: int = 30) -> TopicLifecycle:
        """Lifecycle of *term* over the trailing *window_days*.

        Returns the zero-filled daily series, the phase (see module
        docstring for precedence), the trailing-7-day polyfit slope, the
        peak day and the first/last-seen days. A term with no captures in
        the corpus (or an empty corpus) yields a zeroed DORMANT lifecycle.
        """
        cleaned = _validate_term(term)
        days = _validate_window_days(window_days)
        lo, hi = self._corpus_bounds()
        if lo is None or hi is None:
            return TopicLifecycle(
                term=cleaned,
                phase=_DORMANT,
                counts=[],
                slope_7d=0.0,
                peak_count=0,
                peak_date=None,
                first_seen=None,
                last_seen=None,
            )
        start_dt, end_dt = hi - timedelta(days=days), hi
        per_day = self._matching_days(cleaned, start_dt, end_dt)
        buckets = list(_iter_buckets(start_dt, end_dt, "day"))
        series = np.array([per_day.get(bucket, 0) for bucket in buckets], dtype=np.float64)

        active = [b for b, c in per_day.items() if c > 0]
        first_seen = min(active) if active else None
        last_seen = max(active) if active else None
        total = int(series.sum())
        today = buckets[-1]
        today_count = int(series[-1])
        yesterday_count = int(series[-2]) if len(series) >= 2 else 0
        last_7d_count = int(series[-7:].sum())
        peak_index = int(np.argmax(series)) if total else None
        peak_count = int(series[peak_index]) if peak_index is not None else 0
        slope_7d = _polyfit_slope(list(series[-7:])) if len(series) >= 7 else 0.0

        phase = self._classify(
            total=total,
            first_seen=first_seen,
            today=today,
            today_count=today_count,
            yesterday_count=yesterday_count,
            last_7d_count=last_7d_count,
            peak_count=peak_count,
            slope_7d=slope_7d,
            mean_7d=float(series[-7:].mean()) if len(series) >= 7 else 0.0,
            mean_14d=float(series[-14:].mean()) if len(series) >= 14 else float(series.mean()),
            distinct_active_days=len(active),
        )
        return TopicLifecycle(
            term=cleaned,
            phase=phase,
            counts=[TimeBucket(ts=bucket, count=int(c)) for bucket, c in zip(buckets, series, strict=True)],
            slope_7d=round(slope_7d, 4),
            peak_count=peak_count,
            peak_date=buckets[peak_index] if peak_index is not None else None,
            first_seen=first_seen,
            last_seen=last_seen,
        )

    @staticmethod
    def _classify(
        *,
        total: int,
        first_seen: datetime | None,
        today: datetime,
        today_count: int,
        yesterday_count: int,
        last_7d_count: int,
        peak_count: int,
        slope_7d: float,
        mean_7d: float,
        mean_14d: float,
        distinct_active_days: int,
    ) -> str:
        """Phase classification; first match wins (see module docstring)."""
        if total == 0:
            return _DORMANT
        # PEAKING: today still at/below yesterday's burst level but well
        # above the trailing-14d baseline.
        if today_count >= 1.5 * mean_14d and today_count < yesterday_count:
            return _PEAKING
        # EXPANDING: a sustained rise across >= 2 active days.
        if (
            slope_7d > 0.0
            and today_count > mean_7d
            and distinct_active_days >= 2
        ):
            return _EXPANDING
        # EMERGING: brand-new (first mention within 3 days) and material.
        if (
            first_seen is not None
            and first_seen >= today - timedelta(days=_EMERGING_WINDOW_DAYS - 1)
            and total >= _EMERGING_FLOOR
        ):
            return _EMERGING
        # DECLINING: trailing slope negative, or a material term (peak >= 3)
        # that has gone completely quiet in the last 7 days.
        if slope_7d < 0.0 or (
            last_7d_count == 0 and peak_count >= _DECAY_PEAK_FLOOR
        ):
            return _DECLINING
        # DORMANT when the term barely appears in the last 7 days; a steady
        # presence (>= 3 docs/week) with no trend is STABLE.
        return _DORMANT if last_7d_count < _EMERGING_FLOOR else _STABLE

    def compare_lifecycles(
        self, terms: list[str], window_days: int = 30
    ) -> list[TopicLifecycle]:
        """Lifecycles for up to :data:`_MAX_COMPARE_TERMS` terms (in order).

        Raises :class:`ValueError` when *terms* is empty or exceeds the cap;
        each term is validated the same way as :meth:`lifecycle`.
        """
        if not terms:
            raise ValueError("terms must not be empty")
        if len(terms) > _MAX_COMPARE_TERMS:
            raise ValueError(f"at most {_MAX_COMPARE_TERMS} terms are supported")
        cleaned = [_validate_term(term) for term in terms]
        return [self.lifecycle(term, window_days=window_days) for term in cleaned]

    # ── emerging topics ──────────────────────────────────────────────────

    def top_emerging(
        self, window_days: int = 7, limit: int = 20
    ) -> list[EmergingTopic]:
        """Corpus-wide terms first seen within 3 days, ranked by volume.

        Tokenizes the most recent *window_days* of docs (bounded scan,
        stopwords excluded), keeps terms whose first mention falls within
        the last 3 calendar days with at least 3 docs, and ranks by count
        desc (ties by term asc). ``domains_covered`` counts the distinct
        domains that published the term.
        """
        days = _validate_window_days(window_days)
        limit = _clamp_limit(limit, default=20)
        lo, hi = self._corpus_bounds()
        if lo is None or hi is None:
            return []
        start_dt, end_dt = hi - timedelta(days=days), hi
        rows = self._index.execute(
            " ".join(
                [
                    "SELECT fetch_ts, domain, text",
                    "FROM captures",
                    "WHERE fetch_ts >= $start AND fetch_ts <= $end",
                    "ORDER BY fetch_ts DESC",
                    "LIMIT $max_rows",
                ]
            ),
            {"start": start_dt, "end": end_dt, "max_rows": _MAX_SCAN_DOCS},
        )
        counts: Counter[str] = Counter()
        first_seen: dict[str, datetime] = {}
        domains: dict[str, set[str]] = {}
        for row in rows:
            ts = row.get("fetch_ts")
            text = row.get("text")
            if ts is None or not text:
                continue
            day = floor_to_day(ts)
            domain = str(row.get("domain") or "")
            for token in tokenize(text):
                if len(token) < 2 or token in _STOPWORDS:
                    continue
                counts[token] += 1
                if token not in first_seen or day < first_seen[token]:
                    first_seen[token] = day
                if domain:
                    domains.setdefault(token, set()).add(domain)

        cutoff = floor_to_day(end_dt) - timedelta(days=_EMERGING_WINDOW_DAYS - 1)
        emerging = [
            (token, counts[token], first_seen[token])
            for token in counts
            if counts[token] >= _EMERGING_FLOOR and first_seen[token] >= cutoff
        ]
        emerging.sort(key=lambda item: (-item[1], item[0]))
        return [
            EmergingTopic(
                term=token,
                count=count,
                first_seen=first_day,
                domains_covered=len(domains.get(token, set())),
            )
            for token, count, first_day in emerging[:limit]
        ]

    # ── source impact ────────────────────────────────────────────────────

    def source_impact(
        self, window_days: int = 30, limit: int = 20
    ) -> list[SourceImpact]:
        """Per-origin-domain impact over the replication map.

        Reuses :meth:`SourceIntelEngine.replication_map` for the directed
        (origin → replica) edges. For each origin domain::

            impact = replica_copies * log1p(degree) + captures_norm

        where ``replica_copies`` is the total replicated capture count over
        the origin's edges, ``degree`` is the number of distinct replica
        relationships, and ``captures_norm`` is the domain's own capture
        count log-normalized against the corpus pool. ``avg_lead_minutes``
        is the mean head start (replica first-observed minus origin
        first-observed) over the origin's replica edges.
        """
        days = _validate_window_days(window_days)
        limit = _clamp_limit(limit, default=20)
        edges = self._sourceintel.replication_map(window_days=days, limit=_MAX_LIMIT)
        if not edges:
            return []

        cutoff = utcnow() - timedelta(days=days)
        capture_rows = self._index.execute(
            " ".join(
                [
                    "SELECT domain AS domain, count(*) AS n",
                    "FROM captures",
                    "WHERE observed_ts >= $cutoff",
                    "AND domain IS NOT NULL AND TRIM(CAST(domain AS VARCHAR)) != ''",
                    "GROUP BY domain",
                ]
            ),
            {"cutoff": cutoff},
        )
        captures = {str(r["domain"]): int(r["n"]) for r in capture_rows}
        max_captures = max(captures.values(), default=0)

        by_origin: dict[str, dict[str, Any]] = {}
        for edge in edges:
            row = by_origin.setdefault(
                edge.origin, {"copies": 0, "degree": 0}
            )
            row["copies"] += edge.count
            row["degree"] += 1

        lead_rows = self._index.execute(
            self._lead_query(days, limit=_MAX_LIMIT * 10),
            {"cutoff": cutoff},
        )
        avg_lead = {str(r["origin_domain"]): float(r["avg_lead_minutes"] or 0.0) for r in lead_rows}

        result: list[SourceImpact] = []
        for domain, row in by_origin.items():
            copies = row["copies"]
            degree = row["degree"]
            n_captures = captures.get(domain, 0)
            captures_norm = (
                log1p(n_captures) / log1p(max_captures) if max_captures > 0 else 0.0
            )
            result.append(
                SourceImpact(
                    domain=domain,
                    impact_score=round(copies * log1p(degree) + captures_norm, 4),
                    captures=n_captures,
                    replica_edges=degree,
                    avg_lead_minutes=round(avg_lead.get(domain, 0.0), 2),
                )
            )
        result.sort(key=lambda r: (-r.impact_score, -r.captures, r.domain))
        return result[:limit]

    def _lead_query(self, days: int, *, limit: int) -> str:
        """SQL for per-origin average replica lead time (minutes).

        Mirrors the sourceintel multi-domain-group scan: within each
        dup-group the domain with the earliest ``observed_ts`` is the
        origin; the lead for each (origin, replica) pair is the replica's
        first-observed minus the origin's first-observed, averaged per
        origin domain.
        """
        return " ".join(
            [
                "WITH multi AS (",
                "SELECT parent_doc_or_dup_group AS grp",
                "FROM captures",
                "WHERE parent_doc_or_dup_group IS NOT NULL",
                "AND CAST(parent_doc_or_dup_group AS VARCHAR) <> ''",
                "AND domain IS NOT NULL AND TRIM(CAST(domain AS VARCHAR)) != ''",
                "AND observed_ts >= $cutoff",
                "GROUP BY parent_doc_or_dup_group",
                "HAVING COUNT(DISTINCT domain) >= 2",
                "ORDER BY COUNT(DISTINCT domain) DESC",
                f"LIMIT {int(limit)}",
                "),",
                "member_first AS (",
                "SELECT c.parent_doc_or_dup_group AS grp,",
                "c.domain AS domain,",
                "MIN(c.observed_ts) AS first_ts",
                "FROM captures c",
                "JOIN multi m ON m.grp = c.parent_doc_or_dup_group",
                "WHERE c.domain IS NOT NULL AND TRIM(CAST(c.domain AS VARCHAR)) != ''",
                "GROUP BY 1, 2",
                "),",
                "origins AS (",
                "SELECT grp,",
                "first(domain ORDER BY first_ts NULLS LAST, domain) AS domain,",
                "min(first_ts) AS first_ts",
                "FROM member_first",
                "GROUP BY grp",
                ")",
                "SELECT o.domain AS origin_domain,",
                "COUNT(*) AS replica_edges,",
                "AVG(epoch(mf.first_ts) - epoch(o.first_ts)) / 60.0 AS avg_lead_minutes",
                "FROM origins o",
                "JOIN member_first mf ON mf.grp = o.grp AND mf.domain <> o.domain",
                "GROUP BY o.domain",
            ]
        )

    # ── topic dominance ──────────────────────────────────────────────────

    def topic_dominance(
        self, term: str, window_days: int = 14, limit: int = 10
    ) -> list[TopicDominance]:
        """Per-domain share of the docs mentioning *term*.

        Docs are matched word-boundary, case-insensitively over the trailing
        *window_days*; ``doc_fraction`` is each domain's share of the
        matching docs (sums to 1.0 across the full result set).
        ``avg_sentiment`` is the mean lexicon score of the first 200
        characters of each matching doc (cheap: no full-text scoring).
        """
        cleaned = _validate_term(term)
        days = _validate_window_days(window_days)
        limit = _clamp_limit(limit, default=10)
        lo, hi = self._corpus_bounds()
        if lo is None or hi is None:
            return []
        start_dt, end_dt = hi - timedelta(days=days), hi
        rows = self._index.execute(
            " ".join(
                [
                    "SELECT domain, text",
                    "FROM captures",
                    "WHERE",
                    _match_expr("title_text"),
                    "AND fetch_ts >= $start AND fetch_ts <= $end",
                    "ORDER BY fetch_ts DESC",
                    "LIMIT $max_rows",
                ]
            ),
            {
                "pat": _term_pattern(cleaned),
                "start": start_dt,
                "end": end_dt,
                "max_rows": _MAX_DOMINANCE_DOCS,
            },
        )
        counts: Counter[str] = Counter()
        sentiment_sums: Counter[str] = Counter()
        for row in rows:
            domain = str(row.get("domain") or "").strip()
            if not domain:
                continue
            text = row.get("text")
            score = self._sentiment.score_text(str(text)[:200])["score"]
            counts[domain] += 1
            sentiment_sums[domain] += float(score)
        total = sum(counts.values())
        if total == 0:
            return []
        result: list[TopicDominance] = []
        for domain, count in counts.items():
            result.append(
                TopicDominance(
                    domain=domain,
                    doc_count=count,
                    doc_fraction=round(count / total, 4),
                    avg_sentiment=round(sentiment_sums[domain] / count, 4),
                )
            )
        result.sort(key=lambda r: (-r.doc_count, r.domain))
        return result[:limit]
