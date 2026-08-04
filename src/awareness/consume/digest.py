"""Weekly digest generator over the captures lake.

A :class:`Digest` summarizes a rolling capture window (default 7 days):
totals, domains first seen in the window, top domains, token-level top terms,
title-bigram "entities", recent headlines, language breakdown, and a growth
rate versus the previous window of equal length.

All ordering is deterministic (count DESC with lexical tie-breaks) so two
runs over the same corpus produce byte-identical digests. Term/entity
counting runs over a *bounded deterministic sample* of the window (the most
recent N captures by timestamp, tie-broken by ``capture_id``) so a very hot
7-day window cannot balloon memory.
"""

from __future__ import annotations

import itertools
import logging
import re
from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from awareness.obs.logging import get_logger
from awareness.storage.duckdb_index import DuckDbIndex
from awareness.util.lang import PRIMARY_LANGUAGE_SQL
from awareness.util.timeutil import utcnow

logger = get_logger("consume.digest")

MAX_DIGEST_DAYS = 365
_TERM_SAMPLE_LIMIT = 2000
_TERM_CHARS_PER_TEXT = 2000
TOP_N_DOMAINS = 10
TOP_N_TERMS = 20
TOP_N_ENTITIES = 20
TOP_N_LANGUAGES = 20
SAMPLE_TITLES = 10

# Semantic capture timestamp (observed time, falling back to fetch time).
_CAPTURE_TS_SQL = "COALESCE(observed_ts, fetch_ts)"

# Letters-only tokens (unicode-aware); digits/underscores excluded.
_TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

_STOPWORDS: frozenset[str] = frozenset(
    """
    a about above after again against all am an and any are as at be because
    been before being below between both but by can did do does doing down
    during each few for from further had has have having he her here hers
    herself him himself his how i if in into is it its itself just me more
    most my myself no nor not now of off on once only or other our ours
    ourselves out over own same she should so some such than that the their
    theirs them themselves then there these they this those through to too
    under until up very was we were what when where which while who whom why
    will with would you your yours yourself yourselves
    """.split()
)

# ── SQL fragments (module-level; interpolate ONLY code-owned constants, all
# user input flows through bound $params — see the S608 noqas below) ────────
_SQL_COUNT_WINDOW = (
    f"SELECT COUNT(*) AS n FROM captures WHERE {_CAPTURE_TS_SQL} >= $start "  # noqa: S608 -- code-owned constants only
    f"AND {_CAPTURE_TS_SQL} <= $end"
)
_SQL_COUNT_PREVIOUS = (
    f"SELECT COUNT(*) AS n FROM captures WHERE {_CAPTURE_TS_SQL} >= $start "  # noqa: S608 -- code-owned constants only
    f"AND {_CAPTURE_TS_SQL} < $end"
)
_SQL_TERM_SAMPLE = (
    f"SELECT title, text FROM captures WHERE {_CAPTURE_TS_SQL} >= $start "  # noqa: S608 -- code-owned constants only
    f"AND {_CAPTURE_TS_SQL} <= $end "
    f"ORDER BY {_CAPTURE_TS_SQL} DESC, capture_id ASC LIMIT $sample"
)
_SQL_NEW_DOMAINS = (
    "SELECT domain FROM captures "  # noqa: S608 -- code-owned constants only
    "WHERE domain IS NOT NULL AND TRIM(CAST(domain AS VARCHAR)) != '' "
    "GROUP BY domain "
    f"HAVING MIN({_CAPTURE_TS_SQL}) >= $start AND MIN({_CAPTURE_TS_SQL}) <= $end "
    "ORDER BY domain ASC"
)
_SQL_TOP_DOMAINS = (
    "SELECT domain, COUNT(*) AS n FROM captures "  # noqa: S608 -- code-owned constants only
    f"WHERE {_CAPTURE_TS_SQL} >= $start AND {_CAPTURE_TS_SQL} <= $end "
    "AND domain IS NOT NULL AND TRIM(CAST(domain AS VARCHAR)) != '' "
    "GROUP BY domain ORDER BY n DESC, domain ASC LIMIT $n"
)
_SQL_LANGUAGES = (
    f"SELECT {PRIMARY_LANGUAGE_SQL} AS language, COUNT(*) AS n FROM captures "  # noqa: S608 -- code-owned constants only
    f"WHERE {_CAPTURE_TS_SQL} >= $start AND {_CAPTURE_TS_SQL} <= $end "
    "AND language IS NOT NULL AND TRIM(CAST(language AS VARCHAR)) != '' "
    "GROUP BY 1 ORDER BY n DESC, language ASC LIMIT $n"
)
_SQL_SAMPLE_TITLES = (
    "SELECT title FROM captures "  # noqa: S608 -- code-owned constants only
    f"WHERE {_CAPTURE_TS_SQL} >= $start AND {_CAPTURE_TS_SQL} <= $end "
    "AND title IS NOT NULL AND TRIM(CAST(title AS VARCHAR)) != '' "
    f"ORDER BY {_CAPTURE_TS_SQL} DESC, capture_id ASC LIMIT $n"
)


