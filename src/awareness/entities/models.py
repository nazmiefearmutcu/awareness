"""Pydantic models for the entities subsystem."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ExtractedEntity(BaseModel):
    """An entity aggregated across the corpus."""

    text: str = Field(min_length=1, max_length=100)
    kind: str = Field(pattern=r"^(ORG|PERSON|PLACE|TICKER)$")
    count: int = Field(ge=0)


class CoOccurrence(BaseModel):
    """An entity that appears in documents alongside another entity."""

    entity: str = Field(min_length=1, max_length=100)
    kind: str = Field(pattern=r"^(ORG|PERSON|PLACE|TICKER)$")
    count: int = Field(ge=0)


class TimeBucket(BaseModel):
    """Count per time bucket."""

    ts: datetime
    count: int = Field(ge=0)


class CorrelationResult(BaseModel):
    """Pearson correlation between two term series, with best lead-lag."""

    a: str
    b: str
    r: float = Field(ge=-1.0, le=1.0)
    n: int = Field(ge=0)
    best_lag: int
    best_r: float = Field(ge=-1.0, le=1.0)
    series: list[TimeBucket]
