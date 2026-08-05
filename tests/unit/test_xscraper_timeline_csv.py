"""Unit tests for analyze.export_timeline_csv."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from awareness.xscraper.analyze import analyze_session, export_timeline_csv
from awareness.xscraper.models import SearchRequest
from awareness.xscraper.query import build_search_query
from awareness.xscraper.simulate import simulate_session
from awareness.xscraper.store import SessionStore

_HEADER = ["date", "tweet_count", "pos", "neg", "neutral", "avg_score"]


async def _store(tmp_path) -> SessionStore:
    store = SessionStore(tmp_path / "data" / "xscraper.sqlite")
    await store.open()
    await store.init()
    return store


async def _create_session(store: SessionStore, keywords: list[str], lookback: str = "3d"):
    request = SearchRequest(keywords=keywords, lookback=lookback, language="en")
    query = build_search_query(keywords=request.keywords)
    return await store.create_session(request, query)


def _read_csv(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.reader(fh))


@pytest.mark.asyncio
async def test_timeline_rows_match_aggregate_sentiment(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        session = await _create_session(store, ["bitcoin"], lookback="3d")
        await simulate_session(store, session.session_id, n_tweets=50, seed=7)

        analysis = await analyze_session(store, session.session_id)
        assert len(analysis["sentiment_trend"]) > 1, "3d lookback should span several days"

        out = tmp_path / "out" / "timeline.csv"
        assert await export_timeline_csv(store, session.session_id, out) == len(
            analysis["sentiment_trend"]
        )

        rows = _read_csv(out)
        assert rows[0] == _HEADER
        body = rows[1:]
        assert len(body) == len(analysis["sentiment_trend"])

        trend_by_date = {day["date"]: day for day in analysis["sentiment_trend"]}
        assert {row[0] for row in body} == set(trend_by_date)

        total_pos = total_neg = total_neutral = total_count = 0
        for row in body:
            date, count, pos, neg, neutral, avg_score = row
            assert date in trend_by_date
            assert int(count) == trend_by_date[date]["pos"] + trend_by_date[date]["neg"] + int(neutral)
            assert float(avg_score) == trend_by_date[date]["avg_score"]
            total_pos += int(pos)
            total_neg += int(neg)
            total_neutral += int(neutral)
            total_count += int(count)

        sentiment = analysis["sentiment"]
        assert total_pos == sentiment["positive"]
        assert total_neg == sentiment["negative"]
        assert total_neutral == sentiment["neutral"]
        assert total_count == analysis["tweet_count"]
        assert not out.with_name(out.name + ".tmp").exists()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_timeline_limit_clamps_tweets(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        session = await _create_session(store, ["ai"], lookback="3d")
        await simulate_session(store, session.session_id, n_tweets=50, seed=1)

        out = tmp_path / "timeline.csv"
        count = await export_timeline_csv(store, session.session_id, out, limit=10)
        assert count >= 1
        rows = _read_csv(out)
        assert rows[0] == _HEADER
        assert sum(int(row[1]) for row in rows[1:]) == 10
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_timeline_empty_session_writes_header_only(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        session = await _create_session(store, ["ai"])
        out = tmp_path / "timeline.csv"
        assert await export_timeline_csv(store, session.session_id, out) == 0
        assert _read_csv(out) == [_HEADER]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_timeline_atomic_cleanup_on_failure(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        session = await _create_session(store, ["ai"])
        await simulate_session(store, session.session_id, n_tweets=20, seed=2)

        out = tmp_path / "timeline.csv"
        out.mkdir()  # destination is a directory → os.replace fails
        with pytest.raises(OSError):
            await export_timeline_csv(store, session.session_id, out)
        assert not out.with_name(out.name + ".tmp").exists()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_timeline_unknown_session_raises(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        with pytest.raises(KeyError, match="not found"):
            await export_timeline_csv(store, "does-not-exist", tmp_path / "timeline.csv")
        assert not (tmp_path / "timeline.csv").exists()
    finally:
        await store.close()
