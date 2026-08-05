"""Pydantic models for the qualityx subsystem (time-series corpus quality).

Response models mirror :class:`~awareness.qualityx.engine.QualityTimeEngine`
outputs 1:1 so the router can expose them as typed FastAPI responses.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class QualityPoint(BaseModel):
    """One calendar bucket of corpus-quality aggregates.

    ``duplicate_ratio`` is the fraction of the bucket's docs whose
    ``content_hash`` is shared by at least one other doc captured in the same
    bucket; ``near_duplicate_ratio`` is the same bucket-scoped fraction of
    non-root dup-group members (``parent_doc_or_dup_group != doc_id`` with a
    sibling in the bucket); ``new_domains`` counts domains whose first-ever
    capture falls in the bucket; ``capture_rate`` is the bucket's doc count
    divided by the number of calendar days it spans (docs/day).
    """

    ts: date
    total: int
    duplicate_ratio: float
    near_duplicate_ratio: float
    avg_length: float
    new_domains: int
    capture_rate: float


class QualityHistory(BaseModel):
    """Per-bucket corpus-quality series over a trailing window (oldest first)."""

    days: int
    points: list[QualityPoint]