class _TermCount(BaseModel):
    term: str
    count: int


class Digest(BaseModel):
    """Snapshot of a capture window for reporting / dashboards."""

    title: str
    days: int
    window_start: datetime
    window_end: datetime
    previous_window_start: datetime
    previous_window_end: datetime
    generated_at: datetime
    total_captures: int = 0
    previous_captures: int = 0
    growth_rate: float | None = None
    new_domains: list[str] = Field(default_factory=list)
    top_domains: list[_TermCount] = Field(default_factory=list)
    top_terms: list[_TermCount] = Field(default_factory=list)
    top_entities: list[_TermCount] = Field(default_factory=list)
    sample_titles: list[str] = Field(default_factory=list)
    languages: list[_TermCount] = Field(default_factory=list)
    gdelt_note: str | None = None


def _tokenize(text: str) -> Iterable[str]:
    """Yield lowercase letter tokens (length >= 3, non-stopword)."""
    for match in _TOKEN_RE.finditer(text.lower()):
        word = match.group()
        if len(word) >= 3 and word not in _STOPWORDS:
            yield word


def _title_bigrams(title: str) -> Iterable[str]:
    """Yield stopword-filtered consecutive title-word pairs ("entity"-ish)."""
    tokens = list(_tokenize(title))
    for pair in itertools.pairwise(tokens):
        yield f"{pair[0]} {pair[1]}"


def _top_counts(counter: Counter[str], n: int) -> list[_TermCount]:
    """Top *n* counts, deterministic: count DESC, term ASC."""
    return [
        _TermCount(term=term, count=count)
        for term, count in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
    ]


def _window_counts(
    index: DuckDbIndex,
    start: datetime,
    end: datetime,
) -> tuple[int, list[dict[str, Any]]]:
    """Total captures in ``[start, end]`` plus the bounded term sample."""
    total_rows = index.execute(_SQL_COUNT_WINDOW, {"start": start, "end": end})
    total = int(total_rows[0]["n"]) if total_rows else 0
    sample = index.execute(
        _SQL_TERM_SAMPLE,
        {"start": start, "end": end, "sample": _TERM_SAMPLE_LIMIT},
    )
    return total, sample


def _count_terms(sample: list[dict[str, Any]]) -> tuple[Counter[str], Counter[str]]:
    """Token counters (terms + title bigrams) over the bounded sample."""
    term_counts: Counter[str] = Counter()
    bigram_counts: Counter[str] = Counter()
    for row in sample:
        title = str(row.get("title") or "")
        text = str(row.get("text") or "")[:_TERM_CHARS_PER_TEXT]
        term_counts.update(_tokenize(f"{title} {text}"))
        bigram_counts.update(_title_bigrams(title))
    return term_counts, bigram_counts


