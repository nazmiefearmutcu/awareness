"""Regression: a pre-``next_attempt_at`` tasks table must be migrated on init.

Legacy DBs (created before the retry-backoff column landed) otherwise raised
``sqlite3.OperationalError: no such column: tasks.next_attempt_at`` on every
``/tail/status`` poll, because the column was added to the model but not to the
auto-migration.
"""

from __future__ import annotations

import sqlite3

from awareness.storage.state import StateDB

_LEGACY_TASKS = """
CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY, job_id TEXT, source_type TEXT, partition_key TEXT,
    payload_json TEXT, status TEXT, attempts INTEGER, last_error TEXT,
    created_at DATETIME, started_at DATETIME, completed_at DATETIME,
    docs_emitted INTEGER, docs_dedup_dropped INTEGER, bytes_processed INTEGER,
    checkpoint_json TEXT
)
"""


def test_legacy_tasks_table_gains_next_attempt_at(tmp_path) -> None:
    db = tmp_path / "legacy.sqlite"
    con = sqlite3.connect(db)
    con.execute(_LEGACY_TASKS)
    con.commit()
    con.close()

    # init() must ALTER the legacy table (not crash) so downstream selects work.
    StateDB(f"sqlite:///{db}").init()

    con = sqlite3.connect(db)
    cols = [row[1] for row in con.execute("PRAGMA table_info(tasks)")]
    con.close()
    assert "next_attempt_at" in cols
