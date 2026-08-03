"""Thin bridge exposing :mod:`awareness.xscraper` to the HTTP API.

Keeps the (previously orphaned) X-scraper subsystem usable over the wire
without touching the scraper code itself: these helpers validate requests
with the scraper's own pydantic models and delegate to
:class:`awareness.xscraper.store.SessionStore`.

All functions are async because ``SessionStore`` is aiosqlite-backed.
"""

from __future__ import annotations

from typing import Any

from awareness.xscraper.models import SearchRequest, SessionSnapshot, TweetRecord
from awareness.xscraper.query import build_search_query
from awareness.xscraper.store import SessionStore

__all__ = [
    "create_session",
    "get_session",
    "list_session_tweets",
    "list_sessions",
]


async def list_sessions(store: SessionStore, limit: int = 50) -> list[SessionSnapshot]:
    """Most recent sessions, newest first (bounded by *limit*)."""
    return await store.list_sessions(limit=limit)


async def get_session(store: SessionStore, session_id: str) -> SessionSnapshot | None:
    """One session snapshot, or ``None`` when unknown."""
    return await store.get_session(session_id)


async def list_session_tweets(
    store: SessionStore,
    session_id: str,
    limit: int = 500,
) -> list[TweetRecord]:
    """Tweets for a session, newest first (bounded by *limit*)."""
    return await store.list_tweets(session_id, limit=limit)


async def create_session(store: SessionStore, request_dict: dict[str, Any]) -> SessionSnapshot:
    """Create a session from a raw request dict.

    Validates through :class:`SearchRequest` (raising
    ``pydantic.ValidationError`` on bad input) and derives the X query string
    via the scraper's own query builder. The store returns the persisted
    ``SessionSnapshot``.
    """
    request = SearchRequest.model_validate(request_dict)
    query = build_search_query(
        keywords=request.keywords,
        accounts=request.accounts,
        raw_query=request.raw_query,
        language=request.language,
        include_retweets=request.include_retweets,
        include_replies=request.include_replies,
    )
    return await store.create_session(request, query)
