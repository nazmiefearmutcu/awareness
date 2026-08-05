"""Time-series engine: per-bucket corpus-quality aggregates over ``captures``.

:class:`QualityTimeEngine` adds a time dimension to the point-in-time
:class:`~awareness.corpusx.engine.CorpusXEngine.quality_snapshot`:

* ``history`` — per-bucket corpus-quality aggregates computed **from the
  corpus itself** (the ``captures`` view), so it works on old corpus data.
  Every scan is bounded: the window is clamped to 1..365 days and each
  doc-level scan reads at most :data:`_MAX_SCAN_ROWS` rows (newest first)
  inside the window.
* ``current`` — the point-in-time snapshot, delegated straight to
  :class:`~awareness.corpusx.engine.CorpusXEngine.quality_snapshot`.

All bucketing is calendar arithmetic in UTC and mirrors the analytics
engine's ``_iter_buckets`` (day/week/month; the month-drift fix), so bucket
boundaries always line up with the rest of the repo. Ratios are computed
**within each bucket's own docs** — a content hash / dup group shared across
buckets does not count. ``new_domains`` counts domains whose first-ever
capture (``min(fetch_ts)`` over the whole corpus) falls inside the bucket.
An empty corpus yields zeroed points, never an error.
"""

from __future__ import annotations

import calendar
from datetime import UTC, date, datetime, timedelta
from typing import Any

from awareness.analytics.engine import _iter_buckets
from awareness.corpusx.engine import CorpusXEngine, _zero_snapshot
from awareness.qualityx.models import QualityPoint
from awareness.storage.duckdb_index import DuckDbIndex
from awareness.util.timeutil import floor_to_day

_MIN_DAYS = 1
_MAX_DAYS = 365
_MAX_SCAN_ROWS = 200_000

_GRANULARITIES = ("day", "week", "month")

# Scan fragment: the doc-level scans are windowed to the newest rows, so a
# multi-million-row corpus cannot blow up memory or latency. Every bucket
# metric derives from this bounded, ordered projection of the window.
_SCAN_SOURCE = (
    "SELECT {cols} FROM captures WHERE fetch_ts >= $start "
    "ORDER BY fetch_ts DESC LIMIT $max_rows"
)


def _bucket_expr(granularity: str, col: str = "fetch_ts") -> str:
    """SQL bucket-key expression for *granularity* (whitelisted constants).

    Mirrors ``analytics.engine._floor_bucket`` so SQL keys line up with the
    Python bucket starts: day = calendar date, week = the Monday of the ISO
    week, month = the first of the month (all UTC — DuckDB's default session
    timezone).
    """
    if granularity == "day":
        return f"CAST({col} AS DATE)"
    if granularity == "week":
        return f"CAST(date_trunc('week', CAST({col} AS TIMESTAMP)) AS DATE)"
    if granularity == "month":
        return f"CAST(date_trunc('month', CAST({col} AS TIMESTAMP)) AS DATE)"
    raise ValueError(f"invalid granularity: {granularity!r} (choose from {_GRANULARITIES})")


def _bucket_days(day: date, granularity: str) -> int:
    """Calendar days spanned by the bucket starting at *day*."""
    if granularity == "day":
        return 1
    if granularity == "week":
        return 7
    if granularity == "month":
        return calendar.monthrange(day.year, day.month)[1]
    raise ValueError(f"invalid granularity: {granularity!r}")


