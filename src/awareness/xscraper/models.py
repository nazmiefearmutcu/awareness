"""Pydantic models for the X scraper app."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from awareness.util.timeutil import to_utc


class SearchRequest(BaseModel):
    """Search form submitted by the UI or CLI."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    keywords: list[str] = Field(default_factory=list)
    accounts: list[str] = Field(default_factory=list)
    raw_query: str | None = None
    lookback: str = "1h"
    start_time: datetime | None = None
    end_time: datetime | None = None
    language: str | None = None
    include_retweets: bool = False
    include_replies: bool = False
    similar_accounts: bool = False
    similar_accounts_limit: int = 12
    poll_seconds: float = 15.0
    page_size: int = 100
    backfill_pages: int = 5

    @field_validator("keywords", "accounts", mode="before")
    @classmethod
    def _clean_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            items = value.splitlines()
        else:
            items = list(value)
        cleaned: list[str] = []
        for item in items:
            text = str(item).strip()
            if text:
                cleaned.append(text)
        return cleaned

    @field_validator("title", "raw_query", "language", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("start_time", "end_time", mode="before")
    @classmethod
    def _coerce_utc(cls, value: Any) -> Any:
        if value is None or isinstance(value, str):
            return value
        if isinstance(value, datetime):
            return to_utc(value)
        return value

    @field_validator("similar_accounts_limit")
    @classmethod
    def _limit_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("similar_accounts_limit must be >= 0")
        return value

    @field_validator("poll_seconds")
    @classmethod
    def _poll_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("poll_seconds must be > 0")
        return value

    @field_validator("page_size")
    @classmethod
    def _page_size_bounds(cls, value: int) -> int:
        if not 10 <= value <= 100:
            raise ValueError("page_size must be between 10 and 100")
        return value

    @field_validator("backfill_pages")
    @classmethod
    def _backfill_pages_bounds(cls, value: int) -> int:
        if value < 0:
            raise ValueError("backfill_pages must be >= 0")
        return value

    @model_validator(mode="after")
    def _require_filters(self) -> SearchRequest:
        if not (self.keywords or self.accounts or self.raw_query):
            raise ValueError("Provide at least one keyword, account, or raw query term")
        if self.start_time and self.end_time and self.end_time < self.start_time:
            raise ValueError("end_time must be greater than or equal to start_time")
        return self


class TweetRecord(BaseModel):
    """Normalized tweet row persisted to SQLite and returned to the UI."""

    model_config = ConfigDict(extra="forbid")

    tweet_id: str
    session_id: str
    author_id: str
    username: str
    display_name: str | None = None
    text: str
    created_at: datetime
    fetched_at: datetime
    url: str
    source: Literal["backfill", "stream"]
    query: str
    lang: str | None = None
    metrics: dict[str, int] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("tweet_id", "session_id", "author_id", "username", "text", "url", "query", mode="before")
    @classmethod
    def _strip_required(cls, value: Any) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("field cannot be empty")
        return text

    @field_validator("display_name", "lang", mode="before")
    @classmethod
    def _strip_optional(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("created_at", "fetched_at", mode="before")
    @classmethod
    def _coerce_dt(cls, value: Any) -> Any:
        if value is None or isinstance(value, str):
            return value
        if isinstance(value, datetime):
            return to_utc(value)
        return value


class SessionSnapshot(BaseModel):
    """Public state for a scraper session."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    title: str | None = None
    status: Literal["queued", "backfilling", "streaming", "stopping", "stopped", "failed", "completed"]
    query: str
    created_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    keywords: list[str] = Field(default_factory=list)
    accounts: list[str] = Field(default_factory=list)
    similar_accounts: list[str] = Field(default_factory=list)
    lookback_seconds: int = 0
    backfill_tweets: int = 0
    stream_tweets: int = 0
    duplicates: int = 0
    events_emitted: int = 0
    error: str | None = None

    @field_validator("session_id", "query", mode="before")
    @classmethod
    def _strip_required(cls, value: Any) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("field cannot be empty")
        return text

    @field_validator("title", "error", mode="before")
    @classmethod
    def _strip_optional(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("created_at", "started_at", "ended_at", mode="before")
    @classmethod
    def _coerce_times(cls, value: Any) -> Any:
        if value is None or isinstance(value, str):
            return value
        if isinstance(value, datetime):
            return to_utc(value)
        return value


class SessionEvent(BaseModel):
    """Persistent event log entry for SSE and auditability."""

    model_config = ConfigDict(extra="forbid")

    event_id: int
    session_id: str
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    @field_validator("session_id", "type", mode="before")
    @classmethod
    def _strip_required(cls, value: Any) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("field cannot be empty")
        return text

    @field_validator("created_at", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> Any:
        if value is None or isinstance(value, str):
            return value
        if isinstance(value, datetime):
            return to_utc(value)
        return value
