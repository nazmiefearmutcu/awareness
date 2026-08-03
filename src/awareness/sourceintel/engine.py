"""Source-quality / replication-analysis engine over the captures lake.

Mines the ``captures`` view (see ``awareness.storage.duckdb_index``) for
per-domain source intelligence:

* ``domain_profile`` — aggregate profile of a single domain.
* ``domain_rank`` — composite quality ranking of domains.
* ``replication_map`` — directed "who copies whom" edges derived from the
  dedup structure (``parent_doc_or_dup_group``).
* ``top_replicators`` — domains that copy other domains the most.
* ``freshness_report`` — per-domain recency / staleness.

Scoring formula (``domain_rank``)
---------------------------------

    score = 0.4 * captures_norm
          + 0.3 * avg_length_norm
          + 0.2 * (1 - replication_ratio)
          + 0.1 * velocity_norm

where each component is normalized into 0..1:

* ``captures_norm``      = log1p(captures) / log1p(max_captures_in_pool)
* ``avg_length_norm``    = min-max of mean document length (chars) over the
  ranked pool (1.0 when the pool is uniform)
* ``replication_ratio``  = fraction of the domain's captures whose
  ``parent_doc_or_dup_group`` is shared with at least one capture of a
  *different* domain (i.e. syndicated copies). Already in 0..1.
* ``velocity_norm``      = captures/day over the last 30 days (intersected
  with the requested window), min-max normalized over the pool.

The weighting rewards volume, substantive content length, originality
(low replication) and recency, and penalizes pure copycats.

Replication semantics
---------------------

A capture is a *copy* when its ``parent_doc_or_dup_group`` (the dedup cluster
root id) is non-NULL and shared with a capture from another domain. Within a
group containing >= 2 distinct domains, the domain with the earliest
``observed_ts`` is the originator; every other domain in the group is a
replicator of it (one edge per group per pair). NULL ``parent_doc_or_dup_group``
values are treated as unique content and never produce edges.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta
from math import log1p
from typing import Any

from awareness.obs.logging import get_logger
from awareness.sourceintel.models import (
    DomainFreshness,
    DomainProfile,
    DomainScore,
    LanguageShare,
    ReplicationEdge,
    SourceTypeShare,
    TermCount,
)
from awareness.storage.duckdb_index import DuckDbIndex
from awareness.util.lang import PRIMARY_LANGUAGE_SQL
from awareness.util.timeutil import inclusive_end, to_utc, utcnow
from awareness.util.urls import domain_of

logger = get_logger("sourceintel")

# Window (days) used for replication edges and velocity by default.
DEFAULT_REPLICATION_WINDOW_DAYS = 30
# Velocity is measured over the trailing 30 days, clamped to the query window.
VELOCITY_WINDOW_DAYS = 30
# Bounds for the replication / staleness windows.
MIN_WINDOW_DAYS = 1
MAX_WINDOW_DAYS = 3650
# Hard caps so a large corpus cannot blow up a single analysis call.
MAX_TERM_DOCS = 5000
MAX_PROFILE_LANGUAGES = 10
MAX_PROFILE_SOURCE_TYPES = 20
# Candidate dup-groups scanned per replication call (bounded pre-aggregation).
_GROUP_FETCH_FACTOR = 10
_GROUP_FETCH_MIN = 200

# Small English stopword list reused across all top-term extraction.
STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "if", "then", "else", "for",
        "nor", "so", "yet", "of", "in", "on", "at", "by", "with", "from",
        "to", "into", "onto", "over", "under", "again", "further", "about",
        "above", "across", "after", "before", "behind", "below", "beneath",
        "beside", "between", "during", "through", "throughout", "within",
        "without", "is", "am", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "having", "do", "does", "did", "doing", "will",
        "would", "shall", "should", "may", "might", "must", "can", "could",
        "it", "its", "this", "that", "these", "those", "i", "you", "he",
        "she", "they", "we", "them", "his", "her", "their", "our", "your",
        "my", "me", "him", "us", "not", "no", "yes", "as", "than", "very",
        "just", "also", "too",
    }
)

_TERM_RE = re.compile(r"[a-z0-9]+")
_NULL_DOMAIN_FILTER = "domain IS NOT NULL AND CAST(domain AS VARCHAR) <> ''"


class UnknownDomainError(ValueError):
    """Raised when a requested domain has no captures in the corpus."""


def _normalize_domain(domain: str) -> str:
    """Normalize a user-supplied domain to the corpus's eTLD+1 form.

    Accepts bare domains (``Example.COM``, ``www.example.com``), URLs
    (``https://www.example.com/story``) and hostnames with ports; returns the
    lowercased registered domain. Raises :class:`ValueError` when nothing
    recognizable remains.
    """
    raw = (domain or "").strip().lower()
    if not raw:
        raise ValueError("domain is empty")
    if "://" in raw or "/" in raw:
        extracted = domain_of(raw)
        if extracted:
            raw = extracted
    elif "." in raw:
        # Bare hostname: collapse alias subdomains to eTLD+1 as well.
        host = raw.split(":", 1)[0].rstrip(".")
        extracted = domain_of(host) if host else None
        if extracted:
            raw = extracted
    if ":" in raw and not raw.startswith("["):
        raw = raw.split(":", 1)[0]
    normalized = raw.rstrip(".")
    if not normalized:
        raise ValueError(f"invalid domain: {domain!r}")
    return normalized


class SourceIntelEngine:
    """Read-only source-intelligence queries over a :class:`DuckDbIndex`.

    All SQL is parameterized and bounded (LIMITs + date filters); timestamps
    are handled in UTC. Every call goes through the index's lock so this is
    safe to use from FastAPI's threadpool.
    """

    def __init__(self, index: DuckDbIndex) -> None:
        self._index = index

    # ── public surface ────────────────────────────────────────────────────

    def domain_profile(self, domain: str) -> DomainProfile:
        """Aggregate profile for *domain* (case/URL-insensitive).

        Raises :class:`UnknownDomainError` when the domain has no captures and
        :class:`ValueError` when *domain* cannot be normalized.
        """
        normalized = _normalize_domain(domain)
        rows = self._index.execute(
            """
            SELECT COUNT(*) AS total,
                   AVG(length(text)) AS avg_len,
                   MIN(observed_ts) AS first_seen,
                   MAX(observed_ts) AS last_seen
            FROM captures
            WHERE lower(domain) = $dom
            """,
            {"dom": normalized},
        )
        row = rows[0] if rows else {}
        total = int(row.get("total") or 0)
        if total == 0:
            raise UnknownDomainError(normalized)

        cutoff = utcnow() - timedelta(days=VELOCITY_WINDOW_DAYS)
        vel_rows = self._index.execute(
            """
            SELECT COUNT(*) AS n
            FROM captures
            WHERE lower(domain) = $dom AND observed_ts >= $cutoff
            """,
            {"dom": normalized, "cutoff": cutoff},
        )
        captures_per_day = (int(vel_rows[0]["n"]) if vel_rows else 0) / float(VELOCITY_WINDOW_DAYS)

        lang_rows = self._index.execute(
            f"""
            SELECT {PRIMARY_LANGUAGE_SQL} AS language, COUNT(*) AS n
            FROM captures
            WHERE lower(domain) = $dom
              AND language IS NOT NULL AND CAST(language AS VARCHAR) <> ''
            GROUP BY 1
            ORDER BY n DESC, language ASC
            LIMIT {int(MAX_PROFILE_LANGUAGES)}
            """,  # noqa: S608 -- PRIMARY_LANGUAGE_SQL is a code-owned constant
            {"dom": normalized},
        )
        languages = [LanguageShare(language=r["language"], count=int(r["n"])) for r in lang_rows]

        type_rows = self._index.execute(
            """
            SELECT source_type, COUNT(*) AS n
            FROM captures
            WHERE lower(domain) = $dom
              AND source_type IS NOT NULL AND CAST(source_type AS VARCHAR) <> ''
            GROUP BY 1
            ORDER BY n DESC, source_type ASC
            LIMIT $lim
            """,
            {"dom": normalized, "lim": MAX_PROFILE_SOURCE_TYPES},
        )
        source_types = [
            SourceTypeShare(source_type=r["source_type"], count=int(r["n"])) for r in type_rows
        ]

        return DomainProfile(
            domain=normalized,
            total_captures=total,
            first_seen=row.get("first_seen"),
            last_seen=row.get("last_seen"),
            avg_doc_length=round(float(row.get("avg_len") or 0.0), 1),
            languages=languages,
            top_terms=self._top_terms(normalized),
            captures_per_day=round(captures_per_day, 4),
            source_types=source_types,
        )

    def domain_rank(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 50,
    ) -> list[DomainScore]:
        """Rank domains by the composite quality score (see module docstring).

        *start*/*end* bound the window on ``observed_ts``; a window whose
        start is at or after its end yields an empty ranking. Results are
        ordered score desc, then captures desc, then domain asc.
        """
        limit = self._clamp_limit(limit, default=50)
        start_dt = to_utc(start)
        end_dt = inclusive_end(to_utc(end))
        if start_dt is not None and end_dt is not None and start_dt >= end_dt:
            return []

        stats = self._domain_stats(start_dt, end_dt)
        if not stats:
            return []
        velocity = self._velocity(start_dt, end_dt)

        pool_sizes = [s["total"] for s in stats.values()]
        pool_lengths = [float(s["avg_len"] or 0.0) for s in stats.values()]
        max_captures = max(pool_sizes)
        min_len, max_len = min(pool_lengths), max(pool_lengths)
        max_velocity = max(velocity.values()) if velocity else 0.0

        ranked: list[DomainScore] = []
        for domain, s in stats.items():
            total = s["total"]
            avg_len = float(s["avg_len"] or 0.0)
            replication_ratio = s["replicated"] / float(total)
            captures_norm = log1p(total) / log1p(max_captures)
            length_norm = (avg_len - min_len) / (max_len - min_len) if max_len > min_len else 1.0
            vel = velocity.get(domain, 0.0)
            velocity_norm = vel / max_velocity if max_velocity > 0.0 else 0.0
            score = (
                0.4 * captures_norm
                + 0.3 * length_norm
                + 0.2 * (1.0 - replication_ratio)
                + 0.1 * velocity_norm
            )
            ranked.append(
                DomainScore(
                    domain=domain,
                    score=round(score, 4),
                    captures=total,
                    replication_ratio=round(replication_ratio, 4),
                    avg_length=round(avg_len, 1),
                    velocity=round(vel, 4),
                )
            )
        ranked.sort(key=lambda r: (-r.score, -r.captures, r.domain))
        return ranked[:limit]

    def replication_map(
        self,
        limit: int = 50,
        window_days: int | None = None,
    ) -> list[ReplicationEdge]:
        """"Who copies whom": directed edges over multi-domain dup-groups.

        Default *window_days* is 30 (only dup-groups with at least one
        capture observed in the trailing window are considered); pass
        ``None``-style defaults are clamped into ``1..3650``. Within each
        group the domain with the earliest ``observed_ts`` is the origin;
        every other domain gets one edge from it per group. Edges are
        ordered count desc, origin asc, replica asc.
        """
        limit = self._clamp_limit(limit, default=50)
        window_days = self._clamp_window(window_days)
        group_limit = self._group_fetch_limit(limit)
        logger.debug(
            "sourceintel_replication",
            window_days=window_days,
            group_limit=group_limit,
        )
        edges = self._replication_edges(window_days=window_days, group_limit=group_limit)
        edges.sort(key=lambda e: (-e["count"], e["origin"], e["replica"]))
        return [ReplicationEdge(**e) for e in edges[:limit]]

    def top_replicators(self, limit: int = 20) -> list[DomainScore]:
        """Domains with the most outbound replication edges (they copy others).

        Ranks by the number of (originator → domain) dup-group edges over the
        whole corpus; ties break on captures desc, then domain asc. The
        ``score`` field carries the edge count.
        """
        limit = self._clamp_limit(limit, default=20)
        edges = self._replication_edges(window_days=None, group_limit=self._group_fetch_limit(limit))
        replica_counts: Counter[str] = Counter(e["replica"] for e in edges)
        if not replica_counts:
            return []
        domains = sorted(replica_counts, key=lambda d: (-replica_counts[d], d))[:limit]

        stats = self._domain_stats(None, None)
        velocity = self._velocity(None, None)
        ranked: list[DomainScore] = []
        for domain in domains:
            s = stats.get(domain, {"total": 0, "replicated": 0, "avg_len": None})
            total = s["total"]
            ranked.append(
                DomainScore(
                    domain=domain,
                    score=float(replica_counts[domain]),
                    captures=total,
                    replication_ratio=round(s["replicated"] / float(total), 4) if total else 0.0,
                    avg_length=round(float(s["avg_len"] or 0.0), 1),
                    velocity=round(velocity.get(domain, 0.0), 4),
                )
            )
        ranked.sort(key=lambda r: (-r.score, -r.captures, r.domain))
        return ranked

    def freshness_report(self, limit: int = 50) -> list[DomainFreshness]:
        """Per-domain recency: last seen, days since, 7d/30d capture counts.

        ``days_since_last`` doubles as staleness (``999`` when the domain has
        no captures at all). Ordered last_seen desc (most recent first), then
        domain asc — deterministic and cheap (one grouped scan).
        """
        limit = self._clamp_limit(limit, default=50)
        now = utcnow()
        cutoff_7d = now - timedelta(days=7)
        cutoff_30d = now - timedelta(days=30)
        rows = self._index.execute(
            """
            SELECT domain AS domain,
                   MAX(observed_ts) AS last_seen,
                   COUNT(*) FILTER (WHERE observed_ts >= $c7) AS captures_7d,
                   COUNT(*) FILTER (WHERE observed_ts >= $c30) AS captures_30d
            FROM captures
            WHERE domain IS NOT NULL AND CAST(domain AS VARCHAR) <> ''
            GROUP BY domain
            ORDER BY last_seen DESC NULLS LAST, domain ASC
            LIMIT $lim
            """,
            {"c7": cutoff_7d, "c30": cutoff_30d, "lim": limit},
        )
        report: list[DomainFreshness] = []
        for r in rows:
            last_seen = r["last_seen"]
            if last_seen is None:
                days_since = 999
            else:
                last_seen_utc = to_utc(last_seen)
                assert last_seen_utc is not None
                days_since = max(0, int((now - last_seen_utc).days))
            report.append(
                DomainFreshness(
                    domain=r["domain"],
                    last_seen=last_seen,
                    days_since_last=days_since,
                    captures_7d=int(r["captures_7d"] or 0),
                    captures_30d=int(r["captures_30d"] or 0),
                )
            )
        return report

    # ── internals ─────────────────────────────────────────────────────────

    @staticmethod
    def _clamp_limit(limit: int, *, default: int) -> int:
        return max(1, min(int(limit or default), 500))

    @staticmethod
    def _clamp_window(window_days: int | None) -> int:
        if window_days is None:
            return DEFAULT_REPLICATION_WINDOW_DAYS
        return max(MIN_WINDOW_DAYS, min(int(window_days), MAX_WINDOW_DAYS))

    @staticmethod
    def _group_fetch_limit(limit: int) -> int:
        return max(_GROUP_FETCH_MIN, limit * _GROUP_FETCH_FACTOR)

    @staticmethod
    def _window_clause(
        start: datetime | None,
        end: datetime | None,
    ) -> tuple[list[str], dict[str, Any]]:
        """WHERE fragments + params for an ``observed_ts`` window."""
        where: list[str] = []
        params: dict[str, Any] = {}
        if start is not None:
            where.append("observed_ts >= $win_start")
            params["win_start"] = start
        if end is not None:
            where.append("observed_ts <= $win_end")
            params["win_end"] = end
        return where, params

    def _domain_stats(
        self,
        start: datetime | None,
        end: datetime | None,
    ) -> dict[str, dict[str, Any]]:
        """Per-domain aggregates: total, replicated, avg length, first/last seen.

        A capture counts as *replicated* when its ``parent_doc_or_dup_group``
        is shared with a capture from a different domain (NULL groups never
        match). The dup-group CTE honors the same window as the outer scan so
        ratios are window-consistent.
        """
        win_where, params = self._window_clause(start, end)
        win_sql = ("AND " + " AND ".join(win_where)) if win_where else ""
        rows = self._index.execute(
            f"""
            WITH multi_domain_groups AS (
              SELECT parent_doc_or_dup_group AS grp
              FROM captures
              WHERE parent_doc_or_dup_group IS NOT NULL
                AND CAST(parent_doc_or_dup_group AS VARCHAR) <> ''
                AND {_NULL_DOMAIN_FILTER}
                {win_sql}
              GROUP BY parent_doc_or_dup_group
              HAVING COUNT(DISTINCT domain) >= 2
            )
            SELECT c.domain AS domain,
                   COUNT(*) AS total,
                   COUNT(CASE WHEN m.grp IS NOT NULL THEN 1 END) AS replicated,
                   AVG(length(text)) AS avg_len,
                   MIN(observed_ts) AS first_seen,
                   MAX(observed_ts) AS last_seen
            FROM captures c
            LEFT JOIN multi_domain_groups m ON m.grp = c.parent_doc_or_dup_group
            WHERE c.{_NULL_DOMAIN_FILTER}
              {win_sql}
            GROUP BY c.domain
            """,  # noqa: S608 -- interpolations are code-owned constants
            params,
        )
        return {
            r["domain"]: {
                "total": int(r["total"]),
                "replicated": int(r["replicated"] or 0),
                "avg_len": r["avg_len"],
                "first_seen": r["first_seen"],
                "last_seen": r["last_seen"],
            }
            for r in rows
        }

    def _velocity(
        self,
        start: datetime | None,
        end: datetime | None,
    ) -> dict[str, float]:
        """Captures/day per domain over the trailing 30 days, clamped to window.

        The measured interval is ``[max(start, now-30d), min(end, now)]`` so a
        rank window in the deep past reports zero velocity rather than mixing
        in captures from outside the requested range.
        """
        now = utcnow()
        lo = now - timedelta(days=VELOCITY_WINDOW_DAYS)
        hi = now
        if start is not None:
            lo = max(lo, start)
        if end is not None:
            hi = min(hi, end)
        params: dict[str, Any] = {"vel_lo": lo, "vel_hi": hi}
        rows = self._index.execute(
            f"""
            SELECT domain AS domain, COUNT(*) AS n
            FROM captures
            WHERE {_NULL_DOMAIN_FILTER}
              AND observed_ts >= $vel_lo AND observed_ts <= $vel_hi
            GROUP BY domain
            """,  # noqa: S608 -- _NULL_DOMAIN_FILTER is a code-owned constant
            params,
        )
        return {r["domain"]: int(r["n"]) / float(VELOCITY_WINDOW_DAYS) for r in rows}

    def _replication_edges(
        self,
        window_days: int | None,
        group_limit: int,
    ) -> list[dict[str, Any]]:
        """Aggregate (origin, replica) edges from multi-domain dup-groups.

        *window_days* ``None`` means all-time; otherwise only captures with
        ``observed_ts`` in the trailing window participate. The candidate
        group set is bounded by *group_limit*. Deterministic: groups ordered
        by id, members by (first_ts, domain).
        """
        cutoff = utcnow() - timedelta(days=window_days) if window_days is not None else None
        params: dict[str, Any] = {"gl": group_limit}
        win_sql = "AND observed_ts >= $cutoff" if cutoff is not None else ""
        if cutoff is not None:
            params["cutoff"] = cutoff
        rows = self._index.execute(
            f"""
            WITH multi AS (
              SELECT parent_doc_or_dup_group AS grp
              FROM captures
              WHERE parent_doc_or_dup_group IS NOT NULL
                AND CAST(parent_doc_or_dup_group AS VARCHAR) <> ''
                AND {_NULL_DOMAIN_FILTER}
                {win_sql}
              GROUP BY parent_doc_or_dup_group
              HAVING COUNT(DISTINCT domain) >= 2
              ORDER BY COUNT(DISTINCT domain) DESC
              LIMIT $gl
            )
            SELECT c.parent_doc_or_dup_group AS grp,
                   c.domain AS domain,
                   MIN(c.observed_ts) AS first_ts,
                   first(c.url ORDER BY c.observed_ts NULLS LAST) AS sample_url,
                   COUNT(*) AS n
            FROM captures c
            JOIN multi m ON m.grp = c.parent_doc_or_dup_group
            WHERE c.{_NULL_DOMAIN_FILTER}
            GROUP BY 1, 2
            ORDER BY grp ASC, first_ts ASC, domain ASC
            """,  # noqa: S608 -- interpolations are code-owned constants
            params,
        )
        by_group: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_group.setdefault(r["grp"], []).append(r)

        edges: dict[tuple[str, str], dict[str, Any]] = {}
        for grp in sorted(by_group):
            members = sorted(
                by_group[grp],
                key=lambda r: (r["first_ts"] is None, r["first_ts"], r["domain"]),
            )
            origin = members[0]
            for m in members[1:]:
                key = (origin["domain"], m["domain"])
                edge = edges.get(key)
                if edge is None:
                    sample_urls = [u for u in (origin["sample_url"], m["sample_url"]) if u]
                    edge = {
                        "origin": origin["domain"],
                        "replica": m["domain"],
                        "count": 0,
                        "sample_urls": sample_urls[:2],
                    }
                    edges[key] = edge
                edge["count"] += 1
        return list(edges.values())

    def _top_terms(self, domain: str) -> list[TermCount]:
        """Most frequent content terms in the domain's captured text (<= 20).

        Reads at most :data:`MAX_TERM_DOCS` most-recent documents (bounded)
        and tokenizes in-process, dropping the shared stopword list and pure
        digit tokens.
        """
        rows = self._index.execute(
            """
            SELECT text AS text
            FROM captures
            WHERE lower(domain) = $dom AND text IS NOT NULL
            ORDER BY observed_ts DESC NULLS LAST
            LIMIT $lim
            """,
            {"dom": domain, "lim": MAX_TERM_DOCS},
        )
        counts: Counter[str] = Counter()
        for r in rows:
            for token in _TERM_RE.findall(str(r["text"]).lower()):
                if len(token) >= 3 and token not in STOPWORDS and not token.isdigit():
                    counts[token] += 1
        return [TermCount(term=t, count=c) for t, c in counts.most_common(20)]
