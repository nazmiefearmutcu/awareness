"""Pydantic models for the breaking-news origin subsystem (responses).

Response models mirror engine outputs 1:1: a story's origin document plus
the chain of replica domains that syndicated it, and per-publisher origin
rankings. All timestamps are UTC tz-aware datetimes; ``model_dump(mode=
"json")`` is used at the router boundary so they serialize as ISO-8601.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class Replica(BaseModel):
    """One domain that syndicated a story after its origin.

    ``first_ts`` is that domain's earliest capture of the story within the
    dedup cluster.
    """

    domain: str = Field(min_length=1)
    first_ts: datetime


class StoryOrigin(BaseModel):
    """A breaking-news cluster and where it first broke.

    The cluster is a ``parent_doc_or_dup_group`` dedup group containing at
    least two docs; the doc with the earliest ``observed_ts`` is the origin.
    ``replicas`` lists the distinct domains (excluding the origin domain)
    that picked the story up, each with its earliest capture time;
    ``lead_minutes`` is the gap between the origin timestamp and the
    earliest replica timestamp (0 when no replicas).
    """

    term: str
    origin_domain: str = Field(min_length=1)
    origin_url: str | None = None
    origin_title: str | None = None
    origin_ts: datetime
    replica_count: int = Field(ge=0)
    replicas: list[Replica]
    lead_minutes: int = Field(ge=0)


class PublisherFirst(BaseModel):
    """How often a publisher was the first to break tracked stories.

    ``origin_count`` is the number of clusters where the domain published
    earliest; ``total_stories`` is the number of clusters the domain
    participated in at all (as origin or replica).
    """

    domain: str = Field(min_length=1)
    origin_count: int = Field(ge=1)
    total_stories: int = Field(ge=0)
