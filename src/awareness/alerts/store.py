"""SQLite persistence for alert rules and firing history.

:class:`AlertStore` uses the stdlib ``sqlite3`` module with WAL journaling.
All writes happen under a single writer lock and commit explicitly; datetimes
are stored as UTC ISO-8601 strings so rows round-trip through any consumer.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from awareness.alerts.models import AlertRule, AlertRuleCreate

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  term TEXT NOT NULL,
  threshold REAL NOT NULL,
  window_hours REAL NOT NULL,
  webhook_url TEXT,
  webhooks_json TEXT,
  webhook_format TEXT NOT NULL DEFAULT 'json',
  cooldown_minutes REAL NOT NULL,
  active INT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS firings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  rule_id TEXT NOT NULL,
  rule_name TEXT NOT NULL,
  kind TEXT NOT NULL,
  term TEXT NOT NULL,
  count REAL NOT NULL,
  threshold REAL NOT NULL,
  detail TEXT NOT NULL,
  fired_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_firings_rule_id ON firings (rule_id);
CREATE INDEX IF NOT EXISTS idx_firings_fired_at ON firings (fired_at);
"""

_RULE_SELECT_SQL = (
    "SELECT id, name, kind, term, threshold, window_hours, webhook_url, "
    "webhooks_json, webhook_format, cooldown_minutes, active, created_at, "
    "updated_at FROM rules"
)

_FIRING_SELECT_SQL = (
    "SELECT id, rule_id, rule_name, kind, term, count, threshold, detail, "
    "fired_at FROM firings"
)

_MAX_FIRINGS_LIMIT = 500


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _fmt(dt: datetime) -> str:
    """UTC ISO-8601 string for storage (aware, round-trippable)."""
    return dt.astimezone(UTC).isoformat()


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s)