class QualityTimeEngine:
    """Time-series corpus-quality engine over the DuckDbIndex ``captures`` view."""

    def __init__(self, index: DuckDbIndex | None) -> None:
        self._index = index

    # ── window helpers ───────────────────────────────────────────────────

    def _corpus_bounds(self) -> tuple[datetime | None, datetime | None]:
        """Earliest/latest ``fetch_ts`` in the corpus, or ``(None, None)``."""
        if self._index is None:
            return None, None
        rows = self._index.execute(
            "SELECT min(fetch_ts) AS lo, max(fetch_ts) AS hi FROM captures"
        )
        if not rows:
            return None, None
        return rows[0].get("lo"), rows[0].get("hi")

    # ── per-bucket history (query-computed from the corpus) ──────────────

    def history(self, days: int = 30, granularity: str = "day") -> list[QualityPoint]:
        """Per-bucket corpus-quality points over the trailing *days* (oldest first).

        Buckets are calendar buckets (UTC) ending at ``floor(max(fetch_ts))``,
        generated with the analytics engine's ``_iter_buckets`` calendar
        arithmetic; *days* is clamped to 1..365. Ratios are computed within
        each bucket's own docs (a content hash / dup group shared across
        buckets does not count); ``new_domains`` counts domains whose
        first-ever capture falls in the bucket; ``capture_rate`` is the
        bucket's doc count divided by its calendar-day span. Buckets without
        captures yield zeroed points. An empty corpus yields *days* zeroed
        points, never an error.
        """
        days = min(max(int(days), _MIN_DAYS), _MAX_DAYS)
        bucket_key = _bucket_expr(granularity)
        bucket_key_o = _bucket_expr(granularity, "o.fetch_ts")
        bucket_key_c = _bucket_expr(granularity, "c.fetch_ts")
        first_seen_key = _bucket_expr(granularity, "first_seen")

        _, hi = self._corpus_bounds()
        end = floor_to_day(hi) if hi is not None else floor_to_day(datetime.now(UTC))
        start = end - timedelta(days=days - 1)
        buckets = [
            bucket.date() for bucket in _iter_buckets(start, end, granularity)
        ]

        day_totals: dict[Any, int] = {}
        day_avg_length: dict[Any, float] = {}
        day_dups: dict[Any, int] = {}
        day_near_dups: dict[Any, int] = {}
        day_new_domains: dict[Any, int] = {}

        if self._index is not None:
            agg_rows = self._index.execute(
                " ".join(
                    [
                        f"SELECT {bucket_key} AS day, count(*) AS total,",
                        "avg(length(COALESCE(text, ''))) AS avg_length",
                        "FROM (",
                        _SCAN_SOURCE.format(cols="fetch_ts, text"),
                        ") c",
                        "GROUP BY day",
                    ]
                ),
                {"start": start, "max_rows": _MAX_SCAN_ROWS},
            )
            for row in agg_rows:
                day = row["day"]
                day_totals[day] = int(row["total"])
                avg = row.get("avg_length")
                day_avg_length[day] = float(avg) if avg is not None else 0.0

            # Docs whose content_hash appears >1x WITHIN the same bucket —
            # the corpusx EXISTS pattern, bucket-scoped on both sides.
            dup_rows = self._index.execute(
                " ".join(
                    [
                        f"SELECT {bucket_key_c} AS day, count(*) AS n",
                        "FROM (",
                        _SCAN_SOURCE.format(cols="fetch_ts, capture_id, content_hash"),
                        ") c",
                        "WHERE c.content_hash IS NOT NULL",
                        "AND TRIM(CAST(c.content_hash AS VARCHAR)) != ''",
                        "AND EXISTS (",
                        "SELECT 1 FROM captures o",
                        "WHERE o.content_hash = c.content_hash",
                        "AND o.capture_id != c.capture_id",
                        "AND o.fetch_ts >= $start",
                        f"AND {bucket_key_o} = {bucket_key_c}",
                        ")",
                        "GROUP BY day",
                    ]
                ),
                {"start": start, "max_rows": _MAX_SCAN_ROWS},
            )
            for row in dup_rows:
                day_dups[row["day"]] = int(row["n"])

            # Non-root dup-group members with a sibling in the same bucket.
            near_rows = self._index.execute(
                " ".join(
                    [
                        f"SELECT {bucket_key_c} AS day, count(*) AS n",
                        "FROM (",
                        _SCAN_SOURCE.format(
                            cols="fetch_ts, capture_id, doc_id, parent_doc_or_dup_group"
                        ),
                        ") c",
                        "WHERE c.parent_doc_or_dup_group IS NOT NULL",
                        "AND TRIM(CAST(c.parent_doc_or_dup_group AS VARCHAR)) != ''",
                        "AND c.parent_doc_or_dup_group != c.doc_id",
                        "AND EXISTS (",
                        "SELECT 1 FROM captures o",
                        "WHERE o.parent_doc_or_dup_group = c.parent_doc_or_dup_group",
                        "AND o.capture_id != c.capture_id",
                        "AND o.fetch_ts >= $start",
                        f"AND {bucket_key_o} = {bucket_key_c}",
                        ")",
                        "GROUP BY day",
                    ]
                ),
                {"start": start, "max_rows": _MAX_SCAN_ROWS},
            )
            for row in near_rows:
                day_near_dups[row["day"]] = int(row["n"])

            # First-seen day per domain over the whole corpus (so a domain is
            # "new" only on the day of its very first capture, even when the
            # window is shorter than the corpus).
            domain_rows = self._index.execute(
                " ".join(
                    [
                        f"SELECT {first_seen_key} AS day, count(*) AS n",
                        "FROM (",
                        "SELECT domain, min(fetch_ts) AS first_seen",
                        "FROM captures",
                        "WHERE domain IS NOT NULL",
                        "AND TRIM(CAST(domain AS VARCHAR)) != ''",
                        "GROUP BY domain",
                        ") _first",
                        "WHERE first_seen >= $start",
                        "GROUP BY day",
                    ]
                ),
                {"start": start},
            )
            for row in domain_rows:
                day_new_domains[row["day"]] = int(row["n"])

        points: list[QualityPoint] = []
        for day in buckets:
            total = day_totals.get(day, 0)
            points.append(
                QualityPoint(
                    ts=day,
                    total=total,
                    duplicate_ratio=day_dups.get(day, 0) / total if total else 0.0,
                    near_duplicate_ratio=day_near_dups.get(day, 0) / total if total else 0.0,
                    avg_length=day_avg_length.get(day, 0.0),
                    new_domains=day_new_domains.get(day, 0),
                    capture_rate=float(total) / _bucket_days(day, granularity),
                )
            )
        return points

    # ── point-in-time snapshot (delegated) ───────────────────────────────

    def current(self) -> dict[str, Any]:
        """Point-in-time corpus-quality snapshot as a JSON-ready dict.

        Delegates to :meth:`CorpusXEngine.quality_snapshot` (whole corpus);
        an empty corpus yields the zeroed snapshot, never an error.
        """
        if self._index is None:
            return _zero_snapshot().model_dump(mode="json")
        return CorpusXEngine(self._index).quality_snapshot().model_dump(mode="json")
