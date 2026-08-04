"""Unit tests for awareness.xscraper.simulate.simulate_session."""

from __future__ import annotations

import pytest

from awareness.xscraper.models import SearchRequest
from awareness.xscraper.query import build_search_query
from awareness.xscraper.simulate import simulate_session
from awareness.xscraper.store import SessionStore


async def _store(tmp_path) -> SessionStore:
    store = SessionStore(tmp_path / "data" / "xscraper.sqlite")
    await store.open()
    await store.init()
    return store


async def _create_session(store: SessionStore, keywords: list[str], accounts: list[str] | None = None):
    request = SearchRequest(keywords=keywords, accounts=accounts or [], lookback="1h", language="en")
    query = build_search_query(keywords=request.keywords, accounts=request.accounts)
    return await store.create_session(request, query)


@pytest.mark.asyncio
async def test_same_seed_yields_identical_texts(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        session = await _create_session(store, ["bitcoin"])
        first = await simulate_session(store, session.session_id, n_tweets=10, seed=42)
        assert first == 10
        texts_before = [t.text for t in await store.list_tweets(session.session_id)]

        second = await simulate_session(store, session.session_id, n_tweets=10, seed=42)
        assert second == 0  # identical ids → PK dedup, no crash
        texts_after = [t.text for t in await store.list_tweets(session.session_id)]
        assert texts_after == texts_before
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_tweets_contain_session_keywords(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        session = await _create_session(store, ["bitcoin", "eth"])
        inserted = await simulate_session(store, session.session_id, n_tweets=30, seed=3)
        assert inserted == 30
        tweets = await store.list_tweets(session.session_id, limit=500)
        assert len(tweets) == 30
        for tweet in tweets:
            assert "bitcoin" in tweet.text or "eth" in tweet.text
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_count_is_clamped(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        session = await _create_session(store, ["ai"])
        assert await simulate_session(store, session.session_id, n_tweets=5000) == 200
        assert await simulate_session(store, session.session_id, n_tweets=0) == 1
        assert len(await store.list_tweets(session.session_id, limit=500)) == 201
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_rerun_with_new_seed_gets_new_ids(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        session = await _create_session(store, ["ai"])
        assert await simulate_session(store, session.session_id, n_tweets=5, seed=11) == 5
        ids_first = {t.tweet_id for t in await store.list_tweets(session.session_id, limit=500)}

        assert await simulate_session(store, session.session_id, n_tweets=5, seed=22) == 5
        all_ids = {t.tweet_id for t in await store.list_tweets(session.session_id, limit=500)}
        ids_second = all_ids - ids_first
        assert len(ids_first) == 5
        assert len(ids_second) == 5
        assert ids_first.isdisjoint(ids_second)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_stored_rows_are_marked_simulated(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        session = await _create_session(store, ["ai"], accounts=["openai"])
        inserted = await simulate_session(store, session.session_id, n_tweets=8, seed=5)
        assert inserted == 8
        tweets = await store.list_tweets(session.session_id)
        assert all(t.source == "simulated" for t in tweets)
        assert all(t.username == "openai" for t in tweets)
        snapshot = await store.get_session(session.session_id)
        assert snapshot is not None
        assert snapshot.stream_tweets == 8
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_unknown_session_raises(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        with pytest.raises(KeyError, match="not found"):
            await simulate_session(store, "does-not-exist", n_tweets=5)
    finally:
        await store.close()
