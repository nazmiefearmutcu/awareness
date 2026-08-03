"""GDELT analytics bridge engine.

:class:`GdeltBridge` wraps a :class:`~awareness.storage.duckdb_index.DuckDbIndex`
and cross-references external GDELT article volume with local capture volume:

* ``gdelt_query`` — per-day article counts for a term from the GDELT DOC 2.0
  API (``mode=artlist`` JSON), cached on disk under ``{cache_dir}`` with a
  6-hour TTL. The bridge is deliberately resilient: every API failure (HTTP
  status, transport, unparseable JSON) logs a structured warning and degrades
  to ``[]`` — it never raises, so it keeps working offline.
* ``compare_with_local`` — aligned per-day local vs GDELT series for a term
  plus their Pearson correlation.
* ``coverage_gap`` — flags terms where GDELT reports big volume but local
  capture is near-zero ("you are missing this story").

Local counts reuse the analytics word-boundary pattern: case-insensitive
``\\bterm\\b`` matched with ``regexp_matches`` over ``title``/``text`` within
the ``fetch_ts`` window, bucketed per day in Python and zero-filled.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from awareness.analytics.models import TimeBucket
from awareness.config import get_settings
from awareness.gdeltx.models import GapReport, GdeltComparison, GdeltWindow
from awareness.obs.logging import get_logger
from awareness.storage.duckdb_index import DuckDbIndex
from awareness.util.timeutil import floor_to_day, to_utc, utcnow

logger = get_logger("gdeltx.engine")

GDELT_API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_TIMEOUT_SECONDS = 10.0
GDELT_CACHE_TTL_SECONDS = 6 * 3600

MAX_TERM_LEN = 80
MAX_WINDOW_DAYS = 60  # per-day GDELT calls cap; keeps a cold query bounded
MAX_MATCHING_ROWS = 100_000
MAX_GAP_TERMS = 20
GAP_MIN_GDELT_COUNT = 25  # a "big story" per GDELT needs at least this volume
GAP_RATIO_THRESHOLD = 0.1  # local/gdelt ratio below this with big gdelt → gap

GRANULARITIES: tuple[str, ...] = ("day", "week", "month")


def _validate_term(term: str) -> str:
    """Strip + validate a term; raise :class:`ValueError` when unusable."""
    cleaned = (term or "").strip()
    if not cleaned:
        raise ValueError("term must not be empty")
    if len(cleaned) > MAX_TERM_LEN:
        raise ValueError(f"term must be at most {MAX_TERM_LEN} characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise ValueError("term must not contain control characters")
    return cleaned


class GdeltBridge:
    """Cross-reference external GDELT article counts with local captures.

    *index* is a :class:`~awareness.storage.duckdb_index.DuckDbIndex` (or a
    duck-typed shim exposing ``execute``). *cache_dir* defaults to the
    configured ``data_dir/cache``; GDELT query results are cached there with
    a 6-hour TTL so repeated calls never re-hit the API.
    """

    def __init__(self, index: DuckDbIndex, cache_dir: Path | None = None) -> None:
        self._index = index
        if cache_dir is not None:
            self._cache_dir = Path(cache_dir)
        else:
            self._cache_dir = Path(get_settings().cache_dir)

    # ── window helpers ───────────────────────────────────────────────────

    @staticmethod
    def _validate_window_days(window_days: int) -> int:
        days = int(window_days)
        if not 1 <= days <= MAX_WINDOW_DAYS:
            raise ValueError(f"window_days must be between 1 and {MAX_WINDOW_DAYS}")
        return days

    @staticmethod
    def _coerce_window(start: Any, end: Any) -> tuple[datetime, datetime]:
        start_dt = to_utc(start)
        end_dt = to_utc(end)
        if start_dt is None or end_dt is None:
            raise ValueError("start and end must be valid UTC datetimes")
        if start_dt > end_dt:
            raise ValueError("start must not be after end")
        days = (floor_to_day(end_dt) - floor_to_day(start_dt)).days + 1
        if days > MAX_WINDOW_DAYS:
            raise ValueError(f"window too large: at most {MAX_WINDOW_DAYS} days")
        return start_dt, end_dt

    @staticmethod
    def _iter_days(start_dt: datetime, end_dt: datetime) -> list[datetime]:
        """Every day bucket (UTC midnight) in ``[floor(start), floor(end)]``."""
        first = floor_to_day(start_dt)
        last = floor_to_day(end_dt)
        return [first + timedelta(days=i) for i in range((last - first).days + 1)]

    @staticmethod
    def _floor_bucket(dt: datetime, granularity: str) -> datetime:
        day = floor_to_day(dt)
        if granularity == "day":
            return day
        if granularity == "week":
            return day - timedelta(days=day.weekday())  # Monday
        return day.replace(day=1)

    # ── GDELT DOC 2.0 API ────────────────────────────────────────────────

    async def _gdelt_counts(self, term: str, start: Any, end: Any) -> list[GdeltWindow]:
        """Fetch per-day GDELT article counts for *term* over the window.

        One DOC 2.0 call per day (``mode=artlist``, JSON), counting the
        returned article list. A non-200 status, transport error, or
        unparseable payload is retried once; if the day still fails the whole
        call degrades to ``[]`` with a warning (never raises), so the bridge
        stays usable when offline.
        """
        cleaned = _validate_term(term)
        start_dt, end_dt = self._coerce_window(start, end)
        days = self._iter_days(start_dt, end_dt)
        results: list[GdeltWindow] = []
        async with httpx.AsyncClient(timeout=GDELT_TIMEOUT_SECONDS) as client:
            for day in days:
                params = {
                    "query": cleaned,
                    "mode": "artlist",
                    "maxrecords": "250",
                    "format": "json",
                    "startdatetime": day.strftime("%Y%m%d%H%M%S"),
                    "enddatetime": day.replace(hour=23, minute=59, second=59).strftime("%Y%m%d%H%M%S"),
                }
                last_err: BaseException | None = None
                for _ in (0, 1):  # one retry
                    try:
                        r = await client.get(GDELT_API_URL, params=params)
                        if r.status_code != 200:
                            raise ValueError(f"GDELT API returned HTTP {r.status_code}")
                        payload = r.json()
                        if not isinstance(payload, dict):
                            raise ValueError("GDELT API returned a non-object JSON payload")
                        articles = payload.get("articles")
                        if not isinstance(articles, list):
                            raise ValueError("GDELT API payload has no articles list")
                        # GDELT DOC 2.0 artlist caps at 250 records/request; a
                        # day that returns exactly 250 may be truncated (the
                        # count is then a floor, flagged via count==250).
                        results.append(
                            GdeltWindow(
                                term=cleaned, ts=day, count=len(articles), truncated=len(articles) >= 250
                            )
                        )
                        break
                    except (httpx.HTTPError, ValueError, TypeError) as exc:
                        last_err = exc
                else:
                    logger.warning(
                        "gdeltx_api_failed",
                        term=cleaned,
                        day=day.isoformat(),
                        err=str(last_err),
                    )
                    return []
        return results

    @staticmethod
    def _aggregate(windows: list[GdeltWindow], granularity: str) -> list[GdeltWindow]:
        """Re-bucket per-day windows to week/month; day passes through."""
        if granularity == "day":
            return windows
        buckets: Counter[datetime] = Counter()
        for window in windows:
            buckets[GdeltBridge._floor_bucket(window.ts, granularity)] += window.count
        term = windows[0].term
        return [GdeltWindow(term=term, ts=bucket, count=buckets[bucket]) for bucket in sorted(buckets)]

    # ── disk cache ───────────────────────────────────────────────────────

    def _cache_path(self, term: str, start_dt: datetime, end_dt: datetime, granularity: str) -> Path:
        # Floor the end to the day: data is day-bucketed, so requests for the
        # same day range must share a cache entry even though utcnow() ticks.
        key = json.dumps(
            {
                "term": term.lower(),
                "start": start_dt.isoformat(),
                "end": end_dt.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
                "granularity": granularity,
            },
            sort_keys=True,
        )
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._cache_dir / f"gdeltx_{digest}.json"

    def _cache_read(self, cache_path: Path) -> list[GdeltWindow] | None:
        """Windows from the cache file, or None on miss / stale / corrupt."""
        try:
            st = cache_path.stat()
            if time.time() - st.st_mtime > GDELT_CACHE_TTL_SECONDS:
                return None
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            raw_windows = payload.get("windows")
            if not isinstance(raw_windows, list):
                return None
            windows: list[GdeltWindow] = []
            for item in raw_windows:
                if not isinstance(item, dict):
                    return None
                ts = datetime.fromisoformat(str(item.get("ts")))
                windows.append(
                    GdeltWindow(term=str(item.get("term")), ts=ts, count=int(item.get("count", 0)))
                )
            return windows
        except (OSError, ValueError, TypeError):
            return None

    def _cache_write(self, cache_path: Path, windows: list[GdeltWindow]) -> None:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "cached_at": utcnow().isoformat(),
                "windows": [window.model_dump(mode="json") for window in windows],
            }
            tmp = cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(cache_path)
        except OSError as exc:
            logger.warning("gdeltx_cache_write_failed", err=str(exc))

    # ── public surface ───────────────────────────────────────────────────

    def gdelt_query(
        self,
        term: str,
        start: Any,
        end: Any,
        granularity: str = "day",
    ) -> list[GdeltWindow]:
        """GDELT article counts per bucket for *term* over the window.

        Cached on disk (``gdeltx_<sha>.json``, TTL 6h) keyed by
        (term, start, end, granularity). API failures degrade to ``[]`` with
        a warning — never raise.
        """
        cleaned = _validate_term(term)
        start_dt, end_dt = self._coerce_window(start, end)
        if granularity not in GRANULARITIES:
            raise ValueError(f"invalid granularity: {granularity!r}")
        cache_path = self._cache_path(cleaned, start_dt, end_dt, granularity)
        cached = self._cache_read(cache_path)
        if cached is not None:
            return cached
        windows = asyncio.run(self._gdelt_counts(cleaned, start_dt, end_dt))
        if not windows:
            return []
        out = self._aggregate(windows, granularity)
        self._cache_write(cache_path, out)
        return out

    def _local_daily_counts(self, term: str, start_dt: datetime, end_dt: datetime) -> list[GdeltWindow]:
        """Per-day local capture counts for *term* in the window.

        Analytics-style word-boundary match (``\\bterm\\b``, case-insensitive)
        over ``title``/``text`` with the ``fetch_ts`` window, zero-filled per
        day so the series is chart-ready and aligned with the GDELT days.
        """
        pat = "(?i)\\b" + re.escape(term) + "\\b"
        rows = self._index.execute(
            " ".join(
                [
                    "SELECT fetch_ts",
                    "FROM captures",
                    "WHERE",
                    "(COALESCE(regexp_matches(title, $pat), false)"
                    " OR COALESCE(regexp_matches(text, $pat), false))",
                    "AND fetch_ts >= $start AND fetch_ts <= $end",
                    "ORDER BY fetch_ts DESC",
                    "LIMIT $max_rows",
                ]
            ),
            {"pat": pat, "start": start_dt, "end": end_dt, "max_rows": MAX_MATCHING_ROWS},
        )
        counts: Counter[datetime] = Counter()
        for row in rows:
            ts = row.get("fetch_ts")
            if ts is not None:
                counts[floor_to_day(ts)] += 1
        return [
            GdeltWindow(term=term, ts=day, count=counts.get(day, 0))
            for day in self._iter_days(start_dt, end_dt)
        ]

    @staticmethod
    def _zero_variance(counts: list[int]) -> bool:
        if len(counts) < 2:
            return True
        return max(counts) == min(counts)

    @staticmethod
    def _pearson(local_counts: list[int], gdelt_counts: list[int]) -> float:
        """Pearson r between the two series; 0.0 when undefined."""
        if len(local_counts) != len(gdelt_counts) or len(local_counts) < 2:
            return 0.0
        if GdeltBridge._zero_variance(local_counts) or GdeltBridge._zero_variance(gdelt_counts):
            return 0.0
        a = np.asarray(local_counts, dtype=np.float64)
        b = np.asarray(gdelt_counts, dtype=np.float64)
        r = float(np.corrcoef(a, b)[0, 1])
        return r if np.isfinite(r) else 0.0

    def compare_with_local(self, term: str, window_days: int = 14) -> GdeltComparison:
        """Compare local capture volume with external GDELT volume for *term*.

        Both series cover the last *window_days* days (UTC), zero-filled and
        aligned; ``correlation_r`` is their Pearson coefficient (``0.0`` with
        a note when it cannot be computed). A GDELT API failure keeps the
        response valid: ``gdelt_series`` is empty and the ``note`` says so.
        """
        cleaned = _validate_term(term)
        days = self._validate_window_days(window_days)
        end_dt = utcnow()
        start_dt = floor_to_day(end_dt - timedelta(days=days - 1))

        local_windows = self._local_daily_counts(cleaned, start_dt, end_dt)
        local_counts = [window.count for window in local_windows]
        local_series = [TimeBucket(ts=window.ts, count=window.count) for window in local_windows]

        gdelt_windows = self.gdelt_query(cleaned, start_dt, end_dt)
        if gdelt_windows:
            by_day = {floor_to_day(window.ts): window.count for window in gdelt_windows}
            gdelt_counts = [by_day.get(window.ts, 0) for window in local_windows]
            gdelt_series = [
                TimeBucket(ts=window.ts, count=count)
                for window, count in zip(local_windows, gdelt_counts, strict=True)
            ]
        else:
            gdelt_counts = []
            gdelt_series = []

        local_count = sum(local_counts)
        gdelt_count = sum(gdelt_counts)
        correlation_r = self._pearson(local_counts, gdelt_counts)

        note_parts: list[str] = []
        if not gdelt_windows:
            note_parts.append("gdelt API unavailable; gdelt_series empty")
        if local_count == 0:
            note_parts.append("no local captures match the term in the window")
        if gdelt_windows and (
            self._zero_variance(local_counts) or self._zero_variance(gdelt_counts)
        ):
            note_parts.append("correlation undefined (zero variance); reported 0.0")

        return GdeltComparison(
            term=cleaned,
            local_count=local_count,
            gdelt_count=gdelt_count,
            local_series=local_series,
            gdelt_series=gdelt_series,
            correlation_r=correlation_r,
            n_days=days,
            note="; ".join(note_parts),
        )

    def coverage_gap(self, terms: list[str], window_days: int = 7) -> list[GapReport]:
        """Flag terms where GDELT volume is high but local capture is near-zero.

        For each term (validated, deduped, capped at ``MAX_GAP_TERMS``) the
        ratio ``local_count / gdelt_count`` is computed over the window; a
        term is a gap when GDELT volume is big (``gdelt_count >= 25``) while
        the ratio is below ``0.1`` — the "you are missing this story"
        signal. Gaps are sorted first, then by descending GDELT volume.
        """
        cleaned: list[str] = []
        for term in terms or []:
            term_clean = _validate_term(term)
            if term_clean not in cleaned:
                cleaned.append(term_clean)
        cleaned = cleaned[:MAX_GAP_TERMS]
        if not cleaned:
            return []
        days = self._validate_window_days(window_days)
        end_dt = utcnow()
        start_dt = floor_to_day(end_dt - timedelta(days=days - 1))

        reports: list[GapReport] = []
        for term in cleaned:
            local_count = sum(
                window.count for window in self._local_daily_counts(term, start_dt, end_dt)
            )
            gdelt_count = sum(window.count for window in self.gdelt_query(term, start_dt, end_dt))
            ratio = (local_count / gdelt_count) if gdelt_count > 0 else 0.0
            reports.append(
                GapReport(
                    term=term,
                    local_count=local_count,
                    gdelt_count=gdelt_count,
                    ratio=round(ratio, 4),
                    gap=gdelt_count >= GAP_MIN_GDELT_COUNT and ratio < GAP_RATIO_THRESHOLD,
                )
            )
        reports.sort(key=lambda report: (not report.gap, -report.gdelt_count, report.term))
        return reports
