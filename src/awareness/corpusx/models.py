"""Pydantic models for the corpus-quality subsystem (corpusx).

Response models mirror :class:`~awareness.corpusx.engine.CorpusXEngine`
outputs 1:1 so the router can expose them as typed FastAPI responses.
"""

from __future__ import annotations

from pydantic import BaseModel

from awareness.analytics.models import DomainCount


class DomainTermCell(BaseModel):
    """Document count for one (term, domain) pair of the topic matrix."""

    term: str
    domain: str
    count: int


class TopicMatrix(BaseModel):
    """Term x domain matrix: rows = terms, columns = top domains.

    ``cells`` is rectangular — every (term, domain) pair appears, including
    zero counts — so the payload is directly chartable. ``totals`` holds
    ``{"terms": {term: n}, "domains": {domain: n}}`` where domain totals are
    the per-domain corpus volumes used to rank the columns.
    """

    terms: list[str]
    domains: list[str]
    cells: list[DomainTermCell]
    totals: dict[str, dict[str, int]]


class QualitySnapshot(BaseModel):
    """Corpus health metrics over the capture window.

    ``duplicate_ratio`` is the fraction of docs whose ``content_hash`` is
    shared by at least one other capture; ``near_duplicate_ratio`` is the
    fraction of docs that are non-root members of a dup group with at least
    two members; ``languages`` rolls BCP-47 tags up to their primary subtag
    (undetected → ``"unknown"``); ``top_domains`` ranks domains by capture
    count; ``capture_rate_per_day`` is ``total / days spanned`` (0 when the
    corpus spans less than a day).
    """

    total_captures: int
    empty_text: int
    duplicate_ratio: float
    near_duplicate_ratio: float
    avg_length: float
    languages: dict[str, int]
    top_domains: list[DomainCount]
    dedup_group_count: int
    capture_rate_per_day: float
