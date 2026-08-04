"""API tests for /x/sessions/{id}/simulate and /x/sessions/{id}/analysis."""

from __future__ import annotations

import csv
import io

import httpx
import pytest
from fastapi import FastAPI

from awareness.consume.xrouter import close_store
from awareness.consume.xrouter import router as xrouter


@pytest.fixture()
def api_app(tmp_project) -> FastAPI:
    app = FastAPI()
    app.include_router(xrouter)
    return app


@pytest.fixture(autouse=True)
async def _close_x_store() -> None:
    yield
    # aiosqlite keeps a worker thread per open connection; close the X store
    # so the pytest process can exit cleanly.
    await close_store()


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


@pytest.mark.asyncio
async def test_simulate_and_analysis_roundtrip(api_app: FastAPI) -> None:
    async with _client(api_app) as client:
        r = await client.post("/x/sessions", json={"keywords": ["bitcoin"], "title": "btc watch"})
        assert r.status_code == 200, r.text
        session_id = r.json()["session_id"]

        r = await client.post(f"/x/sessions/{session_id}/simulate", json={"n_tweets": 15})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["inserted"] == 15
        assert body["total"] == 15

        r = await client.get(f"/x/sessions/{session_id}/analysis")
        assert r.status_code == 200, r.text
        analysis = r.json()
        assert analysis["tweet_count"] == 15
        assert sum(analysis["sentiment"][k] for k in ("positive", "negative", "neutral")) == 15
        assert any(term["term"] == "bitcoin" for term in analysis["top_terms"])
        assert analysis["engagement"]["total_likes"] > 0


@pytest.mark.asyncio
async def test_simulate_clamps_count(api_app: FastAPI) -> None:
    async with _client(api_app) as client:
        r = await client.post("/x/sessions", json={"keywords": ["ai"]})
        assert r.status_code == 200, r.text
        session_id = r.json()["session_id"]

        r = await client.post(f"/x/sessions/{session_id}/simulate", json={"n_tweets": 5000})
        assert r.status_code == 200, r.text
        assert r.json()["inserted"] == 200

        r = await client.post(f"/x/sessions/{session_id}/simulate", json={"n_tweets": 0})
        assert r.status_code == 200, r.text
        assert r.json()["inserted"] == 1
        assert r.json()["total"] == 201


@pytest.mark.asyncio
async def test_simulate_bad_body_returns_400(api_app: FastAPI) -> None:
    async with _client(api_app) as client:
        r = await client.post("/x/sessions", json={"keywords": ["ai"]})
        assert r.status_code == 200, r.text
        session_id = r.json()["session_id"]

        r = await client.post(f"/x/sessions/{session_id}/simulate", json={"n_tweets": "many"})
        assert r.status_code == 400
        r = await client.post(f"/x/sessions/{session_id}/simulate", json={"n_tweets": True})
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_unknown_session_returns_404(api_app: FastAPI) -> None:
    async with _client(api_app) as client:
        r = await client.post("/x/sessions/does-not-exist/simulate", json={"n_tweets": 5})
        assert r.status_code == 404
        r = await client.get("/x/sessions/does-not-exist/analysis")
        assert r.status_code == 404
        r = await client.get("/x/sessions/does-not-exist/tweets.csv")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_tweets_csv_export_endpoint(api_app: FastAPI) -> None:
    async with _client(api_app) as client:
        r = await client.post("/x/sessions", json={"keywords": ["bitcoin"]})
        assert r.status_code == 200, r.text
        session_id = r.json()["session_id"]

        r = await client.post(f"/x/sessions/{session_id}/simulate", json={"n_tweets": 5})
        assert r.status_code == 200, r.text

        r = await client.get(f"/x/sessions/{session_id}/tweets.csv")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/csv")
        assert r.headers["content-disposition"] == (
            f'attachment; filename="session-{session_id}-tweets.csv"'
        )
        rows = list(csv.reader(io.StringIO(r.text)))
        assert rows[0] == [
            "tweet_id", "created_at", "username", "text", "likes", "retweets", "lang", "source",
        ]
        assert len(rows) == 6  # header + 5 tweets

        r = await client.get(f"/x/sessions/{session_id}/tweets.csv", params={"limit": 2})
        assert r.status_code == 200, r.text
        rows = list(csv.reader(io.StringIO(r.text)))
        assert len(rows) == 3  # header + 2 tweets


@pytest.mark.asyncio
async def test_tweets_csv_empty_session_has_header_only(api_app: FastAPI) -> None:
    async with _client(api_app) as client:
        r = await client.post("/x/sessions", json={"keywords": ["ai"]})
        assert r.status_code == 200, r.text
        session_id = r.json()["session_id"]

        r = await client.get(f"/x/sessions/{session_id}/tweets.csv")
        assert r.status_code == 200, r.text
        rows = list(csv.reader(io.StringIO(r.text)))
        assert rows == [[
            "tweet_id", "created_at", "username", "text", "likes", "retweets", "lang", "source",
        ]]


@pytest.mark.asyncio
async def test_analysis_empty_session_returns_zeros(api_app: FastAPI) -> None:
    async with _client(api_app) as client:
        r = await client.post("/x/sessions", json={"keywords": ["ai"]})
        assert r.status_code == 200, r.text
        session_id = r.json()["session_id"]

        r = await client.get(f"/x/sessions/{session_id}/analysis")
        assert r.status_code == 200, r.text
        analysis = r.json()
        assert analysis["tweet_count"] == 0
        assert analysis["sentiment"] == {"positive": 0, "negative": 0, "neutral": 0, "avg_score": 0.0}
        assert analysis["engagement"] == {"total_likes": 0, "total_retweets": 0, "avg_likes": 0.0}
