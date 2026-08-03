"""Breaking-news origin tracking over the captures lake.

:class:`OriginEngine` mines the ``captures`` view (see
``awareness.storage.duckdb_index``) for "who broke this story first":

* ``story_origins`` — for docs containing a term, group by dedup cluster
  (``parent_doc_or_dup_group``), take the earliest-observed doc as the
  origin, and list the distinct domains that syndicated it later.
* ``publisher_firsts`` — rank domains by how often they were the origin of
  a tracked cluster.

Origin semantics
----------------

A capture belongs to a *story cluster* when its ``parent_doc_or_dup_group``
is non-NULL (the dedup cluster root id). Within a cluster containing >= 2
docs, the doc with the earliest ``observed_ts`` is the origin; every other
distinct domain in the cluster is a replica of it. NULL
``parent_doc_or_dup_group`` values cannot be clustered and are skipped, as
are NULL/empty domains. All SQL is parameterized and bounded
(``LIMIT`` + date filters); timestamps are handled in UTC.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from awareness.obs.logging import get_logger
from awareness.origin.models import PublisherFirst, Replica, StoryOrigin
from awareness.storage.duckdb_index import DuckDbIndex

logger = get_logger("origin.engine")

# Hard caps so a large corpus cannot blow up a single analysis call.
MAX_DOCS_SCAN = 5000  # matching docs scanned for cluster detection
MAX_LIMIT = 500
MAX_TERM_LEN = 200
MAX_WINDOW_DAYS = 365

_NULL_DOMAIN_FILTER = "domain IS NOT NULL AND TRIM(CAST(domain AS VARCHAR)) != ''"


def _validate_term(term: str) -> str:
    """Strip + validate a term; raise :class:`ValueError` when unusable."""
    cleaned = (term or "").strip()
    if not cleaned:
        raise ValueError("term must not be empty")
    if len(cleaned) > MAX_TERM_LEN:
        raise ValueError(f"term must be at most {MAX_TERM_LEN} characters")
    return cleaned


def _validate_window_days(window_days: int) -> int:
    """Validate a window length (1..365); raise :class:`ValueError` otherwise."""
    days = int(window_days)
    if not 1 <= days <= MAX_WINDOW_DAYS:
        raise ValueError(f"window_days must be between 1 and {MAX_WINDOW_DAYS}")
    return days


def _word_pattern(term: str) -> str:
    """Word-boundary regex pattern for a term (case-insensitive).

    The pattern itself is bound as a query parameter (never interpolated
    into SQL) and ``re.escape`` protects regex metacharacters in the term.
    """
    return rf"(?i)\b{re.escape(term)}\b"


class OriginEngine:
    """Breaking-news origin queries over a :class:`DuckDbIndex`.

    All SQL is parameterized and bounded (LIMITs + date filters); timestamps
    are handled in UTC. Every call goes through the index's lock so this is
    safe to use from FastAPI's threadpool.
    """

    def __init__(self, index: DuckDbIndex) -> None:
        self._index = index

    # ── public surface ────────────────────────────────────────────────────

    def story_origins(
        self,
        term: str,
        window_days: int = 30,
        limit: int = 20,
    ) -> list[StoryOrigin]:
        """Origins of breaking-news clusters containing *term*.

        Docs containing *term* (word-boundary, title or text) with
        ``observed_ts`` in the trailing window are grouped by dedup cluster.
        Clusters with fewer than 2 docs (or NULL groups) are skipped. Each
        surviving cluster reports its origin doc (earliest ``observed_ts``)
        and the distinct replica domains with their earliest capture times;
        ``lead_minutes`` is the gap to the earliest replica. Results are
        ordered replica count desc, then origin timestamp asc; at most
        :data:`MAX_DOCS_SCAN` matching docs are scanned.
        """
        cleaned = _validate_term(term)
        days = _validate_window_days(window_days)
        limit = max(1, min(int(limit), MAX_LIMIT))
        lo, hi = self._corpus_bounds()
        if lo is None or hi is None:
            return []
        clusters = self._clusters(cleaned, hi - timedelta(days=days), hi)
        ranked = sorted(
            clusters,
            key=lambda c: (-len(c["replicas"]), c["origin_ts"]),
        )
        return [
            StoryOrigin(
                term=cleaned,
                origin_domain=c["origin_domain"],
                origin_url=c["origin_url"],
                origin_title=c["origin_title"],
                origin_ts=c["origin_ts"],
                replica_count=len(c["replicas"]),
                replicas=c["replicas"],
                lead_minutes=c["lead_minutes"],
            )
            for c in ranked[:limit]
        ]

    def publisher_firsts(
        self,
        term: str,
        window_days: int = 30,
        limit: int = 20,
    ) -> list[PublisherFirst]:
        """Rank publishers by how often they broke tracked stories first.

        Uses the same clusters as :meth:`story_origins`; a domain's
        ``origin_count`` is the number of clusters where it was the origin.
        Only domains that originated at least one cluster are ranked;
        results are ordered origin count desc, then stories participated in
        desc, then domain asc.
        """
        cleaned = _validate_term(term)
        days = _validate_window_days(window_days)
        limit = max(1, min(int(limit), MAX_LIMIT))
        lo, hi = self._corpus_bounds()
        if lo is None or hi is None:
            return []
        clusters = self._clusters(cleaned, hi - timedelta(days=days), hi)
        origin_counts: Counter[str] = Counter()
        involvement: Counter[str] = Counter()
        for c in clusters:
            origin_counts[c["origin_domain"]] += 1
            involvement[c["origin_domain"]] += 1
            for replica in c["replicas"]:
                involvement[replica.domain] += 1
        ranked = sorted(
            origin_counts,
            key=lambda d: (-origin_counts[d], -involvement[d], d),
        )
        return [
            PublisherFirst(
                domain=d,
                origin_count=origin_counts[d],
                total_stories=involvement[d],
            )
            for d in ranked[:limit]
        ]

    # ── internals ─────────────────────────────────────────────────────────

    def _corpus_bounds(self) -> tuple[datetime | None, datetime | None]:
        """Earliest/latest non-NULL ``observed_ts``, or ``(None, None)``."""
        rows = self._index.execute(
            " ".join(
                [
                    "SELECT min(observed_ts) AS lo, max(observed_ts) AS hi",
                    "FROM captures",
                    "WHERE observed_ts IS NOT NULL",
                ]
            )
        )
        if not rows:
            return None, None
        return rows[0].get("lo"), rows[0].get("hi")

    def _clusters(
        self,
        term: str,
        start_dt: datetime,
        end_dt: datetime,
    ) -> list[dict[str, Any]]:
        """Dedup clusters (>= 2 docs) containing *term*, with origin + replicas.

        Scan is bounded at :data:`MAX_DOCS_SCAN` newest-first so truncation
        keeps the most recent clusters. Groups are processed in ascending
        group-id order; members in ascending ``(observed_ts, domain)`` order
        so the earliest doc is always the origin and ties are deterministic.
        """
        rows = self._index.execute(
            " ".join(
                [
                    "SELECT parent_doc_or_dup_group AS grp, domain AS domain,",
                    "url AS url, title AS title, observed_ts AS ts",
                    "FROM captures",
                    "WHERE",
                    "regexp_matches("
                    "COALESCE(title, '') || chr(10) || COALESCE(text, ''), $pat)",
                    "AND observed_ts >= $start AND observed_ts <= $end",
                    "AND parent_doc_or_dup_group IS NOT NULL",
                    "AND TRIM(CAST(parent_doc_or_dup_group AS VARCHAR)) != ''",
                    f"AND {_NULL_DOMAIN_FILTER}",
                    "ORDER BY observed_ts DESC NULLS LAST",
                    "LIMIT $max_rows",
                ]
            ),
            {
                "pat": _word_pattern(term),
                "start": start_dt,
                "end": end_dt,
                "max_rows": MAX_DOCS_SCAN,
            },
        )
        by_group: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grp = row.get("grp")
            if grp is None:
                continue
            by_group.setdefault(str(grp), []).append(row)

        clusters: list[dict[str, Any]] = []
        for grp in sorted(by_group):
            members = sorted(
                by_group[grp],
                key=lambda m: (m["ts"] is None, m["ts"], str(m["domain"])),
            )
            if len(members) < 2:
                continue
            origin = members[0]
            origin_domain = str(origin["domain"])
            replicas: list[Replica] = []
            seen_domains: set[str] = set()
            for member in members[1:]:
                domain = str(member["domain"])
                if domain == origin_domain or domain in seen_domains:
                    continue
                seen_domains.add(domain)
                replicas.append(Replica(domain=domain, first_ts=member["ts"]))
            lead_minutes = 0
            if replicas:
                lead_seconds = (replicas[0].first_ts - origin["ts"]).total_seconds()
                lead_minutes = max(0, int(lead_seconds // 60))
            clusters.append(
                {
                    "origin_domain": origin_domain,
                    "origin_url": origin.get("url"),
                    "origin_title": origin.get("title"),
                    "origin_ts": origin["ts"],
                    "replicas": replicas,
                    "lead_minutes": lead_minutes,
                }
            )
        return clusters
