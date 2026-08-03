"""M-22: uf_find is read-only.

Regression: uf_find used to insert self-root rows (and commit) for any doc
with no dup_parent row — a *read* that mutated state. It now returns the
doc_id itself for missing rows and never writes.
"""

from __future__ import annotations

from pathlib import Path

from awareness.storage.state import StateDB


def _dup_parent_row_count(state: StateDB) -> int:
    from sqlalchemy import func, select

    from awareness.storage.state import DupParentRow

    with state.session() as s:
        return int(s.scalar(select(func.count(DupParentRow.doc_id))) or 0)


def test_uf_find_missing_doc_does_not_write(tmp_path: Path) -> None:
    db = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    db.init()
    assert _dup_parent_row_count(db) == 0

    assert db.uf_find("missing-doc") == "missing-doc"
    # The read must not have created any rows.
    assert _dup_parent_row_count(db) == 0


def test_uf_find_after_union_does_not_add_rows(tmp_path: Path) -> None:
    db = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    db.init()

    root = db.uf_union("A", "B")
    before = _dup_parent_row_count(db)
    assert before >= 1

    assert db.uf_find("A") == root
    assert db.uf_find("B") == root
    # Path-following reads must not create or rewrite rows.
    assert _dup_parent_row_count(db) == before


def test_uf_find_fresh_doc_is_self_root_without_crash(tmp_path: Path) -> None:
    """H-23 calls uf_find on a fresh (never-union'd) doc — must return doc_id."""
    db = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    db.init()
    assert db.uf_find("brand-new-doc") == "brand-new-doc"
    assert _dup_parent_row_count(db) == 0
