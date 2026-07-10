from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from awareness.xscraper.models import SearchRequest, TweetRecord
from awareness.xscraper.store import SessionStore


@pytest.mark.asyncio
async def test_store_deduplicates_tweets_and_records_events(tmp_project) -> None:
    store = SessionStore(tmp_project / "data" / "xscraper.sqlite")
    await store.open()
    await store.init()

    req = SearchRequest(keywords=["ai"], accounts=["openai"], lookback="1h")
    session = await store.create_session(req, query='("ai") (from:openai) -is:retweet -is:reply')

    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    tweet = TweetRecord(
        tweet_id="123",
        session_id=session.session_id,
        author_id="42",
        username="openai",
        display_name="OpenAI",
        text="AI safety is important.",
        created_at=now - timedelta(minutes=5),
        fetched_at=now,
        url="https://x.com/openai/status/123",
        source="backfill",
        query=session.query,
        raw={"id": "123"},
    )

    inserted = await store.store_tweets(session.session_id, [tweet, tweet.model_copy(update={"fetched_at": now + timedelta(seconds=1)})])
    assert inserted == 1

    tweets = await store.list_tweets(session.session_id)
    assert len(tweets) == 1
    assert tweets[0].tweet_id == "123"

    events = await store.list_events(session.session_id)
    assert events[0].type == "session.started"
    assert any(event.type == "tweet" for event in events)

    await store.close()