class AlertStore:
    """SQLite-backed store for alert rules and firing history."""

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
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Idempotently upgrade pre-existing ``rules`` tables.

        Older databases predate per-rule webhook lists: ``webhooks_json`` and
        ``webhook_format`` are added via ``ALTER TABLE`` when missing (new
        databases get them straight from ``_SCHEMA``). Old rows keep their
        ``webhook_url`` with a NULL ``webhooks_json`` — row mapping preserves
        it by seeding ``webhooks`` from ``webhook_url``.
        """
        cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(rules)")}
        if "webhooks_json" not in cols:
            self._conn.execute("ALTER TABLE rules ADD COLUMN webhooks_json TEXT")
        if "webhook_format" not in cols:
            self._conn.execute(
                "ALTER TABLE rules ADD COLUMN webhook_format TEXT NOT NULL DEFAULT 'json'"
            )

    # ── rules ────────────────────────────────────────────────────────────

    def create_rule(self, rule: AlertRuleCreate) -> AlertRule:
        """Persist *rule* with a fresh uuid4 hex id; return the stored row."""
        rule_id = uuid.uuid4().hex
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                "INSERT INTO rules (id, name, kind, term, threshold, window_hours, "
                "webhook_url, webhooks_json, webhook_format, cooldown_minutes, "
                "active, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rule_id,
                    rule.name,
                    rule.kind,
                    rule.term,
                    rule.threshold,
                    rule.window_hours,
                    rule.webhook_url,
                    json.dumps(rule.webhooks),
                    rule.webhook_format,
                    rule.cooldown_minutes,
                    int(rule.active),
                    _fmt(now),
                    _fmt(now),
                ),
            )
            self._conn.commit()
        created = self.get_rule(rule_id)
        assert created is not None
        return created

    def get_rule(self, rule_id: str) -> AlertRule | None:
        """Fetch a single rule by id, or ``None`` when absent."""
        with self._lock:
            row = self._conn.execute(
                _RULE_SELECT_SQL + " WHERE id = ?", (rule_id,)
            ).fetchone()
        return self._row_to_rule(row) if row is not None else None

    def list_rules(self, active_only: bool = False) -> list[AlertRule]:
        """All rules (newest first), optionally filtered to active ones."""
        sql = _RULE_SELECT_SQL
        params: tuple[Any, ...] = ()
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY created_at DESC, id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_rule(r) for r in rows]

    def update_rule(self, rule_id: str, patch: dict[str, Any]) -> AlertRule:
        """Apply *patch* to an existing rule; raise KeyError when absent.

        Patch keys are validated against the rule surface; ``term`` / ``kind``
        values are re-validated through :class:`AlertRuleCreate` (raising
        :class:`ValueError` on bad input).
        """
        existing = self.get_rule(rule_id)
        if existing is None:
            raise KeyError(rule_id)
        allowed = set(AlertRuleCreate.model_fields)
        unknown = set(patch) - allowed
        if unknown:
            raise ValueError(f"unknown rule fields: {sorted(unknown)}")
        merged = {k: v for k, v in existing.model_dump().items() if k in allowed}
        merged.update(patch)
        # The legacy ``webhook_url`` mirror column must follow the canonical
        # ``webhooks`` list: a patched list (even an empty one, which clears
        # the mirror) wins over the stale value from the existing row.
        if "webhooks" in patch:
            merged["webhook_url"] = patch["webhooks"][0] if patch["webhooks"] else None
        validated = AlertRuleCreate(**merged)
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                "UPDATE rules SET name = ?, kind = ?, term = ?, threshold = ?, "
                "window_hours = ?, webhook_url = ?, webhooks_json = ?, "
                "webhook_format = ?, cooldown_minutes = ?, active = ?, "
                "updated_at = ? WHERE id = ?",
                (
                    validated.name,
                    validated.kind,
                    validated.term,
                    validated.threshold,
                    validated.window_hours,
                    validated.webhook_url,
                    json.dumps(validated.webhooks),
                    validated.webhook_format,
                    validated.cooldown_minutes,
                    int(validated.active),
                    _fmt(now),
                    rule_id,
                ),
            )
            self._conn.commit()
        updated = self.get_rule(rule_id)
        assert updated is not None
        return updated

    def delete_rule(self, rule_id: str) -> bool:
        """Delete a rule; return True when a row was removed."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
            self._conn.commit()
            return cur.rowcount > 0

    # ── import / export ──────────────────────────────────────────────────

    def export_rules(self) -> list[dict[str, Any]]:
        """All rules as JSON-ready dicts (newest first), including ``webhooks``."""
        with self._lock:
            rows = self._conn.execute(
                _RULE_SELECT_SQL + " ORDER BY created_at DESC, id"
            ).fetchall()
        return [self._row_to_rule(r).model_dump(mode="json") for r in rows]

    def import_rules(
        self, rules: list[dict[str, Any]], replace: bool = False
    ) -> tuple[int, int]:
        """Bulk-create *rules*, deduplicated by name.

        Returns ``(created, skipped)``. Rules whose name already exists are
        skipped unless *replace* is set, in which case the existing rule is
        deleted first and the imported one created fresh. Raises
        :class:`ValueError` (with the offending rule name) on invalid input;
        extra fields such as ``id`` / ``created_at`` from an export dump are
        ignored.
        """
        # Validate EVERY entry before ANY write so a bad rule mid-list cannot
        # leave a partial import behind (all-or-nothing semantics).
        payloads: list[AlertRuleCreate] = []
        for raw in rules:
            try:
                payloads.append(AlertRuleCreate.model_validate(raw))
            except ValidationError as exc:
                name = raw.get("name") if isinstance(raw, dict) else "<unknown>"
                raise ValueError(f"invalid rule {name!r}: {exc}") from exc
        created = 0
        skipped = 0
        for payload in payloads:
            existing = self._get_rule_by_name(payload.name)
            if existing is not None and not replace:
                skipped += 1
                continue
            if existing is not None:
                self.delete_rule(existing.id)
            self.create_rule(payload)
            created += 1
        return created, skipped

    def _get_rule_by_name(self, name: str) -> AlertRule | None:
        with self._lock:
            row = self._conn.execute(
                _RULE_SELECT_SQL + " WHERE name = ?", (name,)
            ).fetchone()
        return self._row_to_rule(row) if row is not None else None

    # ── firings ──────────────────────────────────────────────────────────

    def record_firing(
        self,
        *,
        rule_id: str,
        rule_name: str,
        kind: str,
        term: str,
        count: float,
        threshold: float,
        detail: str,
    ) -> int:
        """Insert a firing row; return the new autoincrement row id."""
        now = _utcnow()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO firings (rule_id, rule_name, kind, term, count, "
                "threshold, detail, fired_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rule_id,
                    rule_name,
                    kind,
                    term,
                    count,
                    threshold,
                    detail,
                    _fmt(now),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def list_firings(
        self, limit: int = 50, since: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Most recent firings (newest first), optionally from *since* onward.

        *limit* is clamped to ``1..500``. Rows are returned as dicts ready for
        :class:`~awareness.alerts.models.AlertFiring` validation.
        """
        limit = min(max(int(limit), 1), _MAX_FIRINGS_LIMIT)
        sql = _FIRING_SELECT_SQL
        params: list[Any] = []
        if since is not None:
            sql += " WHERE fired_at >= ?"
            params.append(_fmt(since))
        sql += " ORDER BY fired_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_firing(r) for r in rows]

    def count_firings_since(self, ts: datetime) -> int:
        """Number of firings with ``fired_at >= *ts*``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT count(*) AS n FROM firings WHERE fired_at >= ?",
                (_fmt(ts),),
            ).fetchone()
        return int(row["n"]) if row is not None else 0

    def last_firing_time(self, rule_id: str) -> datetime | None:
        """Most recent firing time for *rule_id*, or ``None``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT fired_at FROM firings WHERE rule_id = ? "
                "ORDER BY fired_at DESC, id DESC LIMIT 1",
                (rule_id,),
            ).fetchone()
        return _parse(row["fired_at"]) if row is not None else None

    # ── lifecycle ────────────────────────────────────────────────────────

    def close(self) -> None:
        """Commit and close the underlying connection."""
        with self._lock:
            self._conn.commit()
            self._conn.close()

    # ── row mappers ──────────────────────────────────────────────────────

    def _row_to_rule(self, row: sqlite3.Row) -> AlertRule:
        raw_webhooks = row["webhooks_json"]
        webhooks: list[str] = json.loads(raw_webhooks) if raw_webhooks else []
        return AlertRule(
            id=row["id"],
            name=row["name"],
            kind=row["kind"],
            term=row["term"],
            threshold=row["threshold"],
            window_hours=row["window_hours"],
            webhooks=webhooks,
            webhook_url=row["webhook_url"],
            webhook_format=row["webhook_format"] or "json",
            cooldown_minutes=row["cooldown_minutes"],
            active=bool(row["active"]),
            created_at=_parse(row["created_at"]),
            updated_at=_parse(row["updated_at"]),
        )

    def _row_to_firing(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "rule_id": row["rule_id"],
            "rule_name": row["rule_name"],
            "kind": row["kind"],
            "term": row["term"],
            "count": round(float(row["count"])),
            "threshold": float(row["threshold"]),
            "detail": row["detail"],
            "fired_at": _parse(row["fired_at"]),
        }
