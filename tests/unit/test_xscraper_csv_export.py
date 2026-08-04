"""Unit tests for analyze.export_tweets_csv."""

from __future__ import annotations

import csv
from datetime import UTC, datetime

import pytest

from awareness.xscraper.analyze import export_tweets_csv
from awareness.xscraper.models import SearchRequest, TweetRecord
from awareness.xscraper.query import build_search_query
from awareness.xscraper.simulate import simulate_session
from awareness.xscraper.store import SessionStore

_HEADER = ["tweet_id", "created_at", "username", "text", "likes", "retweets", "lang", "source"]


async def _store(tmp_path) -> SessionStore:
    store = SessionStore(tmp_path / "data" / "xscraper.sqlite")
    await store.open()
    await store.init()
    return store


async def _create_session(store: SessionStore, keywords: list[str]):
    request = SearchRequest(keywords=keywords, lookback="1h", language="en")
    query = build_search_query(keywords=request.keywords)
    return await store.create_session(request, query)


def _read_csv(path) -> list[list[str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.reader(fh))


@pytest.mark.asyncio
async def test_export_round_trips_text_with_commas_and_newlines(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        session = await _create_session(store, ["bitcoin"])
        now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
        tricky = TweetRecord(
            tweet_id="tricky-1",
            session_id=session.session_id,
            author_id="42",
            username="openai",
            text='Bullish, "quoted"\nsecond line with, comma',
            created_at=now,
            fetched_at=now,
            url="https://x.com/openai/status/tricky-1",
            source="backfill",
            query=session.query,
            lang="en",
            metrics={"likes": 3, "retweets": 7},
        )
        await store.store_tweets(session.session_id, [tricky])

        out = tmp_path / "out" / "export.csv"
        assert await export_tweets_csv(store, session.session_id, out, limit=500) == 1

        rows = _read_csv(out)
        assert rows[0] == _HEADER
        assert len(rows) == 2
        body = rows[1]
        assert body[0] == "tricky-1"
        assert body[2] == "openai"
        assert body[3] == tricky.text
        assert body[4] == "3"
        assert body[5] == "7"
        assert body[6] == "en"
        assert body[7] == "backfill"
        assert datetime.fromisoformat(body[1]) == tricky.created_at
        assert not out.with_name(out.name + ".tmp").exists()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_export_limit_clamps_rows(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        session = await _create_session(store, ["ai"])
        await simulate_session(store, session.session_id, n_tweets=50, seed=1)

        out = tmp_path / "export.csv"
        assert await export_tweets_csv(store, session.session_id, out, limit=10) == 10
        rows = _read_csv(out)
        assert rows[0] == _HEADER
        assert len(rows) == 11  # header + 10 rows
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_export_empty_session_writes_header_only(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        session = await _create_session(store, ["ai"])
        out = tmp_path / "export.csv"
        assert await export_tweets_csv(store, session.session_id, out) == 0
        assert _read_csv(out) == [_HEADER]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_export_unknown_session_raises(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        with pytest.raises(KeyError, match="not found"):
            await export_tweets_csv(store, "does-not-exist", tmp_path / "export.csv")
        assert not (tmp_path / "export.csv").exists()
    finally:
        await store.close()
