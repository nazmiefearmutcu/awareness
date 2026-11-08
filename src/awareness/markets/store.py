"""SQLite (WAL) store for the markets layer.

One writer process (``awareness-markets``) and any number of readers. All
timestamps are stored as fixed-width UTC strings ``YYYY-MM-DDTHH:MM:SSZ`` so
lexicographic order equals chronological order. Full records are kept as JSON
in ``doc`` columns; frequently filtered fields are mirrored into columns and
side tables (``news_tag``, ``news_entity_sentiment``) for indexed queries.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from awareness.markets.models import (
    QUALITY_ORDER,
    CorpEvent,
    EconEvent,
    Holiday,
    IndicatorPoint,
    NewsItem,
    ProviderHealth,
    ProviderRef,
    Story,
    ensure_utc,
)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS news (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    publisher_id TEXT NOT NULL,
    publisher_tier INTEGER NOT NULL,
    published_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    quality_label TEXT NOT NULL,
    quality_rank INTEGER NOT NULL,
    quality_score REAL NOT NULL,
    importance REAL NOT NULL,
    content_type TEXT NOT NULL,
    story_id TEXT,
    language TEXT,
    sentiment_score REAL,
    sentiment_label TEXT,
    doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_news_published ON news(published_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS ix_news_story ON news(story_id);
CREATE INDEX IF NOT EXISTS ix_news_publisher ON news(publisher_id, published_at DESC);
CREATE INDEX IF NOT EXISTS ix_news_quality ON news(quality_rank, published_at DESC);
CREATE INDEX IF NOT EXISTS ix_news_importance ON news(importance, published_at DESC);

CREATE TABLE IF NOT EXISTS news_tag (
    news_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (news_id, kind, value)
);
CREATE INDEX IF NOT EXISTS ix_news_tag_kv ON news_tag(kind, value, news_id);

CREATE TABLE IF NOT EXISTS news_entity_sentiment (
    news_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    score REAL NOT NULL,
    label TEXT NOT NULL,
    confidence REAL NOT NULL,
    published_at TEXT NOT NULL,
    quality_score REAL NOT NULL,
    publisher_id TEXT NOT NULL,
    PRIMARY KEY (news_id, entity_id)
);
CREATE INDEX IF NOT EXISTS ix_nes_entity ON news_entity_sentiment(entity_id, published_at);

CREATE VIRTUAL TABLE IF NOT EXISTS news_fts USING fts5(
    news_id UNINDEXED, title, summary, body, tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS stories (
    id TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    last_updated_at TEXT NOT NULL,
    importance REAL NOT NULL,
    corroboration INTEGER NOT NULL,
    doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_stories_updated ON stories(last_updated_at DESC);

CREATE TABLE IF NOT EXISTS econ_events (
    id TEXT PRIMARY KEY,
    country TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    importance INTEGER NOT NULL,
    category TEXT,
    status TEXT NOT NULL,
    doc TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_econ_time ON econ_events(scheduled_at);
CREATE INDEX IF NOT EXISTS ix_econ_country ON econ_events(country, scheduled_at);

CREATE TABLE IF NOT EXISTS corp_events (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    symbol TEXT,
    date TEXT NOT NULL,
    doc TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_corp_date ON corp_events(date);
CREATE INDEX IF NOT EXISTS ix_corp_symbol ON corp_events(symbol, date);
CREATE INDEX IF NOT EXISTS ix_corp_type ON corp_events(type, date);

CREATE TABLE IF NOT EXISTS holidays (
    id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    country TEXT,
    exchange TEXT,
    kind TEXT NOT NULL,
    doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_holidays_date ON holidays(date);

CREATE TABLE IF NOT EXISTS indicators (
    indicator TEXT NOT NULL,
    ts TEXT NOT NULL,
    value REAL NOT NULL,
    label TEXT,
    meta TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (indicator, ts)
);

CREATE TABLE IF NOT EXISTS provider_health (provider TEXT PRIMARY KEY, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS watchlist (symbol TEXT PRIMARY KEY, added_at TEXT NOT NULL);
"""


