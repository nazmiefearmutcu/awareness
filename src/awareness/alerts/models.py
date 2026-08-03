"""Pydantic models for the alerts subsystem.

Two rule kinds are supported:

* ``term_count`` — fire when the number of captures mentioning a term inside
  the rolling window reaches a threshold.
* ``term_spike`` — fire when the window count exceeds a threshold *and* is at
  least 3x the mean of the previous 7 days of term volume.

Response models mirror engine outputs 1:1; ``AlertRuleCreate`` carries input
validation (term length / control characters) so the router can translate
validation failures into deterministic HTTP 400s.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

RuleKind = Literal["term_count", "term_spike"]

MIN_TERM_LEN = 1
MAX_TERM_LEN = 200


class AlertRule(BaseModel):
    """A persisted alert rule bound to a term and a firing condition."""

    id: str
    name: str
    kind: RuleKind
    term: str
    threshold: float
    window_hours: float
    webhook_url: str | None = None
    cooldown_minutes: float = 30.0
    active: bool = True
    created_at: datetime
    updated_at: datetime


class AlertRuleCreate(BaseModel):
    """Input payload for creating an alert rule (no id / timestamps)."""

    name: str = Field(min_length=1, max_length=200)
    kind: RuleKind
    term: str
    threshold: float = Field(gt=0)
    window_hours: float = Field(24.0, gt=0)
    webhook_url: str | None = None
    cooldown_minutes: float = Field(30.0, ge=0)
    active: bool = True

    @field_validator("term")
    @classmethod
    def _validate_term(cls, value: Any) -> str:
        term = (value or "").strip()
        if not MIN_TERM_LEN <= len(term) <= MAX_TERM_LEN:
            raise ValueError(
                f"term must be between {MIN_TERM_LEN} and {MAX_TERM_LEN} characters"
            )
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in term):
            raise ValueError("term must not contain control characters")
        return term

    @field_validator("webhook_url")
    @classmethod
    def _validate_webhook(cls, value: Any) -> str | None:
        if value is None or not str(value).strip():
            return None
        from awareness.alerts.notify import validate_webhook_url

        return validate_webhook_url(str(value).strip())


class AlertFiring(BaseModel):
    """A single firing event for one rule evaluation."""

    id: int
    rule_id: str
    rule_name: str
    kind: RuleKind
    term: str
    count: int
    threshold: float
    fired_at: datetime
    detail: str


class AlertStatus(BaseModel):
    """High-level alerting subsystem health snapshot."""

    rules_total: int
    rules_active: int
    firings_24h: int
    last_firing: datetime | None = None
