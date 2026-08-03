"""C-07: non-SQLite (Postgres) migration branch must add dedup_near.sig_hex.

Regression: the non-SQLite init() branch only migrated tail_state.pid and
tasks.next_attempt_at — legacy Postgres databases never got dedup_near.sig_hex,
so _verify_dedup_schema() raised and init failed permanently. The branch must
also use TIMESTAMP WITH TIME ZONE for next_attempt_at and create the retry
index, mirroring the SQLite migration.

We simulate a legacy Postgres database with a SQLite file: tables are created
in their pre-migration shape (no sig_hex, no next_attempt_at) and the
engine's dialect is patched to "postgresql" so init() takes the non-SQLite
migration branch.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from sqlalchemy import inspect, text

from awareness.storage.state import StateDB


def _simulate_legacy_schema(db: StateDB) -> None:
    """Drop the modern columns so the DB looks like it predates the migration."""
    with db._engine.connect() as conn:
        conn.execute(text("DROP INDEX IF EXISTS ix_tasks_next_attempt_at"))
        conn.execute(text("ALTER TABLE tasks DROP COLUMN next_attempt_at"))
        conn.execute(text("ALTER TABLE dedup_near DROP COLUMN sig_hex"))
        conn.commit()


def test_postgres_branch_adds_sig_hex_and_tz_next_attempt(tmp_path: Path) -> None:
    db = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    db.init()  # full modern schema (sqlite dialect)
    _simulate_legacy_schema(db)

    with patch.object(db._engine.dialect, "name", "postgresql"):
        db._initialized = False
        db.init()

    inspector = inspect(db._engine)
    near_cols = {c["name"] for c in inspector.get_columns("dedup_near")}
    assert "sig_hex" in near_cols, "C-07: dedup_near.sig_hex must exist after migration"

    with db._engine.connect() as conn:
        ddl = str(conn.execute(text("SELECT sql FROM sqlite_master WHERE name = 'tasks'")).scalar() or "")
        assert "TIMESTAMP WITH TIME ZONE" in ddl.upper(), (
            "next_attempt_at must be added as TIMESTAMP WITH TIME ZONE on Postgres"
        )
        idx_rows = conn.execute(
            text(
                "SELECT 1 FROM sqlite_master WHERE type = 'index' "
                "AND name = 'ix_tasks_next_attempt_at'"
            )
        ).fetchall()
        assert idx_rows, "ix_tasks_next_attempt_at must be created on Postgres"


def test_postgres_branch_rerun_is_idempotent(tmp_path: Path) -> None:
    db = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    db.init()
    _simulate_legacy_schema(db)
    with patch.object(db._engine.dialect, "name", "postgresql"):
        db._initialized = False
        db.init()
        # Second init on the already-migrated schema must not raise.
        db._initialized = False
        db.init()
    inspector = inspect(db._engine)
    near_cols = {c["name"] for c in inspector.get_columns("dedup_near")}
    assert "sig_hex" in near_cols