def ts(value: datetime) -> str:
    """Fixed-width sortable UTC string."""
    dt = ensure_utc(value)
    assert dt is not None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _encode_cursor(published_at: str, item_id: str) -> str:
    return base64.urlsafe_b64encode(f"{published_at}|{item_id}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[str, str] | None:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        published_at, item_id = raw.split("|", 1)
        parse_ts(published_at)
        return published_at, item_id
    except Exception:  # a bad cursor is treated as "no cursor"
        return None


def _fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 query (AND of quoted prefix terms)."""
    terms = []
    for raw in text.replace('"', " ").split():
        term = "".join(ch for ch in raw if ch.isalnum() or ch in "-_.$")
        term = term.strip("-_.$")
        if term:
            terms.append(f'"{term}"*')
    return " AND ".join(terms)


@dataclass
class NewsQuery:
    symbols: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    asset_classes: list[str] = field(default_factory=list)
    publishers: list[str] = field(default_factory=list)
    content_types: list[str] = field(default_factory=list)
    min_quality: str = "standard"
    min_importance: float | None = None
    since: datetime | None = None
    until: datetime | None = None
    q: str | None = None
    story_id: str | None = None
    collapse_stories: bool = False
    limit: int = 50
    cursor: str | None = None


@dataclass
class UpsertResult:
    inserted: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.inserted) + len(self.updated)


@dataclass(frozen=True)
class EntitySentimentRow:
    news_id: str
    entity_id: str
    score: float
    label: str
    confidence: float
    published_at: datetime
    quality_score: float
    publisher_id: str


class MarketsStore:
    """Thread-safe SQLite store. One connection per thread; writes serialised."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._all_conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        self.migrate()

    # ── connection management ─────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
            with self._conns_lock:
                self._all_conns.append(conn)
        return conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def close(self) -> None:
        with self._conns_lock:
            for conn in self._all_conns:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._all_conns.clear()
        self._local = threading.local()

    def migrate(self) -> None:
        with self._write_lock:
            conn = self._conn()
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()

    # ── kv ────────────────────────────────────────────────────────────────

    def kv_get(self, key: str) -> Any | None:
        row = self._conn().execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def kv_set(self, key: str, value: Any) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO kv(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, json.dumps(value, default=str), ts(datetime.now(UTC))),
            )

    # ── news ──────────────────────────────────────────────────────────────

    def news_exists(self, ids: Iterable[str]) -> set[str]:
        id_list = list(dict.fromkeys(ids))
        found: set[str] = set()
        conn = self._conn()
        for chunk_start in range(0, len(id_list), 500):
            chunk = id_list[chunk_start : chunk_start + 500]
            marks = ",".join("?" * len(chunk))
            for row in conn.execute(f"SELECT id FROM news WHERE id IN ({marks})", chunk):
                found.add(row["id"])
        return found

    def upsert_news(self, items: Sequence[NewsItem]) -> UpsertResult:
        """Insert or update items. ``first_seen_at`` of existing rows is preserved."""
        result = UpsertResult()
        if not items:
            return result
        with self._tx() as conn:
            for incoming in items:
                existing = conn.execute(
                    "SELECT first_seen_at FROM news WHERE id = ?", (incoming.id,)
                ).fetchone()
                item = incoming
                if existing is not None:
                    item = incoming.model_copy(update={"first_seen_at": parse_ts(existing["first_seen_at"])})
                doc = item.model_dump_json()
                sentiment_score = item.sentiment.score if item.sentiment else None
                sentiment_label = item.sentiment.label if item.sentiment else None
                conn.execute(
                    """
                    INSERT INTO news(id, url, title, publisher_id, publisher_tier, published_at,
                        first_seen_at, quality_label, quality_rank, quality_score, importance,
                        content_type, story_id, language, sentiment_score, sentiment_label, doc)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                        url=excluded.url, title=excluded.title, publisher_id=excluded.publisher_id,
                        publisher_tier=excluded.publisher_tier, published_at=excluded.published_at,
                        quality_label=excluded.quality_label, quality_rank=excluded.quality_rank,
                        quality_score=excluded.quality_score, importance=excluded.importance,
                        content_type=excluded.content_type, story_id=excluded.story_id,
                        language=excluded.language, sentiment_score=excluded.sentiment_score,
                        sentiment_label=excluded.sentiment_label, doc=excluded.doc
                    """,
                    (
                        item.id,
                        item.url,
                        item.title,
                        item.publisher_id,
                        item.publisher_tier,
                        ts(item.published_at),
                        ts(item.first_seen_at),
                        item.quality_label,
                        QUALITY_ORDER[item.quality_label],
                        float(item.quality_score),
                        float(item.importance),
                        item.content_type,
                        item.story_id,
                        item.language,
                        sentiment_score,
                        sentiment_label,
                        doc,
                    ),
                )
                conn.execute("DELETE FROM news_tag WHERE news_id = ?", (item.id,))
                tags: set[tuple[str, str]] = set()
                tags.update(("symbol", s.upper()) for s in item.symbols)
                tags.update(("country", c.upper()) for c in item.countries)
                tags.update(("topic", t) for t in item.topics)
                tags.update(("asset_class", a) for a in item.asset_classes)
                tags.update(("entity", f"{e.type}:{e.id}") for e in item.entities)
                conn.executemany(
                    "INSERT OR IGNORE INTO news_tag(news_id, kind, value) VALUES (?,?,?)",
                    [(item.id, kind, value) for kind, value in sorted(tags)],
                )
                conn.execute("DELETE FROM news_entity_sentiment WHERE news_id = ?", (item.id,))
                conn.executemany(
                    """INSERT OR REPLACE INTO news_entity_sentiment(news_id, entity_id, score, label,
                        confidence, published_at, quality_score, publisher_id) VALUES (?,?,?,?,?,?,?,?)""",
                    [
                        (
                            item.id,
                            entity_id.upper(),
                            float(s.score),
                            s.label,
                            float(s.confidence),
                            ts(item.published_at),
                            float(item.quality_score),
                            item.publisher_id,
                        )
                        for entity_id, s in item.entity_sentiment.items()
                    ],
                )
                conn.execute("DELETE FROM news_fts WHERE news_id = ?", (item.id,))
                conn.execute(
                    "INSERT INTO news_fts(news_id, title, summary, body) VALUES (?,?,?,?)",
                    (item.id, item.title, item.summary or "", (item.body_text or "")[:20000]),
                )
                (result.updated if existing is not None else result.inserted).append(item.id)
        return result

    def get_news(self, item_id: str) -> NewsItem | None:
        row = self._conn().execute("SELECT doc FROM news WHERE id = ?", (item_id,)).fetchone()
        return NewsItem.model_validate_json(row["doc"]) if row else None

    def get_news_many(self, ids: Sequence[str]) -> list[NewsItem]:
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = self._conn().execute(f"SELECT doc FROM news WHERE id IN ({marks})", list(ids)).fetchall()
        by_id = {}
        for row in rows:
            item = NewsItem.model_validate_json(row["doc"])
            by_id[item.id] = item
        return [by_id[i] for i in ids if i in by_id]

    def query_news(self, q: NewsQuery) -> tuple[list[NewsItem], str | None]:
        where: list[str] = ["n.quality_rank >= ?"]
        params: list[Any] = [QUALITY_ORDER.get(q.min_quality, QUALITY_ORDER["standard"])]
        for kind, values in (
            ("symbol", [v.upper() for v in q.symbols]),
            ("country", [v.upper() for v in q.countries]),
            ("topic", q.topics),
            ("asset_class", q.asset_classes),
        ):
            if values:
                marks = ",".join("?" * len(values))
                where.append(
                    f"n.id IN (SELECT news_id FROM news_tag WHERE kind = ? AND value IN ({marks}))"
                )
                params.extend([kind, *values])
        if q.publishers:
            where.append(f"n.publisher_id IN ({','.join('?' * len(q.publishers))})")
            params.extend(q.publishers)
        if q.content_types:
            where.append(f"n.content_type IN ({','.join('?' * len(q.content_types))})")
            params.extend(q.content_types)
        if q.min_importance is not None:
            where.append("n.importance >= ?")
            params.append(float(q.min_importance))
        if q.since is not None:
            where.append("n.published_at >= ?")
            params.append(ts(q.since))
        if q.until is not None:
            where.append("n.published_at <= ?")
            params.append(ts(q.until))
        if q.story_id:
            where.append("n.story_id = ?")
            params.append(q.story_id)
        if q.q:
            fts = _fts_query(q.q)
            if fts:
                where.append("n.id IN (SELECT news_id FROM news_fts WHERE news_fts MATCH ?)")
                params.append(fts)
        cursor = _decode_cursor(q.cursor) if q.cursor else None
        if cursor is not None:
            where.append("(n.published_at < ? OR (n.published_at = ? AND n.id < ?))")
            params.extend([cursor[0], cursor[0], cursor[1]])
        limit = max(1, min(int(q.limit), 500))
        where_sql = " AND ".join(where)
        if q.collapse_stories:
            sql = f"""
                SELECT doc, published_at, id FROM (
                    SELECT n.doc, n.published_at, n.id,
                        ROW_NUMBER() OVER (
                            PARTITION BY COALESCE(n.story_id, n.id)
                            ORDER BY n.publisher_tier ASC, n.quality_score DESC, n.published_at ASC
                        ) AS rn
                    FROM news n WHERE {where_sql}
                ) WHERE rn = 1
                ORDER BY published_at DESC, id DESC LIMIT ?
            """
        else:
            sql = f"""
                SELECT n.doc, n.published_at, n.id FROM news n WHERE {where_sql}
                ORDER BY n.published_at DESC, n.id DESC LIMIT ?
            """
        rows = self._conn().execute(sql, [*params, limit + 1]).fetchall()
        items = [NewsItem.model_validate_json(r["doc"]) for r in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = _encode_cursor(last["published_at"], last["id"])
        return items, next_cursor

    def recent_news(self, since: datetime, *, min_quality: str = "low", limit: int = 5000) -> list[NewsItem]:
        rows = self._conn().execute(
            "SELECT doc FROM news WHERE published_at >= ? AND quality_rank >= ? "
            "ORDER BY published_at DESC LIMIT ?",
            (ts(since), QUALITY_ORDER[min_quality], limit),
        ).fetchall()
        return [NewsItem.model_validate_json(r["doc"]) for r in rows]

    def entity_sentiment_rows(
        self, entity_id: str, since: datetime, until: datetime | None = None
    ) -> list[EntitySentimentRow]:
        params: list[Any] = [entity_id.upper(), ts(since)]
        sql = (
            "SELECT * FROM news_entity_sentiment WHERE entity_id = ? AND published_at >= ?"
        )
        if until is not None:
            sql += " AND published_at <= ?"
            params.append(ts(until))
        sql += " ORDER BY published_at ASC"
        return [
            EntitySentimentRow(
                news_id=r["news_id"],
                entity_id=r["entity_id"],
                score=r["score"],
                label=r["label"],
                confidence=r["confidence"],
                published_at=parse_ts(r["published_at"]),
                quality_score=r["quality_score"],
                publisher_id=r["publisher_id"],
            )
            for r in self._conn().execute(sql, params).fetchall()
        ]

    def tag_counts(self, kind: str, since: datetime, *, min_quality: str = "standard", limit: int = 50) -> list[tuple[str, int]]:
        rows = self._conn().execute(
            """SELECT t.value AS value, COUNT(*) AS c FROM news_tag t JOIN news n ON n.id = t.news_id
               WHERE t.kind = ? AND n.published_at >= ? AND n.quality_rank >= ?
               GROUP BY t.value ORDER BY c DESC LIMIT ?""",
            (kind, ts(since), QUALITY_ORDER[min_quality], limit),
        ).fetchall()
        return [(r["value"], r["c"]) for r in rows]

    def prune_news(self, older_than: datetime) -> int:
        cutoff = ts(older_than)
        with self._tx() as conn:
            ids = [r["id"] for r in conn.execute("SELECT id FROM news WHERE published_at < ?", (cutoff,))]
            for chunk_start in range(0, len(ids), 500):
                chunk = ids[chunk_start : chunk_start + 500]
                marks = ",".join("?" * len(chunk))
                for table, col in (
                    ("news_tag", "news_id"),
                    ("news_entity_sentiment", "news_id"),
                    ("news_fts", "news_id"),
                    ("news", "id"),
                ):
                    conn.execute(f"DELETE FROM {table} WHERE {col} IN ({marks})", chunk)
            conn.execute("DELETE FROM stories WHERE last_updated_at < ?", (cutoff,))
        return len(ids)

    # ── stories ───────────────────────────────────────────────────────────

    def upsert_stories(self, stories: Sequence[Story]) -> int:
        if not stories:
            return 0
        with self._tx() as conn:
            conn.executemany(
                """INSERT INTO stories(id, first_seen_at, last_updated_at, importance, corroboration, doc)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET last_updated_at=excluded.last_updated_at,
                     importance=excluded.importance, corroboration=excluded.corroboration, doc=excluded.doc""",
                [
                    (
                        s.id,
                        ts(s.first_seen_at),
                        ts(s.last_updated_at),
                        float(s.importance),
                        int(s.corroboration),
                        s.model_dump_json(),
                    )
                    for s in stories
                ],
            )
        return len(stories)

    def delete_orphan_stories(self, since: datetime | None = None) -> int:
        """Delete story rows no news item points at any more (e.g. after a recluster)."""
        sql = "DELETE FROM stories WHERE id NOT IN (SELECT DISTINCT story_id FROM news WHERE story_id IS NOT NULL)"
        params: list[Any] = []
        if since is not None:
            sql += " AND last_updated_at >= ?"
            params.append(ts(since))
        with self._tx() as conn:
            return conn.execute(sql, params).rowcount

    def get_story(self, story_id: str) -> Story | None:
        row = self._conn().execute("SELECT doc FROM stories WHERE id = ?", (story_id,)).fetchone()
        return Story.model_validate_json(row["doc"]) if row else None

    def query_stories(
        self,
        *,
        since: datetime | None = None,
        min_corroboration: int = 1,
        symbols: Sequence[str] = (),
        limit: int = 50,
    ) -> list[Story]:
        sql = "SELECT doc FROM stories WHERE corroboration >= ?"
        params: list[Any] = [int(min_corroboration)]
        if since is not None:
            sql += " AND last_updated_at >= ?"
            params.append(ts(since))
        sql += " ORDER BY last_updated_at DESC LIMIT ?"
        params.append(max(1, min(int(limit) * (4 if symbols else 1), 2000)))
        stories = [Story.model_validate_json(r["doc"]) for r in self._conn().execute(sql, params)]
        if symbols:
            wanted = {s.upper() for s in symbols}
            stories = [s for s in stories if wanted.intersection(x.upper() for x in s.symbols)]
        return stories[:limit]

    # ── economic calendar ─────────────────────────────────────────────────

    def upsert_econ_events(self, events: Sequence[EconEvent]) -> int:
        if not events:
            return 0
        now = datetime.now(UTC)
        with self._tx() as conn:
            conn.executemany(
                """INSERT INTO econ_events(id, country, scheduled_at, importance, category, status, doc, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET country=excluded.country, scheduled_at=excluded.scheduled_at,
                     importance=excluded.importance, category=excluded.category, status=excluded.status,
                     doc=excluded.doc, updated_at=excluded.updated_at""",
                [
                    (
                        e.id,
                        e.country.upper(),
                        ts(e.scheduled_at),
                        int(e.importance),
                        e.category,
                        e.status,
                        e.model_copy(update={"updated_at": e.updated_at or now}).model_dump_json(),
                        ts(e.updated_at or now),
                    )
                    for e in events
                ],
            )
        return len(events)

    def delete_econ_events(self, ids: Sequence[str]) -> int:
        if not ids:
            return 0
        with self._tx() as conn:
            marks = ",".join("?" * len(ids))
            cur = conn.execute(f"DELETE FROM econ_events WHERE id IN ({marks})", list(ids))
            return cur.rowcount

    def query_econ(
        self,
        start: datetime,
        end: datetime,
        *,
        countries: Sequence[str] = (),
        min_importance: int = 1,
        categories: Sequence[str] = (),
        statuses: Sequence[str] = (),
        limit: int = 5000,
    ) -> list[EconEvent]:
        sql = "SELECT doc FROM econ_events WHERE scheduled_at >= ? AND scheduled_at <= ? AND importance >= ?"
        params: list[Any] = [ts(start), ts(end), int(min_importance)]
        if countries:
            sql += f" AND country IN ({','.join('?' * len(countries))})"
            params.extend(c.upper() for c in countries)
        if categories:
            sql += f" AND category IN ({','.join('?' * len(categories))})"
            params.extend(categories)
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            params.extend(statuses)
        sql += " ORDER BY scheduled_at ASC, importance DESC LIMIT ?"
        params.append(limit)
        return [EconEvent.model_validate_json(r["doc"]) for r in self._conn().execute(sql, params)]

    # ── corporate calendar ────────────────────────────────────────────────

    def upsert_corp_events(self, events: Sequence[CorpEvent]) -> int:
        if not events:
            return 0
        now = datetime.now(UTC)
        with self._tx() as conn:
            conn.executemany(
                """INSERT INTO corp_events(id, type, symbol, date, doc, updated_at) VALUES (?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET type=excluded.type, symbol=excluded.symbol,
                     date=excluded.date, doc=excluded.doc, updated_at=excluded.updated_at""",
                [
                    (
                        e.id,
                        e.type,
                        e.symbol.upper() if e.symbol else None,
                        e.date.isoformat(),
                        e.model_copy(update={"updated_at": e.updated_at or now}).model_dump_json(),
                        ts(e.updated_at or now),
                    )
                    for e in events
                ],
            )
        return len(events)

    def query_corp(
        self,
        start: date,
        end: date,
        *,
        types: Sequence[str] = (),
        symbols: Sequence[str] = (),
        limit: int = 5000,
    ) -> list[CorpEvent]:
        sql = "SELECT doc FROM corp_events WHERE date >= ? AND date <= ?"
        params: list[Any] = [start.isoformat(), end.isoformat()]
        if types:
            sql += f" AND type IN ({','.join('?' * len(types))})"
            params.extend(types)
        if symbols:
            sql += f" AND symbol IN ({','.join('?' * len(symbols))})"
            params.extend(s.upper() for s in symbols)
        sql += " ORDER BY date ASC, symbol ASC LIMIT ?"
        params.append(limit)
        return [CorpEvent.model_validate_json(r["doc"]) for r in self._conn().execute(sql, params)]

    def get_corp(self, event_id: str) -> CorpEvent | None:
        row = self._conn().execute("SELECT doc FROM corp_events WHERE id = ?", (event_id,)).fetchone()
        return CorpEvent.model_validate_json(row["doc"]) if row else None

    # ── holidays ──────────────────────────────────────────────────────────

    def upsert_holidays(self, holidays: Sequence[Holiday]) -> int:
        if not holidays:
            return 0
        with self._tx() as conn:
            conn.executemany(
                """INSERT INTO holidays(id, date, country, exchange, kind, doc) VALUES (?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET date=excluded.date, country=excluded.country,
                     exchange=excluded.exchange, kind=excluded.kind, doc=excluded.doc""",
                [
                    (
                        h.id,
                        h.date.isoformat(),
                        h.country.upper() if h.country else None,
                        h.exchange.upper() if h.exchange else None,
                        h.kind,
                        h.model_dump_json(),
                    )
                    for h in holidays
                ],
            )
        return len(holidays)

    def query_holidays(
        self,
        start: date,
        end: date,
        *,
        countries: Sequence[str] = (),
        exchanges: Sequence[str] = (),
    ) -> list[Holiday]:
        sql = "SELECT doc FROM holidays WHERE date >= ? AND date <= ?"
        params: list[Any] = [start.isoformat(), end.isoformat()]
        clauses = []
        if countries:
            clauses.append(f"country IN ({','.join('?' * len(countries))})")
            params.extend(c.upper() for c in countries)
        if exchanges:
            clauses.append(f"exchange IN ({','.join('?' * len(exchanges))})")
            params.extend(x.upper() for x in exchanges)
        if clauses:
            sql += " AND (" + " OR ".join(clauses) + ")"
        sql += " ORDER BY date ASC, exchange ASC, country ASC"
        return [Holiday.model_validate_json(r["doc"]) for r in self._conn().execute(sql, params)]

    # ── indicators ────────────────────────────────────────────────────────

    def upsert_indicator_points(self, points: Sequence[IndicatorPoint]) -> int:
        if not points:
            return 0
        with self._tx() as conn:
            conn.executemany(
                """INSERT INTO indicators(indicator, ts, value, label, meta, source) VALUES (?,?,?,?,?,?)
                   ON CONFLICT(indicator, ts) DO UPDATE SET value=excluded.value, label=excluded.label,
                     meta=excluded.meta, source=excluded.source""",
                [
                    (
                        p.indicator,
                        ts(p.ts),
                        float(p.value),
                        p.label,
                        json.dumps(p.meta, default=str),
                        p.source.model_dump_json(),
                    )
                    for p in points
                ],
            )
        return len(points)

    @staticmethod
    def _point(row: sqlite3.Row) -> IndicatorPoint:
        return IndicatorPoint(
            indicator=row["indicator"],
            ts=parse_ts(row["ts"]),
            value=row["value"],
            label=row["label"],
            meta=json.loads(row["meta"]),
            source=ProviderRef.model_validate_json(row["source"]),
        )

    def query_indicator(
        self,
        indicator: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[IndicatorPoint]:
        sql = "SELECT * FROM indicators WHERE indicator = ?"
        params: list[Any] = [indicator]
        if since is not None:
            sql += " AND ts >= ?"
            params.append(ts(since))
        if until is not None:
            sql += " AND ts <= ?"
            params.append(ts(until))
        sql += " ORDER BY ts ASC"
        rows = self._conn().execute(sql, params).fetchall()
        if limit is not None:
            rows = rows[-limit:]
        return [self._point(r) for r in rows]

    def latest_indicator(self, indicator: str) -> IndicatorPoint | None:
        row = self._conn().execute(
            "SELECT * FROM indicators WHERE indicator = ? ORDER BY ts DESC LIMIT 1", (indicator,)
        ).fetchone()
        return self._point(row) if row else None

    def latest_by_prefix(self, prefix: str) -> list[IndicatorPoint]:
        """Latest point of every indicator whose name starts with ``prefix``."""
        rows = self._conn().execute(
            """SELECT i.* FROM indicators i JOIN (
                   SELECT indicator, MAX(ts) AS mts FROM indicators WHERE indicator LIKE ? ESCAPE '\\'
                   GROUP BY indicator
               ) m ON m.indicator = i.indicator AND m.mts = i.ts ORDER BY i.indicator""",
            (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",),
        ).fetchall()
        return [self._point(r) for r in rows]

    # ── provider health ───────────────────────────────────────────────────

    def get_health(self, provider: str) -> ProviderHealth | None:
        row = self._conn().execute(
            "SELECT doc FROM provider_health WHERE provider = ?", (provider,)
        ).fetchone()
        return ProviderHealth.model_validate_json(row["doc"]) if row else None

    def put_health(self, health: ProviderHealth) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO provider_health(provider, doc) VALUES (?, ?) "
                "ON CONFLICT(provider) DO UPDATE SET doc = excluded.doc",
                (health.provider, health.model_dump_json()),
            )

    def all_health(self) -> list[ProviderHealth]:
        return [
            ProviderHealth.model_validate_json(r["doc"])
            for r in self._conn().execute("SELECT doc FROM provider_health ORDER BY provider")
        ]

    # ── watchlist ─────────────────────────────────────────────────────────

    def get_watchlist(self) -> list[str]:
        return [r["symbol"] for r in self._conn().execute("SELECT symbol FROM watchlist ORDER BY added_at, symbol")]

    def set_watchlist(self, symbols: Iterable[str]) -> list[str]:
        clean = list(dict.fromkeys(s.strip().upper() for s in symbols if s and s.strip()))
        now = ts(datetime.now(UTC))
        with self._tx() as conn:
            conn.execute("DELETE FROM watchlist")
            conn.executemany(
                "INSERT INTO watchlist(symbol, added_at) VALUES (?, ?)", [(s, now) for s in clean]
            )
        return clean

    def ensure_watchlist(self, defaults: Iterable[str]) -> list[str]:
        current = self.get_watchlist()
        return current if current else self.set_watchlist(defaults)

    # ── stats ─────────────────────────────────────────────────────────────

    def counts(self) -> dict[str, int]:
        conn = self._conn()
        out: dict[str, int] = {}
        for table in ("news", "stories", "econ_events", "corp_events", "holidays", "indicators"):
            out[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for label in ("premium", "standard", "low", "rejected"):
            out[f"news_{label}"] = int(
                conn.execute("SELECT COUNT(*) FROM news WHERE quality_label = ?", (label,)).fetchone()[0]
            )
        return out
