"""SQLite-backed session store for the X scraper."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from awareness.util.timeutil import to_utc, utcnow
from awareness.xscraper.models import SearchRequest, SessionEvent, SessionSnapshot, TweetRecord
from awareness.xscraper.query import parse_lookback

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    title TEXT,
    status TEXT NOT NULL,
    query TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    keywords_json TEXT NOT NULL DEFAULT '[]',
    accounts_json TEXT NOT NULL DEFAULT '[]',
    similar_accounts_json TEXT NOT NULL DEFAULT '[]',
    lookback_seconds INTEGER NOT NULL DEFAULT 0,
    backfill_tweets INTEGER NOT NULL DEFAULT 0,
    stream_tweets INTEGER NOT NULL DEFAULT 0,
    duplicates INTEGER NOT NULL DEFAULT 0,
    events_emitted INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    request_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS tweets (
    tweet_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    author_id TEXT NOT NULL,
    username TEXT NOT NULL,
    display_name TEXT,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    url TEXT NOT NULL,
    source TEXT NOT NULL,
    query TEXT NOT NULL,
    lang TEXT,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    raw_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (session_id, tweet_id),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_tweets_session ON tweets(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, event_id);
"""


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    aware = to_utc(dt)
    return aware.isoformat() if aware else None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return to_utc(value)


