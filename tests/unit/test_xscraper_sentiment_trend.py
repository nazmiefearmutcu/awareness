"""Unit tests for the per-day sentiment trend in analyze_session."""

from __future__ import annotations

from datetime import UTC, datetime

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


async def _create_session(store: SessionStore, keywords: list[str], lookback: str = "1h"):
    request = SearchRequest(keywords=keywords, lookback=lookback, language="en")
    query = build_search_query(keywords=request.keywords)
    return await store.create_session(request, query)


@pytest.mark.asyncio
async def test_trend_pos_neg_sums_match_aggregate_sentiment(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        session = await _create_session(store, ["bitcoin"])
        await simulate_session(store, session.session_id, n_tweets=50, seed=7)

        analysis = await analyze_session(store, session.session_id)
        trend = analysis["sentiment_trend"]
        assert trend, "a simulated session should produce trend entries"

        sentiment = analysis["sentiment"]
        assert sum(day["pos"] for day in trend) == sentiment["positive"]
        assert sum(day["neg"] for day in trend) == sentiment["negative"]
        assert sum(day["pos"] + day["neg"] for day in trend) + sentiment["neutral"] == analysis["tweet_count"]
        assert len(trend) == len(analysis["timeline"])
        assert sum(day["count"] for day in analysis["timeline"]) == analysis["tweet_count"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_trend_dates_are_utc_days(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        session = await _create_session(store, ["eth"], lookback="2h")
        await simulate_session(store, session.session_id, n_tweets=20, seed=3)

        analysis = await analyze_session(store, session.session_id)
        tweets = await store.list_tweets(session.session_id, limit=500)
        expected_dates = {t.created_at.astimezone(UTC).strftime("%Y-%m-%d") for t in tweets}
        assert {day["date"] for day in analysis["sentiment_trend"]} == expected_dates
        for day in analysis["sentiment_trend"]:
            parsed = datetime.strptime(day["date"], "%Y-%m-%d")
            assert parsed is not None
            assert -1.0 <= day["avg_score"] <= 1.0
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_empty_session_has_empty_trend(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        session = await _create_session(store, ["bitcoin"])
        analysis = await analyze_session(store, session.session_id)
        assert analysis["sentiment_trend"] == []
        assert analysis["timeline"] == []
    finally:
        await store.close()