def _gdelt_summary(index: DuckDbIndex, days: int, term: str) -> str | None:
    """Best-effort one-line GDELT context for *term* (None on any failure).

    The GDELT bridge is imported lazily so this module stays importable
    without httpx/numpy. The digest window is clamped to the bridge's own
    ``MAX_WINDOW_DAYS`` cap. Any failure (import error, network, validation)
    degrades to ``None`` — GDELT context must never fail the digest.

    The bridge's own ``gdeltx.engine`` logger is silenced for the call: this
    is background enrichment, the digest reports GDELT availability itself
    (``gdelt_note``), and the engine's warnings can destabilize callers that
    mix CLI output with logging (see the CLI digest tests).
    """
    engine_logger = logging.getLogger("gdeltx.engine")
    prev_level = engine_logger.level
    engine_logger.setLevel(logging.CRITICAL)
    try:
        from awareness.gdeltx.engine import MAX_WINDOW_DAYS, GdeltBridge  # noqa: PLC0415

        bridge = GdeltBridge(index)
        comparison = bridge.compare_with_local(term, window_days=min(days, MAX_WINDOW_DAYS))
    except Exception as exc:  # best-effort; never fail the digest
        logger.debug("digest_gdelt_unavailable", term=term, err=str(exc))
        return None
    finally:
        engine_logger.setLevel(prev_level)
    return (
        f"GDELT: {comparison.term} local {comparison.local_count} "
        f"vs external {comparison.gdelt_count} (r={comparison.correlation_r:.2f})"
    )


def generate_digest(
    index: DuckDbIndex,
    days: int = 7,
    title: str | None = None,
    *,
    include_gdelt: bool = True,
) -> Digest:
    """Compute a digest over the last *days* of captures.

    The current window is ``[now - days, now]``; the previous window is the
    equal-length period immediately before it. ``growth_rate`` is
    ``this / previous - 1`` and ``None`` when there is no previous data.

    When *include_gdelt* is true (default) and the window has top terms, the
    digest carries a one-line ``gdelt_note`` comparing local volume with
    external GDELT volume for the top term. The bridge call is best-effort:
    GDELT being offline (or unavailable) only clears the note — the digest
    itself never fails and never blocks on the bridge beyond its own
    internal 10s timeout. Callers that must stay offline/snappy (the API
    digest endpoint) pass ``include_gdelt=False``.

    An empty corpus yields a digest with zeros and empty lists (never raises).
    """
    bounded_days = max(1, min(int(days), MAX_DIGEST_DAYS))
    now = utcnow()
    window_start = now - timedelta(days=bounded_days)
    prev_start = window_start - timedelta(days=bounded_days)

    total, sample = _window_counts(index, window_start, now)
    prev_rows = index.execute(
        _SQL_COUNT_PREVIOUS,
        {"start": prev_start, "end": window_start},
    )
    previous = int(prev_rows[0]["n"]) if prev_rows else 0
    growth = (total / previous - 1.0) if previous > 0 else None

    # Domains whose FIRST capture falls inside the current window.
    new_domain_rows = index.execute(
        _SQL_NEW_DOMAINS,
        {"start": window_start, "end": now},
    )
    new_domains = [str(r["domain"]) for r in new_domain_rows]

    top_domain_rows = index.execute(
        _SQL_TOP_DOMAINS,
        {"start": window_start, "end": now, "n": TOP_N_DOMAINS},
    )
    top_domains = [_TermCount(term=str(r["domain"]), count=int(r["n"])) for r in top_domain_rows]

    language_rows = index.execute(
        _SQL_LANGUAGES,
        {"start": window_start, "end": now, "n": TOP_N_LANGUAGES},
    )
    languages = [_TermCount(term=str(r["language"]), count=int(r["n"])) for r in language_rows]

    title_rows = index.execute(
        _SQL_SAMPLE_TITLES,
        {"start": window_start, "end": now, "n": SAMPLE_TITLES},
    )
    sample_titles = [str(r["title"]) for r in title_rows]

    term_counts, bigram_counts = _count_terms(sample)

    digest = Digest(
        title=title or f"Weekly Digest ({bounded_days}d)",
        days=bounded_days,
        window_start=window_start,
        window_end=now,
        previous_window_start=prev_start,
        previous_window_end=window_start,
        generated_at=now,
        total_captures=total,
        previous_captures=previous,
        growth_rate=growth,
        new_domains=new_domains,
        top_domains=top_domains,
        top_terms=_top_counts(term_counts, TOP_N_TERMS),
        top_entities=_top_counts(bigram_counts, TOP_N_ENTITIES),
        sample_titles=sample_titles,
        languages=languages,
    )
    if include_gdelt and digest.top_terms:
        digest.gdelt_note = _gdelt_summary(index, bounded_days, digest.top_terms[0].term)
    logger.info(
        "digest_generated",
        title=digest.title,
        days=bounded_days,
        total_captures=total,
        previous_captures=previous,
        growth_rate=growth,
        gdelt_note=digest.gdelt_note,
    )
    return digest