class SessionStore:
    """Async SQLite store for scraper sessions, tweets, and event logs."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.path))
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA foreign_keys = ON")
        await self._db.execute("PRAGMA journal_mode = WAL")

    async def init(self) -> None:
        db = self._require_db()
        await db.executescript(_SCHEMA)
        await db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("SessionStore is not open; call open() first")
        return self._db

    async def create_session(self, request: SearchRequest, query: str) -> SessionSnapshot:
        db = self._require_db()
        session_id = uuid.uuid4().hex
        now = utcnow()
        try:
            lookback_seconds = int(parse_lookback(request.lookback).total_seconds())
        except ValueError:
            lookback_seconds = 0

        await db.execute(
            """
            INSERT INTO sessions (
                session_id, title, status, query, created_at, started_at,
                keywords_json, accounts_json, similar_accounts_json,
                lookback_seconds, request_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                request.title,
                "queued",
                query,
                _iso(now),
                _iso(now),
                json.dumps(list(request.keywords)),
                json.dumps(list(request.accounts)),
                json.dumps([]),
                lookback_seconds,
                request.model_dump_json(),
            ),
        )
        await self._append_event(
            session_id,
            "session.started",
            {
                "query": query,
                "keywords": list(request.keywords),
                "accounts": list(request.accounts),
            },
            created_at=now,
            commit=False,
        )
        await db.commit()
        snapshot = await self.get_session(session_id)
        assert snapshot is not None
        return snapshot

    async def get_session(self, session_id: str) -> SessionSnapshot | None:
        db = self._require_db()
        cur = await db.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
        row = await cur.fetchone()
        if row is None:
            return None
        return self._row_to_session(row)

    async def list_sessions(self, *, limit: int = 50) -> list[SessionSnapshot]:
        db = self._require_db()
        cur = await db.execute(
            "SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?",
            (max(1, int(limit)),),
        )
        rows = await cur.fetchall()
        return [self._row_to_session(r) for r in rows]

    async def update_session_status(
        self,
        session_id: str,
        status: str,
        *,
        error: str | None = None,
        ended: bool = False,
    ) -> None:
        db = self._require_db()
        ended_at = _iso(utcnow()) if ended else None
        await db.execute(
            """
            UPDATE sessions
            SET status = ?, error = COALESCE(?, error),
                ended_at = COALESCE(?, ended_at)
            WHERE session_id = ?
            """,
            (status, error, ended_at, session_id),
        )
        await db.commit()

    async def store_tweets(self, session_id: str, tweets: list[TweetRecord]) -> int:
        """Insert tweets for a session. Returns count of newly inserted rows."""
        if not tweets:
            return 0
        db = self._require_db()
        inserted = 0
        backfill = 0
        stream = 0
        duplicates = 0

        for tweet in tweets:
            if tweet.session_id != session_id:
                raise ValueError(
                    f"tweet session_id {tweet.session_id!r} does not match {session_id!r}"
                )
            try:
                await db.execute(
                    """
                    INSERT INTO tweets (
                        tweet_id, session_id, author_id, username, display_name,
                        text, created_at, fetched_at, url, source, query, lang,
                        metrics_json, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tweet.tweet_id,
                        tweet.session_id,
                        tweet.author_id,
                        tweet.username,
                        tweet.display_name,
                        tweet.text,
                        _iso(tweet.created_at),
                        _iso(tweet.fetched_at),
                        tweet.url,
                        tweet.source,
                        tweet.query,
                        tweet.lang,
                        json.dumps(tweet.metrics or {}),
                        json.dumps(tweet.raw or {}),
                    ),
                )
            except aiosqlite.IntegrityError:
                duplicates += 1
                continue

            inserted += 1
            if tweet.source == "backfill":
                backfill += 1
            else:
                stream += 1
            await self._append_event(
                session_id,
                "tweet",
                {
                    "tweet_id": tweet.tweet_id,
                    "username": tweet.username,
                    "source": tweet.source,
                },
                created_at=tweet.fetched_at or utcnow(),
                commit=False,
            )

        if inserted or duplicates:
            await db.execute(
                """
                UPDATE sessions
                SET backfill_tweets = backfill_tweets + ?,
                    stream_tweets = stream_tweets + ?,
                    duplicates = duplicates + ?
                WHERE session_id = ?
                """,
                (backfill, stream, duplicates, session_id),
            )
            await db.commit()
        return inserted

    async def list_tweets(self, session_id: str, *, limit: int = 500) -> list[TweetRecord]:
        db = self._require_db()
        cur = await db.execute(
            """
            SELECT * FROM tweets
            WHERE session_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (session_id, max(1, int(limit))),
        )
        rows = await cur.fetchall()
        return [self._row_to_tweet(r) for r in rows]

    async def list_events(self, session_id: str, *, after_id: int = 0, limit: int = 500) -> list[SessionEvent]:
        db = self._require_db()
        cur = await db.execute(
            """
            SELECT * FROM events
            WHERE session_id = ? AND event_id > ?
            ORDER BY event_id ASC
            LIMIT ?
            """,
            (session_id, int(after_id), max(1, int(limit))),
        )
        rows = await cur.fetchall()
        return [self._row_to_event(r) for r in rows]

    async def _append_event(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        created_at: datetime | None = None,
        commit: bool = True,
    ) -> None:
        db = self._require_db()
        when = created_at or utcnow()
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        await db.execute(
            """
            INSERT INTO events (session_id, type, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, event_type, json.dumps(payload or {}), _iso(when)),
        )
        await db.execute(
            "UPDATE sessions SET events_emitted = events_emitted + 1 WHERE session_id = ?",
            (session_id,),
        )
        if commit:
            await db.commit()

    @staticmethod
    def _row_to_session(row: aiosqlite.Row) -> SessionSnapshot:
        return SessionSnapshot(
            session_id=row["session_id"],
            title=row["title"],
            status=row["status"],
            query=row["query"],
            created_at=_parse_dt(row["created_at"]) or utcnow(),
            started_at=_parse_dt(row["started_at"]),
            ended_at=_parse_dt(row["ended_at"]),
            keywords=json.loads(row["keywords_json"] or "[]"),
            accounts=json.loads(row["accounts_json"] or "[]"),
            similar_accounts=json.loads(row["similar_accounts_json"] or "[]"),
            lookback_seconds=int(row["lookback_seconds"] or 0),
            backfill_tweets=int(row["backfill_tweets"] or 0),
            stream_tweets=int(row["stream_tweets"] or 0),
            duplicates=int(row["duplicates"] or 0),
            events_emitted=int(row["events_emitted"] or 0),
            error=row["error"],
        )

    @staticmethod
    def _row_to_tweet(row: aiosqlite.Row) -> TweetRecord:
        return TweetRecord(
            tweet_id=row["tweet_id"],
            session_id=row["session_id"],
            author_id=row["author_id"],
            username=row["username"],
            display_name=row["display_name"],
            text=row["text"],
            created_at=_parse_dt(row["created_at"]) or utcnow(),
            fetched_at=_parse_dt(row["fetched_at"]) or utcnow(),
            url=row["url"],
            source=row["source"],
            query=row["query"],
            lang=row["lang"],
            metrics=json.loads(row["metrics_json"] or "{}"),
            raw=json.loads(row["raw_json"] or "{}"),
        )

    @staticmethod
    def _row_to_event(row: aiosqlite.Row) -> SessionEvent:
        return SessionEvent(
            event_id=int(row["event_id"]),
            session_id=row["session_id"],
            type=row["type"],
            payload=json.loads(row["payload_json"] or "{}"),
            created_at=_parse_dt(row["created_at"]) or utcnow(),
        )
