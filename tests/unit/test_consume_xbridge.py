"""Tests for the X-scraper bridge (xbridge.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from awareness.consume.xbridge import (
    create_session,
    get_session,
    list_session_tweets,
    list_sessions,
)
from awareness.xscraper.models import TweetRecord
from awareness.xscraper.store import SessionStore


async def _store(tmp_path: Path) -> SessionStore:
    store = SessionStore(tmp_path / "data" / "xscraper.sqlite")
    await store.open()
    await store.init()
    return store


@pytest.mark.asyncio
async def test_create_and_roundtrip_session(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        session = await create_session(
            store,
            {"title": "AI watch", "keywords": ["artificial intelligence", "llm"], "lookback": "2h"},
        )
        assert session.title == "AI watch"
        assert session.status == "queued"
        assert session.keywords == ["artificial intelligence", "llm"]
        assert session.query == '("artificial intelligence" OR llm) -is:retweet -is:reply'

        listed = await list_sessions(store, limit=10)
        assert len(listed) == 1
        assert listed[0].session_id == session.session_id

        fetched = await get_session(store, session.session_id)
        assert fetched is not None
        assert fetched.model_dump() == session.model_dump()

        assert await get_session(store, "does-not-exist") is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_create_session_from_raw_query(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        session = await create_session(
            store,
            {"raw_query": "(bitcoin OR ethereum)", "include_retweets": True},
        )
        assert session.query == "((bitcoin OR ethereum)) -is:reply"
        assert "-is:retweet" not in session.query
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_create_session_validation_error(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        with pytest.raises(ValidationError):
            await create_session(store, {})  # no keywords/accounts/raw_query
        with pytest.raises(ValidationError):
            await create_session(store, {"keywords": ["ai"], "page_size": 5})  # out of bounds
        assert await list_sessions(store) == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_list_session_tweets_roundtrip(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        session = await create_session(store, {"keywords": ["ai"], "accounts": ["openai"]})
        now = datetime.now(tz=UTC)
        tweet = TweetRecord(
            tweet_id="tweet-1",
            session_id=session.session_id,
            author_id="42",
            username="openai",
            display_name="OpenAI",
            text="AI safety is important.",
            created_at=now - timedelta(minutes=5),
            fetched_at=now,
            url="https://x.com/openai/status/tweet-1",
            source="backfill",
            query=session.query,
        )
        await store.store_tweets(session.session_id, [tweet])

        tweets = await list_session_tweets(store, session.session_id, limit=10)
        assert len(tweets) == 1
        assert tweets[0].tweet_id == "tweet-1"
        assert tweets[0].username == "openai"
        assert tweets[0].source == "backfill"

        # Unknown session → empty, no crash.
        assert await list_session_tweets(store, "missing", limit=10) == []
    finally:
        await store.close()