def _pct(value: float) -> str:
    """Format a ratio as a signed percentage string."""
    return f"{value * 100:+.1f}%"


def _metrics_section(digest: Digest) -> list[str]:
    """At-a-glance metrics table."""
    lines = ["## At a glance", "", "| Metric | Value |", "| --- | --- |"]
    lines.append(f"| Captures (this window) | {digest.total_captures} |")
    lines.append(f"| Captures (previous window) | {digest.previous_captures} |")
    if digest.growth_rate is None:
        lines.append("| Growth vs previous window | n/a (no previous data) |")
    else:
        lines.append(f"| Growth vs previous window | {_pct(digest.growth_rate)} |")
    lines.append(f"| New domains first seen | {len(digest.new_domains)} |")
    languages = ", ".join(item.term for item in digest.languages) or "—"
    lines.append(f"| Languages | {languages} |")
    return lines


def _counts_section(heading: str, items: list[_TermCount]) -> list[str]:
    """A top-N list section (terms / entities)."""
    lines = [heading, ""]
    if items:
        pairs = ", ".join(f"{item.term} ({item.count})" for item in items)
        lines.append(pairs)
    else:
        lines.append("_No data in window._")
    return lines


def _headlines_section(digest: Digest) -> list[str]:
    """Recent headline bullets."""
    lines = ["## Headlines", ""]
    if digest.sample_titles:
        lines.extend(f"- {title}" for title in digest.sample_titles)
    else:
        lines.append("_No headlines in window._")
    return lines


def _growth_note(digest: Digest) -> list[str]:
    """Human note on the growth rate direction."""
    lines = ["## Notes on growth", ""]
    if digest.growth_rate is None:
        lines.append(
            f"No captures were recorded in the previous {digest.days}-day window, "
            "so growth cannot be computed. This is the first period on record."
        )
    elif digest.growth_rate > 0:
        lines.append(
            f"Capture volume grew {_pct(digest.growth_rate)} versus the previous "
            f"{digest.days}-day window, suggesting increased observation activity."
        )
    elif digest.growth_rate < 0:
        lines.append(
            f"Capture volume shrank {_pct(digest.growth_rate)} versus the previous {digest.days}-day window."
        )
    else:
        lines.append(f"Capture volume was flat versus the previous {digest.days}-day window.")
    if digest.gdelt_note:
        lines.append("")
        lines.append(digest.gdelt_note)
    return lines


def render_digest_markdown(digest: Digest) -> str:
    """Render a :class:`Digest` as a clean, professional markdown report."""
    lines: list[str] = [f"# {digest.title}", ""]
    lines.append(
        f"_Generated {digest.generated_at.strftime('%Y-%m-%d %H:%M UTC')} · "
        f"window {digest.window_start.strftime('%Y-%m-%d %H:%M')} → "
        f"{digest.window_end.strftime('%Y-%m-%d %H:%M')} UTC_"
    )
    lines.append("")
    lines.extend(_metrics_section(digest))
    lines.append("")

    lines.append("## Top domains")
    lines.append("")
    if digest.top_domains:
        for i, item in enumerate(digest.top_domains, start=1):
            lines.append(f"{i}. {item.term} — {item.count} captures")
    else:
        lines.append("_No captures in window._")
    lines.append("")

    if digest.new_domains:
        lines.append("### Newly seen domains")
        lines.append("")
        lines.append(", ".join(digest.new_domains))
        lines.append("")

    lines.extend(_counts_section("## Top terms", digest.top_terms))
    lines.append("")
    lines.extend(_counts_section("## Top title entities", digest.top_entities))
    lines.append("")
    lines.extend(_headlines_section(digest))
    lines.append("")
    lines.extend(_growth_note(digest))
    lines.append("")
    return "\n".join(lines)
