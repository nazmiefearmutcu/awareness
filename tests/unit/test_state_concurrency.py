from __future__ import annotations

from sqlalchemy import text

from awareness.storage.state import StateDB


def test_sqlite_uses_wal_and_busy_timeout(tmp_path) -> None:
    state = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    state.init()
    with state.session() as s:
        journal_mode = s.execute(text("PRAGMA journal_mode")).scalar()
        busy_timeout = s.execute(text("PRAGMA busy_timeout")).scalar()
    assert str(journal_mode).lower() == "wal"
    assert int(busy_timeout) == 5000
