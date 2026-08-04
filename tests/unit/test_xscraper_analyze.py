"""Unit tests for awareness.xscraper.analyze.analyze_session."""

from __future__ import annotations

import pytest

from awareness.xscraper.analyze import analyze_session
from awareness.xscraper.models import SearchRequest
from awareness.xscraper.query import build_search_query
from awareness.xscraper.simulate import simulate_session
from awareness.xscraper.store import SessionStore


async def _store(tmp_path) -> SessionStore:
    store = SessionStore(tmp_path / "data" / "xscraper.sqlite")
    await store.open()
    await store.init()
    return store


async def _create_session(store: SessionStore, keywords: list[str]):
    request = SearchRequest(keywords=keywords, lookback="1h", language="en")
    query = build_search_query(keywords=request.keywords)
    return await store.create_session(request, query)


@pytest.mark.asyncio
async def test_analysis_sums_and_keyword_terms(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        session = await _create_session(store, ["bitcoin"])
        await simulate_session(store, session.session_id, n_tweets=30, seed=7)

        analysis = await analyze_session(store, session.session_id)
        assert analysis["session_id"] == session.session_id
        assert analysis["tweet_count"] == 30

        assert any(term["term"] == "bitcoin" for term in analysis["top_terms"])

        sentiment = analysis["sentiment"]
        assert sum(sentiment[k] for k in ("positive", "negative", "neutral")) == 30

        assert sum(day["count"] for day in analysis["timeline"]) == 30

        tweets = await store.list_tweets(session.session_id, limit=500)
        assert analysis["engagement"]["total_likes"] == sum(
            t.metrics.get("likes", 0) for t in tweets
        )
        assert analysis["engagement"]["total_retweets"] == sum(
            t.metrics.get("retweets", 0) for t in tweets
        )
        expected_avg = sum(t.metrics.get("likes", 0) for t in tweets) / len(tweets)
        assert analysis["engagement"]["avg_likes"] == round(expected_avg, 2)

        assert analysis["authors"][0]["username"].startswith("user")
        assert len(analysis["authors"]) == 10  # top-10 authors of 30 distinct users
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_empty_session_returns_zeros(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        session = await _create_session(store, ["bitcoin"])
        analysis = await analyze_session(store, session.session_id)
        assert analysis["tweet_count"] == 0
        assert analysis["authors"] == []
        assert analysis["top_terms"] == []
        assert analysis["sentiment"] == {"positive": 0, "negative": 0, "neutral": 0, "avg_score": 0.0}
        assert analysis["timeline"] == []
        assert analysis["engagement"] == {"total_likes": 0, "total_retweets": 0, "avg_likes": 0.0}
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_unknown_session_raises(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        with pytest.raises(KeyError, match="not found"):
            await analyze_session(store, "does-not-exist")
    finally:
        await store.close()
