"""HTTP surface for the X scraper subsystem.

Wires the previously orphaned :mod:`awareness.xscraper` into the API via
:mod:`awareness.consume.xbridge`. Sessions are persisted in a SQLite store at
``{data_dir}/xscraper.sqlite``; the store is opened lazily on first request
and reused (guarded by an asyncio lock) until the data dir changes.

Endpoints:
    GET  /x/sessions            — list sessions (newest first)
    GET  /x/sessions/{id}       — one session
    GET  /x/sessions/{id}/tweets — tweets for a session
    GET  /x/sessions/{id}/tweets.csv — tweets for a session as an attached CSV
    POST /x/sessions            — create a session from a SearchRequest dict
    POST /x/sessions/{id}/simulate — generate simulated tweets (clamped 1..200)
    GET  /x/sessions/{id}/analysis — aggregated analysis of captured tweets
"""

from __future__ import annotations

import asyncio
import csv
import io
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import ValidationError

from awareness.config import get_settings
from awareness.consume.xbridge import (
    create_session as bridge_create_session,
)
from awareness.consume.xbridge import (
    get_session as bridge_get_session,
)
from awareness.consume.xbridge import (
    list_session_tweets as bridge_list_tweets,
)
from awareness.consume.xbridge import (
    list_sessions as bridge_list_sessions,
)
from awareness.xscraper.analyze import analyze_session
from awareness.xscraper.simulate import simulate_session
from awareness.xscraper.store import SessionStore

router = APIRouter(prefix="/x", tags=["x"])

_store_lock = asyncio.Lock()
_store: SessionStore | None = None
_store_path: Path | None = None


async def _get_store() -> SessionStore:
    """Return the process-wide SessionStore, (re)opening it lazily.

    Rebuilds when the settings data dir changes (e.g. after a config apply or
    in tests where AW_PROJECT_ROOT is swapped), so the store always points at
    the current ``{data_dir}/xscraper.sqlite``.
    """
    global _store, _store_path
    settings = get_settings()
    assert settings.data_dir is not None
    path = settings.data_dir / "xscraper.sqlite"
    if _store is not None and _store_path == path:
        return _store
    async with _store_lock:
        if _store is None or _store_path != path:
            if _store is not None:
                await _store.close()
            store = SessionStore(path)
            await store.open()
            await store.init()
            _store, _store_path = store, path
    return _store


async def close_store() -> None:
    """Close and forget the process-wide store (shutdown hook).

    aiosqlite keeps a background worker thread per open connection; an
    integrator should call this on app shutdown so the process can exit.
    """
    global _store, _store_path  # noqa: PLW0603 -- process-wide singleton holder
    async with _store_lock:
        if _store is not None:
            await _store.close()
        _store = None
        _store_path = None


def _dump_sessions(sessions: list[Any]) -> list[dict[str, Any]]:
    return [s.model_dump(mode="json") for s in sessions]


@router.get("/sessions")
async def list_x_sessions(
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """List X scraper sessions, newest first."""
    store = await _get_store()
    sessions = await bridge_list_sessions(store, limit=limit)
    return {"sessions": _dump_sessions(sessions), "count": len(sessions)}


@router.get("/sessions/{session_id}")
async def get_x_session(session_id: str) -> dict[str, Any]:
    """Return one session snapshot (404 when unknown)."""
    store = await _get_store()
    session = await bridge_get_session(store, session_id)
    if session is None:
        raise HTTPException(404, f"session {session_id!r} not found")
    return session.model_dump(mode="json")


@router.get("/sessions/{session_id}/tweets")
async def list_x_session_tweets(
    session_id: str,
    limit: int = Query(default=500, ge=1, le=5000),
) -> dict[str, Any]:
    """Return tweets captured for a session, newest first."""
    store = await _get_store()
    session = await bridge_get_session(store, session_id)
    if session is None:
        raise HTTPException(404, f"session {session_id!r} not found")
    tweets = await bridge_list_tweets(store, session_id, limit=limit)
    return {
        "session_id": session_id,
        "tweets": [t.model_dump(mode="json") for t in tweets],
        "count": len(tweets),
    }


@router.get("/sessions/{session_id}/tweets.csv")
async def export_x_session_tweets_csv(
    session_id: str,
    limit: int = Query(default=500, ge=1, le=500),
) -> Response:
    """Export a session's tweets as an attached CSV file (404 when unknown)."""
    store = await _get_store()
    session = await bridge_get_session(store, session_id)
    if session is None:
        raise HTTPException(404, f"session {session_id!r} not found")
    tweets = await bridge_list_tweets(store, session_id, limit=limit)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["tweet_id", "created_at", "username", "text", "likes", "retweets", "lang", "source"]
    )
    for tweet in tweets:
        writer.writerow(
            [
                tweet.tweet_id,
                tweet.created_at.isoformat(),
                tweet.username,
                tweet.text,
                tweet.metrics.get("likes", 0),
                tweet.metrics.get("retweets", 0),
                tweet.lang or "",
                tweet.source,
            ]
        )
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="session-{session_id}-tweets.csv"'},
    )


@router.post("/sessions")
async def create_x_session(body: dict[str, Any]) -> dict[str, Any]:
    """Create a session from a SearchRequest dict (400 on validation error)."""
    store = await _get_store()
    try:
        session = await bridge_create_session(store, body)
    except ValidationError as exc:
        raise HTTPException(400, f"invalid search request: {exc.errors()}") from exc
    except ValueError as exc:
        raise HTTPException(400, f"invalid search request: {exc}") from exc
    return session.model_dump(mode="json")


@router.post("/sessions/{session_id}/simulate")
async def simulate_x_session(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Generate simulated tweets for a session (deterministic per seed).

    Body: ``{"n_tweets": 20}`` — clamped to ``1..200``. Returns the number of
    newly inserted rows plus the session's total tweet count.
    """
    store = await _get_store()
    session = await bridge_get_session(store, session_id)
    if session is None:
        raise HTTPException(404, f"session {session_id!r} not found")
    raw = body.get("n_tweets", 20)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise HTTPException(400, "n_tweets must be an integer")
    n_tweets = max(1, min(int(raw), 200))
    inserted = await simulate_session(store, session_id, n_tweets=n_tweets)
    updated = await bridge_get_session(store, session_id)
    total = (updated.backfill_tweets + updated.stream_tweets) if updated is not None else 0
    return {"session_id": session_id, "inserted": inserted, "total": total}


@router.get("/sessions/{session_id}/analysis")
async def analyze_x_session(session_id: str) -> dict[str, Any]:
    """Return aggregated analysis for a session's captured tweets."""
    store = await _get_store()
    session = await bridge_get_session(store, session_id)
    if session is None:
        raise HTTPException(404, f"session {session_id!r} not found")
    return await analyze_session(store, session_id)
