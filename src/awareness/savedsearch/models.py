"""Pydantic models for the saved-searches subsystem.

``SavedSearch`` is the persisted row (id + timestamps + pin flag);
``SavedSearchCreate`` carries input validation (name/query length and
control-character checks) so the router can translate validation failures
into deterministic HTTP 400s and the store can reject bad payloads before
any write.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

SearchMode = Literal["auto", "fts", "prefix", "substring"]

MIN_NAME_LEN = 1
MAX_NAME_LEN = 100
MIN_QUERY_LEN = 1
MAX_QUERY_LEN = 500
DEFAULT_FIELDS = "title,text"


class SavedSearch(BaseModel):
    """A persisted, user-bookmarked search query."""

    id: str
    name: str
    query: str
    mode: SearchMode = "auto"
    fields: str = DEFAULT_FIELDS
    limit: int = Field(10, ge=1, le=200)
    created_at: datetime
    updated_at: datetime
    pinned: bool = False


class SavedSearchCreate(BaseModel):
    """Input payload for creating a saved search (no id / timestamps)."""

    name: str = Field(min_length=1, max_length=MAX_NAME_LEN)
    query: str = Field(min_length=1, max_length=MAX_QUERY_LEN)
    mode: SearchMode = "auto"
    fields: str = DEFAULT_FIELDS
    limit: int = Field(10, ge=1, le=200)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: Any) -> str:
        name = (value or "").strip()
        if not MIN_NAME_LEN <= len(name) <= MAX_NAME_LEN:
            raise ValueError(
                f"name must be between {MIN_NAME_LEN} and {MAX_NAME_LEN} characters"
            )
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
            raise ValueError("name must not contain control characters")
        return name

    @field_validator("query")
    @classmethod
    def _validate_query(cls, value: Any) -> str:
        query = (value or "").strip()
        if not MIN_QUERY_LEN <= len(query) <= MAX_QUERY_LEN:
            raise ValueError(
                f"query must be between {MIN_QUERY_LEN} and {MAX_QUERY_LEN} characters"
            )
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in query):
            raise ValueError("query must not contain control characters")
        return query

    @field_validator("fields")
    @classmethod
    def _validate_fields(cls, value: Any) -> str:
        fields = ",".join(f.strip() for f in str(value or "").split(",") if f.strip())
        if not fields:
            raise ValueError("fields must not be empty")
        return fields
