"""Pydantic response models for the source-intelligence subsystem.

These models are the API contract for the source-quality / replication
analyses mined from the ``captures`` view (see ``SourceIntelEngine``).
All timestamps are UTC tz-aware datetimes; ``model_dump(mode="json")`` is
used at the router boundary so they serialize as ISO-8601 strings.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class DomainScore(BaseModel):
    """Composite quality ranking of a single domain.

    ``score`` is the composite quality score (see
    ``SourceIntelEngine.domain_rank`` for the formula); the remaining fields
    are the components that feed it so callers can inspect *why* a domain
    ranks where it does.
    """

    domain: str
    score: float
    captures: int
    replication_ratio: float
    avg_length: float
    velocity: float


class ReplicationEdge(BaseModel):
    """A directed "who copies whom" relationship between two domains.

    ``origin`` published the shared content earliest within a dup-group;
    ``replica`` published later copies of it. ``count`` is the number of
    distinct dup-groups (parent_doc_or_dup_group values) in which the pair
    appears together with ``origin`` as the earliest domain. ``sample_urls``
    holds up to two example replica URLs (and the origin URL) for human
    inspection.
    """

    origin: str
    replica: str
    count: int
    sample_urls: list[str]


class DomainFreshness(BaseModel):
    """Recency view of a domain's capture activity.

    ``days_since_last`` doubles as the staleness signal: ``999`` when the
    domain has no observed captures (``last_seen`` is ``None``).
    """

    domain: str
    last_seen: datetime | None
    days_since_last: int
    captures_7d: int
    captures_30d: int


class LanguageShare(BaseModel):
    """Count of captures in one primary BCP-47 language bucket (e.g. ``en``)."""

    language: str
    count: int


class SourceTypeShare(BaseModel):
    """Count of captures attributed to one ingestion source type (e.g. ``rss``)."""

    source_type: str
    count: int


class TermCount(BaseModel):
    """Frequency of one content term within a domain's captured text."""

    term: str
    count: int


class DomainProfile(BaseModel):
    """Aggregate profile of a single domain over all of its captures."""

    domain: str
    total_captures: int
    first_seen: datetime | None
    last_seen: datetime | None
    avg_doc_length: float
    languages: list[LanguageShare]
    top_terms: list[TermCount]
    captures_per_day: float
    source_types: list[SourceTypeShare]
