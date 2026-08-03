"""Pydantic models for the GDELT analytics bridge (requests and responses).

Response models mirror engine outputs 1:1. Request models carry input
validation (term length / control characters, window bounds) so the router
can translate validation failures into deterministic HTTP 400s — the same
pattern as :mod:`awareness.analytics.models`.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from awareness.analytics.models import TimeBucket

# ── response models ──────────────────────────────────────────────────────────


class GdeltWindow(BaseModel):
    """Article count for one day (UTC) reported by GDELT for a term."""

    term: str
    ts: datetime
    count: int
    truncated: bool = False


class GdeltComparison(BaseModel):
    """Local capture volume vs external GDELT volume for one term.

    ``local_series`` / ``gdelt_series`` are aligned per-day ``TimeBucket``
    series over the same window (zero-filled). ``correlation_r`` is the
    Pearson coefficient between them (``0.0`` when it cannot be computed,
    e.g. zero variance or a missing GDELT series — the ``note`` explains).
    """

    term: str
    local_count: int
    gdelt_count: int
    local_series: list[TimeBucket]
    gdelt_series: list[TimeBucket]
    correlation_r: float
    n_days: int
    note: str


class GapReport(BaseModel):
    """Coverage-gap signal for one term.

    ``ratio`` is ``local_count / gdelt_count`` (``0.0`` when GDELT reported
    nothing); ``gap`` is True when GDELT says the story is big (high
    ``gdelt_count``) while local capture is near-zero (``ratio < 0.1``) —
    the "you are missing this story" signal.
    """

    term: str
    local_count: int
    gdelt_count: int
    ratio: float
    gap: bool


# ── request models ───────────────────────────────────────────────────────────

_MAX_TERM_LEN = 80
_MAX_WINDOW_DAYS = 60


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


class CompareRequest(BaseModel):
    """GET /gdelt/compare query parameters."""

    term: str = Field(min_length=1, max_length=_MAX_TERM_LEN)
    window_days: int = Field(14, ge=1, le=_MAX_WINDOW_DAYS)

    @field_validator("term")
    @classmethod
    def _term_no_control_chars(cls, value: str) -> str:
        if _has_control_chars(value):
            raise ValueError("term contains control characters")
        return value


class GapsRequest(BaseModel):
    """GET /gdelt/gaps query parameters."""

    terms: str = Field(min_length=1, max_length=2000)
    window_days: int = Field(7, ge=1, le=_MAX_WINDOW_DAYS)
