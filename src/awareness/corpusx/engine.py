"""Corpus-quality engine: term x domain matrix and corpus health metrics.

:class:`CorpusXEngine` wraps a :class:`~awareness.storage.duckdb_index.DuckDbIndex`
and answers two questions about the captured corpus with bounded, fully
parameterized DuckDB SQL (term content is always bound, never interpolated):

* ``topic_matrix`` — for each term (max 20), the doc count per top domain
  (ranked by corpus volume) within a fetch window, as a rectangular
  term x domain matrix with per-term / per-domain totals.
* ``quality_snapshot`` — corpus health: total captures, empty-text count,
  exact- and near-duplicate ratios, average text length, language rollup,
  top domains, distinct dedup groups and the per-day capture rate.

Design notes:

* Word-boundary, case-insensitive matching reuses the analytics engine's
  ``(?i)\\b<term>\\b`` pattern (``re.escape``d, bound as a query parameter).
* Time windows use ``fetch_ts`` (UTC) and default to the corpus tail:
  ``start = max(fetch_ts) - window_days``.
* An empty corpus returns zeroed results, never an exception.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from awareness.analytics.models import DomainCount
from awareness.corpusx.models import DomainTermCell, QualitySnapshot, TopicMatrix
from awareness.obs.logging import get_logger
from awareness.storage.duckdb_index import DuckDbIndex
from awareness.util.lang import PRIMARY_LANGUAGE_SQL

logger = get_logger("corpusx.engine")

# Hard caps that keep every query bounded (overload guards).
_MAX_TERMS = 20
_MAX_TERM_LEN = 200
_MAX_WINDOW_DAYS = 365
_MAX_TOP_DOMAINS = 100

# Constant window predicate; appended to queries only when windowing.
_WINDOW_CLAUSE = "fetch_ts >= $start"


def _validate_term(term: str) -> str:
    """Strip + validate a term; raise :class:`ValueError` when unusable."""
    cleaned = (term or "").strip()
    if not cleaned:
        raise ValueError("term must not be empty")
    if len(cleaned) > _MAX_TERM_LEN:
        raise ValueError(f"term must be at most {_MAX_TERM_LEN} characters")
    return cleaned


def _validate_window_days(window_days: int | None) -> int:
    """Validate a window length (1..365); raise :class:`ValueError` otherwise."""
    days = int(window_days)
    if not 1 <= days <= _MAX_WINDOW_DAYS:
        raise ValueError(f"window_days must be between 1 and {_MAX_WINDOW_DAYS}")
    return days


def _term_pattern(term: str) -> str:
    """Word-boundary, case-insensitive regex pattern for a term.

    The pattern is bound as a query parameter (never interpolated into SQL)
    and ``re.escape`` protects regex metacharacters in the term.
    """
    return "(?i)\\b" + re.escape(term) + "\\b"


def _zero_snapshot() -> QualitySnapshot:
    """Zeroed snapshot for an empty (or fully outside-window) corpus."""
    return QualitySnapshot(
        total_captures=0,
        empty_text=0,
        duplicate_ratio=0.0,
        near_duplicate_ratio=0.0,
        avg_length=0.0,
        languages={},
        top_domains=[],
        dedup_group_count=0,
        capture_rate_per_day=0.0,
    )


class CorpusXEngine:
    """Corpus-quality analytics over the DuckDbIndex ``captures`` view."""

    def __init__(self, index: DuckDbIndex) -> None:
        self._index = index

    # ── window helpers ───────────────────────────────────────────────────

    def _corpus_bounds(self) -> tuple[datetime | None, datetime | None]:
        """Earliest/latest ``fetch_ts`` in the corpus, or ``(None, None)``."""
        rows = self._index.execute(
            "SELECT min(fetch_ts) AS lo, max(fetch_ts) AS hi FROM captures"
        )
        if not rows:
            return None, None
        return rows[0].get("lo"), rows[0].get("hi")

    def _windowed(self, window_days: int | None) -> dict[str, Any]:
        """Resolve an optional window: params + whether the window applies.

        ``window_days=None`` means the whole corpus. Otherwise the window
        runs from ``max(fetch_ts) - window_days`` through ``max(fetch_ts)``
        (the corpus tail), mirroring the analytics engine's default window.
        """
        if window_days is None:
            return {}
        days = _validate_window_days(window_days)
        _, hi = self._corpus_bounds()
        if hi is None:
            return {"params": {}, "empty": True}
        return {"params": {"start": hi - timedelta(days=days)}, "empty": False}

    # ── topic matrix ─────────────────────────────────────────────────────

    def topic_matrix(
        self,
        terms: list[str],
        window_days: int = 30,
        top_domains: int = 20,
    ) -> TopicMatrix:
        """Doc counts per (term, domain) for *terms* within the fetch window.

        Rows are the validated *terms* (at most :data:`_MAX_TERMS`), columns
        the *top_domains* domains by total in-window volume. Cells are
        rectangular (zero counts included); ``totals`` carries per-term and
        per-domain sums.
        """
        if not terms:
            raise ValueError("terms must not be empty")
        cleaned = [_validate_term(term) for term in terms]
        if len(cleaned) > _MAX_TERMS:
            raise ValueError(f"at most {_MAX_TERMS} terms are supported")
        _validate_window_days(window_days)
        top = min(max(int(top_domains), 1), _MAX_TOP_DOMAINS)

        lo, hi = self._corpus_bounds()
        if lo is None or hi is None:
            return TopicMatrix(
                terms=cleaned,
                domains=[],
                cells=[],
                totals={"terms": {t: 0 for t in cleaned}, "domains": {}},
            )

        params: dict[str, Any] = {"start": hi - timedelta(days=window_days), "end": hi}
        domain_rows = self._index.execute(
            " ".join(
                [
                    "SELECT domain, count(*) AS n",
                    "FROM captures",
                    f"WHERE {_WINDOW_CLAUSE} AND fetch_ts <= $end",
                    "AND domain IS NOT NULL AND TRIM(CAST(domain AS VARCHAR)) != ''",
                    "GROUP BY domain",
                    "ORDER BY n DESC, domain ASC",
                    "LIMIT $limit",
                ]
            ),
            {**params, "limit": top},
        )
        domains = [str(r["domain"]) for r in domain_rows]
        domain_totals = {str(r["domain"]): int(r["n"]) for r in domain_rows}

        cells: list[DomainTermCell] = []
        term_totals = {t: 0 for t in cleaned}
        if domains:
            query_params = {**params, "domains": domains}
            for term in cleaned:
                query_params["pat"] = _term_pattern(term)
                rows = self._index.execute(
                    " ".join(
                        [
                            "SELECT domain, count(*) AS n",
                            "FROM captures",
                            "WHERE",
                            "(COALESCE(regexp_matches(title, $pat), false)",
                            " OR COALESCE(regexp_matches(text, $pat), false))",
                            f"AND {_WINDOW_CLAUSE} AND fetch_ts <= $end",
                            "AND domain IN (SELECT unnest($domains))",
                            "GROUP BY domain",
                        ]
                    ),
                    query_params,
                )
                per_domain = {str(r["domain"]): int(r["n"]) for r in rows}
                for domain in domains:
                    count = per_domain.get(domain, 0)
                    term_totals[term] += count
                    cells.append(DomainTermCell(term=term, domain=domain, count=count))

        return TopicMatrix(
            terms=cleaned,
            domains=domains,
            cells=cells,
            totals={"terms": term_totals, "domains": domain_totals},
        )

    # ── quality snapshot ─────────────────────────────────────────────────

    def quality_snapshot(self, window_days: int | None = None) -> QualitySnapshot:
        """Corpus health metrics over *window_days* (``None`` = whole corpus).

        Ratios are fractions of ``total_captures``; ``languages`` rolls up to
        BCP-47 primary subtags (top 10, undetected → ``"unknown"``);
        ``top_domains`` are the top 10 by capture count.
        """
        params: dict[str, Any] = {}
        windowed = False
        if window_days is not None:
            _validate_window_days(window_days)
            _, hi = self._corpus_bounds()
            if hi is None:
                return _zero_snapshot()
            params["start"] = hi - timedelta(days=int(window_days))
            windowed = True

        agg_parts = [
            "SELECT",
            "count(*) AS total,",
            "count(*) FILTER (WHERE NULLIF(TRIM(text), '') IS NULL) AS empty_text,",
            "avg(length(COALESCE(text, ''))) AS avg_length,",
            "min(fetch_ts) AS lo,",
            "max(fetch_ts) AS hi",
            "FROM captures",
        ]
        if windowed:
            agg_parts.append(f"WHERE {_WINDOW_CLAUSE}")
        row = self._index.execute(" ".join(agg_parts), params)[0]

        total = int(row["total"])
        if total == 0:
            return _zero_snapshot()
        empty_text = int(row["empty_text"])
        avg_length = float(row["avg_length"]) if row["avg_length"] is not None else 0.0
        lo, hi = row.get("lo"), row.get("hi")

        dup_rows = self._index.execute(
            " ".join(
                [
                    "SELECT count(*) AS n",
                    "FROM captures c",
                    "WHERE c.content_hash IS NOT NULL",
                    "AND TRIM(CAST(c.content_hash AS VARCHAR)) != ''",
                    "AND EXISTS (",
                    "SELECT 1 FROM captures o",
                    "WHERE o.content_hash = c.content_hash",
                    "AND o.capture_id != c.capture_id",
                    *(("AND " + _WINDOW_CLAUSE,) if windowed else ()),
                    ")",
                    *(("AND " + _WINDOW_CLAUSE,) if windowed else ()),
                ]
            ),
            params,
        )
        duplicate_ratio = int(dup_rows[0]["n"]) / total

        near_rows = self._index.execute(
            " ".join(
                [
                    "SELECT count(*) AS n",
                    "FROM captures c",
                    "WHERE c.parent_doc_or_dup_group IS NOT NULL",
                    "AND TRIM(CAST(c.parent_doc_or_dup_group AS VARCHAR)) != ''",
                    "AND c.parent_doc_or_dup_group != c.doc_id",
                    "AND EXISTS (",
                    "SELECT 1 FROM captures o",
                    "WHERE o.parent_doc_or_dup_group = c.parent_doc_or_dup_group",
                    "AND o.capture_id != c.capture_id",
                    *(("AND " + _WINDOW_CLAUSE,) if windowed else ()),
                    ")",
                    *(("AND " + _WINDOW_CLAUSE,) if windowed else ()),
                ]
            ),
            params,
        )
        near_duplicate_ratio = int(near_rows[0]["n"]) / total

        group_rows = self._index.execute(
            " ".join(
                [
                    "SELECT count(*) AS n",
                    "FROM (",
                    "SELECT parent_doc_or_dup_group",
                    "FROM captures",
                    "WHERE parent_doc_or_dup_group IS NOT NULL",
                    "AND TRIM(CAST(parent_doc_or_dup_group AS VARCHAR)) != ''",
                    *(("AND " + _WINDOW_CLAUSE,) if windowed else ()),
                    "GROUP BY parent_doc_or_dup_group",
                    "HAVING count(*) >= 2",
                    ") g",
                ]
            ),
            params,
        )
        dedup_group_count = int(group_rows[0]["n"])

        lang_rows = self._index.execute(
            " ".join(
                [
                    "SELECT",
                    PRIMARY_LANGUAGE_SQL,
                    "AS language, count(*) AS n",
                    "FROM captures",
                    *(("WHERE " + _WINDOW_CLAUSE,) if windowed else ()),
                    "GROUP BY 1",
                    "ORDER BY n DESC, language ASC NULLS LAST",
                    "LIMIT 10",
                ]
            ),
            params,
        )
        languages: dict[str, int] = {}
        for lang_row in lang_rows:
            lang = lang_row.get("language")
            if lang is None or not str(lang).strip():
                lang = "unknown"
            languages[str(lang)] = int(lang_row["n"])

        domain_rows = self._index.execute(
            " ".join(
                [
                    "SELECT domain, count(*) AS n",
                    "FROM captures",
                    "WHERE domain IS NOT NULL AND TRIM(CAST(domain AS VARCHAR)) != ''",
                    *(("AND " + _WINDOW_CLAUSE,) if windowed else ()),
                    "GROUP BY domain",
                    "ORDER BY n DESC, domain ASC",
                    "LIMIT 10",
                ]
            ),
            params,
        )
        top_domains = [DomainCount(domain=str(r["domain"]), count=int(r["n"])) for r in domain_rows]

        capture_rate_per_day = 0.0
        if lo is not None and hi is not None:
            spanned = (hi - lo).total_seconds() / 86400.0
            if spanned > 0:
                capture_rate_per_day = total / spanned

        return QualitySnapshot(
            total_captures=total,
            empty_text=empty_text,
            duplicate_ratio=duplicate_ratio,
            near_duplicate_ratio=near_duplicate_ratio,
            avg_length=avg_length,
            languages=languages,
            top_domains=top_domains,
            dedup_group_count=dedup_group_count,
            capture_rate_per_day=capture_rate_per_day,
        )
