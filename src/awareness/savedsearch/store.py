"""SQLite persistence for user-saved searches.

:class:`SavedSearchStore` mirrors the :class:`~awareness.alerts.store.AlertStore`
conventions: stdlib ``sqlite3`` with WAL journaling, a single writer lock,
explicit commits, and UTC ISO-8601 datetime strings so rows round-trip
through any consumer. The database lives at ``<data_dir>/saved_searches.db``.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from awareness.savedsearch.models import SavedSearch, SavedSearchCreate

_SCHEMA = """
CREATE TABLE IF NOT EXISTS saved_searches (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  query TEXT NOT NULL,
  mode TEXT NOT NULL,
  fields TEXT NOT NULL,
  "limit" INT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  pinned INT NOT NULL
);
"""

_SELECT_SQL = (
    "SELECT id, name, query, mode, fields, \"limit\", created_at, updated_at, pinned "
    "FROM saved_searches"
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _fmt(dt: datetime) -> str:
    """UTC ISO-8601 string for storage (aware, round-trippable)."""
    return dt.astimezone(UTC).isoformat()


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s)


class SavedSearchStore:
    """SQLite-backed store for saved searches."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def create(
        self,
        name: str,
        query: str,
        *,
        mode: str = "auto",
        fields: str = "title,text",
        limit: int = 10,
    ) -> SavedSearch:
        """Persist a new saved search with a fresh uuid4 hex id.

        Input is validated through :class:`SavedSearchCreate`, raising
        :class:`ValueError` on bad values (empty name/query, control
        characters, unknown mode, out-of-range limit).
        """
        payload = SavedSearchCreate(name=name, query=query, mode=mode, fields=fields, limit=limit)
        saved_id = uuid.uuid4().hex
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                "INSERT INTO saved_searches (id, name, query, mode, fields, \"limit\", "
                "created_at, updated_at, pinned) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    saved_id,
                    payload.name,
                    payload.query,
                    payload.mode,
                    payload.fields,
                    payload.limit,
                    _fmt(now),
                    _fmt(now),
                ),
            )
            self._conn.commit()
        created = self.get(saved_id)
        assert created is not None
        return created

    def list(self, pinned_first: bool = True) -> list[SavedSearch]:
        """All saved searches; pinned rows first, then most recently touched."""
        sql = _SELECT_SQL
        if pinned_first:
            sql += " ORDER BY pinned DESC, updated_at DESC, id"
        else:
            sql += " ORDER BY updated_at DESC, id"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [self._row_to_search(r) for r in rows]

    def get(self, saved_id: str) -> SavedSearch | None:
        """Fetch a single saved search by id, or ``None`` when absent."""
        with self._lock:
            row = self._conn.execute(
                _SELECT_SQL + " WHERE id = ?", (saved_id,)
            ).fetchone()
        return self._row_to_search(row) if row is not None else None

    def update(self, saved_id: str, patch: dict[str, Any]) -> SavedSearch:
        """Apply *patch* to an existing saved search; raise KeyError when absent.

        Patch keys are validated against the :class:`SavedSearchCreate`
        surface (raising :class:`ValueError` on unknown keys or bad values);
        ``updated_at`` always bumps.
        """
        existing = self.get(saved_id)
        if existing is None:
            raise KeyError(saved_id)
        allowed = set(SavedSearchCreate.model_fields)
        unknown = set(patch) - allowed
        if unknown:
            raise ValueError(f"unknown saved search fields: {sorted(unknown)}")
        merged = {k: v for k, v in existing.model_dump().items() if k in allowed}
        merged.update(patch)
        validated = SavedSearchCreate(**merged)
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                "UPDATE saved_searches SET name = ?, query = ?, mode = ?, fields = ?, "
                "\"limit\" = ?, updated_at = ? WHERE id = ?",
                (
                    validated.name,
                    validated.query,
                    validated.mode,
                    validated.fields,
                    validated.limit,
                    _fmt(now),
                    saved_id,
                ),
            )
            self._conn.commit()
        updated = self.get(saved_id)
        assert updated is not None
        return updated

    def delete(self, saved_id: str) -> bool:
        """Delete a saved search; return True when a row was removed."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM saved_searches WHERE id = ?", (saved_id,)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def pin(self, saved_id: str, pinned: bool) -> SavedSearch:
        """Set the pin flag; raise KeyError when absent."""
        existing = self.get(saved_id)
        if existing is None:
            raise KeyError(saved_id)
        with self._lock:
            self._conn.execute(
                "UPDATE saved_searches SET pinned = ? WHERE id = ?",
                (int(bool(pinned)), saved_id),
            )
            self._conn.commit()
        updated = self.get(saved_id)
        assert updated is not None
        return updated

    def touch(self, saved_id: str) -> SavedSearch:
        """Bump ``updated_at`` (used for "last run" sorting); raise KeyError
        when absent."""
        existing = self.get(saved_id)
        if existing is None:
            raise KeyError(saved_id)
        with self._lock:
            self._conn.execute(
                "UPDATE saved_searches SET updated_at = ? WHERE id = ?",
                (_fmt(_utcnow()), saved_id),
            )
            self._conn.commit()
        updated = self.get(saved_id)
        assert updated is not None
        return updated

    def close(self) -> None:
        """Commit and close the underlying connection."""
        with self._lock:
            self._conn.commit()
            self._conn.close()

    def _row_to_search(self, row: sqlite3.Row) -> SavedSearch:
        return SavedSearch(
            id=row["id"],
            name=row["name"],
            query=row["query"],
            mode=row["mode"],
            fields=row["fields"],
            limit=int(row["limit"]),
            created_at=_parse(row["created_at"]),
            updated_at=_parse(row["updated_at"]),
            pinned=bool(row["pinned"]),
        )
