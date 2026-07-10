# Awareness Cycle 1 — P3b: Process-Wide Index Singleton (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the FastAPI app from constructing a fresh `DuckDbIndex` on every request. Share ONE process-wide index (which already caches its DuckDB connection + FTS-build signature and serializes all access behind an `RLock`), so the FTS index is built once instead of per-request, and concurrent `/search` calls can no longer collide on DuckDB's single-writer lock during an FTS rebuild.

**Architecture:** `DuckDbIndex` is already internally thread-safe: `search()`, `execute()`, and `refresh()` each run under `self._lock` (an `RLock`), `connect()` memoizes `self._conn`, and `_ensure_fts()` rebuilds only when `_fts_built_signature` changes. The ONLY per-request state that matters is the instance itself — so the fix is to create it once and reuse it. Two changes: (1) add a lock-guarded `DuckDbIndex.related()` so the `/related` endpoint stops running a query on the raw connection *outside* the lock (the one access path that isn't serialized today); (2) introduce a `_get_index()` accessor in `api/server.py` (double-checked locking, stored on `_State.index`) and route all six endpoints through it, closing it on lifespan shutdown.

**Tech Stack:** Python 3.13 stdlib (`threading`), FastAPI, DuckDB, pytest. No new deps. No FastAPI TestClient needed — the singleton accessor is unit-tested by monkeypatching `get_settings`, and `related()` is tested directly like the existing search tests.

**Standard test command:** `PYTHONPATH=src .venv/bin/python -m pytest -q -p no:cacheprovider`
**Full-suite gate:** `PYTHONPATH=src .venv/bin/python -m pytest -q -m "not slow and not smoke"`
**Baseline at plan start:** 259 passing (after Cycle 2 Plan 3 BM25F re-rank).

**Spec:** `docs/superpowers/specs/2026-06-08-awareness-make-it-work-design.md` (P3b — search availability) and `HANDOFF.md` item 2. *Scope note:* this plan delivers ONLY the singleton/serialized-rebuild fix. The other P3b remnants (inclusive end-of-day, pagination corruption, phrase/prefix/fuzzy) are separate follow-ups and are NOT in scope here.

**Files touched:**
- Modify: `src/awareness/storage/duckdb_index.py` — add a lock-guarded `related()` method on `DuckDbIndex`.
- Modify: `src/awareness/api/server.py` — add `_State.index`, a module-level `_index_lock`, a `_get_index()` accessor; replace the six per-request `DuckDbIndex(...)` constructions; route `/related` through `idx.related(...)`; close the index on lifespan shutdown.
- Test (create): `tests/unit/test_duckdb_related.py` — `related()` correctness + lock-guarding.
- Test (create): `tests/unit/test_api_index_singleton.py` — singleton identity + thread-safe creation.

---

### Task 1: Lock-guarded `DuckDbIndex.related()`

**Files:**
- Modify: `src/awareness/storage/duckdb_index.py` (add a method on `DuckDbIndex`, e.g. right after `execute`, ~line 333)
- Test (create): `tests/unit/test_duckdb_related.py`

- [ ] **Step 1: Write the failing test** — create `tests/unit/test_duckdb_related.py`:

```python
"""DuckDbIndex.related(): lock-guarded sibling lookup for the shared singleton.

Equivalent to the module-level find_related_captures(conn, ...) but routed
through self._lock so a process-wide singleton can serve /related safely
across FastAPI's threadpool without touching the raw connection unguarded.
"""

from __future__ import annotations

import json
from pathlib import Path

from awareness.storage.duckdb_index import DuckDbIndex, find_related_captures

_FULL_KEYS = (
    "doc_id", "capture_id", "parent_doc_or_dup_group", "source_type",
    "source_name", "source_locator", "source_shard",
    "source_offset_or_record_id", "discovery_channel", "job_id", "batch_id",
    "ingest_version", "url", "canonical_url", "domain", "fetch_ts",
    "observed_ts", "published_ts", "last_modified", "content_type",
    "http_status", "etag", "title", "text", "language", "content_hash",
    "near_dup_hash", "robots_decision", "terms_note_if_relevant",
)


def _write_doc(root: Path, idx: int, *, group: str, title: str = "t", text: str = "body") -> None:
    day = root / "captures" / "2026" / "06" / "01"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}", capture_id=f"cap-{idx}", parent_doc_or_dup_group=group,
        source_type="rss", domain="example.com", url=f"https://example.com/{idx}",
        fetch_ts=f"2026-06-01T12:0{idx}:00+00:00", title=title, text=text,
    )
    (day / f"chunk-{idx}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _index(tmp_path: Path) -> DuckDbIndex:
    return DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )


def test_related_returns_siblings_in_same_group(tmp_path: Path) -> None:
    jsonl = tmp_path / "jsonl"
    _write_doc(jsonl, 0, group="grp-A")
    _write_doc(jsonl, 1, group="grp-A")     # sibling of cap-0
    _write_doc(jsonl, 2, group="grp-B")     # unrelated
    idx = _index(tmp_path)
    sibs = idx.related("cap-0", limit=12)
    ids = {r["capture_id"] for r in sibs}
    assert ids == {"cap-1"}                 # same group, excludes self, excludes other group


def test_related_matches_module_function(tmp_path: Path) -> None:
    jsonl = tmp_path / "jsonl"
    _write_doc(jsonl, 0, group="grp-A")
    _write_doc(jsonl, 1, group="grp-A")
    idx = _index(tmp_path)
    via_method = idx.related("cap-0", limit=5)
    via_func = find_related_captures(idx.connect(), "cap-0", limit=5)
    assert [r["capture_id"] for r in via_method] == [r["capture_id"] for r in via_func]


def test_related_respects_limit(tmp_path: Path) -> None:
    jsonl = tmp_path / "jsonl"
    for i in range(5):
        _write_doc(jsonl, i, group="grp-A")
    idx = _index(tmp_path)
    assert len(idx.related("cap-0", limit=2)) == 2


def test_related_unknown_capture_is_empty(tmp_path: Path) -> None:
    jsonl = tmp_path / "jsonl"
    _write_doc(jsonl, 0, group="grp-A")
    idx = _index(tmp_path)
    assert idx.related("cap-does-not-exist") == []
```

- [ ] **Step 2: Run, confirm FAIL** (`AttributeError: 'DuckDbIndex' object has no attribute 'related'`):
`PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_duckdb_related.py -q`

- [ ] **Step 3: Implement** — add the method to `DuckDbIndex` in `src/awareness/storage/duckdb_index.py`, directly after the `execute` method (after its `return [dict(zip(...))...]` line, ~line 333):

```python
    def related(self, capture_id: str, *, limit: int = 12) -> list[dict[str, Any]]:
        """Sibling captures in the same dup-group, lock-guarded.

        Wraps the module-level :func:`find_related_captures` under ``self._lock``
        so a process-wide singleton can serve ``/related`` from FastAPI's
        threadpool without ever touching the raw connection unsynchronized.
        """
        with self._lock:
            conn = self.connect()
            self._refresh_views_if_stale(conn)
            return find_related_captures(conn, capture_id, limit=limit)
```

(`find_related_captures` is defined later in the same module; the forward reference resolves at call time, so order is fine. `self._lock` is a re-entrant `RLock`, so `connect()`/`_refresh_views_if_stale()` re-acquiring it is safe.)

- [ ] **Step 4: Confirm PASS:** `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_duckdb_related.py -q`
- [ ] **Step 5: Full-suite gate:** `PYTHONPATH=src .venv/bin/python -m pytest -q -m "not slow and not smoke"` (expect 263 = 259 + 4).
- [ ] **Step 6: Ruff:** `.venv/bin/python -m ruff check src/awareness/storage/duckdb_index.py tests/unit/test_duckdb_related.py` — no NEW errors.
- [ ] **Step 7: Commit:**
```bash
git add src/awareness/storage/duckdb_index.py tests/unit/test_duckdb_related.py
git commit -m "feat(search): add lock-guarded DuckDbIndex.related() for shared use"
```

---

### Task 2: Process-wide index singleton in the API

**Files:**
- Modify: `src/awareness/api/server.py`
- Test (create): `tests/unit/test_api_index_singleton.py`

**Context the implementer must confirm first (read `src/awareness/api/server.py`):**
- The `_State` holder class (holds `state`, `planner`, `tail`, `background_tasks` as class attributes) — add `index: DuckDbIndex | None = None`.
- Whether `import threading` is already present near the top imports; add it if not.
- The six endpoints that build `DuckDbIndex(db_path=s.duckdb_path(), jsonl_dir=s.staging_jsonl_dir(), iceberg_warehouse=s.iceberg_warehouse)`: `/inspect`, `/counts`, `/captures`, `/captures/{capture_id}`, `/captures/{capture_id}/related`, `/search`.
- The `lifespan` async context manager's `finally:` block (for shutdown cleanup).

- [ ] **Step 1: Write the failing test** — create `tests/unit/test_api_index_singleton.py`:

```python
"""The API shares ONE process-wide DuckDbIndex instead of building one per
request (so FTS is built once and concurrent searches don't collide on
DuckDB's single-writer lock during a rebuild)."""

from __future__ import annotations

import threading
from pathlib import Path

import awareness.api.server as server
from awareness.storage.duckdb_index import DuckDbIndex


class _FakeSettings:
    """Minimal stand-in exposing just what _get_index() reads."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self.iceberg_warehouse = None

    def duckdb_path(self) -> Path:
        return self._root / "duckdb" / "metadata.duckdb"

    def staging_jsonl_dir(self) -> Path:
        return self._root / "jsonl"


def test_get_index_returns_same_instance(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(server, "get_settings", lambda: _FakeSettings(tmp_path))
    server._State.index = None
    try:
        a = server._get_index()
        b = server._get_index()
        assert isinstance(a, DuckDbIndex)
        assert a is b                      # reused, not rebuilt per call
    finally:
        server._State.index = None


def test_get_index_is_threadsafe_single_instance(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(server, "get_settings", lambda: _FakeSettings(tmp_path))
    server._State.index = None
    try:
        seen: list[int] = []
        barrier = threading.Barrier(8)

        def worker() -> None:
            barrier.wait()                 # maximize the race on first creation
            seen.append(id(server._get_index()))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(set(seen)) == 1, "double-checked locking must yield exactly one instance"
    finally:
        server._State.index = None
```

- [ ] **Step 2: Run, confirm FAIL** (`AttributeError: module 'awareness.api.server' has no attribute '_get_index'`):
`PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_api_index_singleton.py -q`

- [ ] **Step 3: Implement** in `src/awareness/api/server.py`:

(a) Ensure `import threading` is among the top-of-file imports (add it if absent, keeping the import block sorted).

(b) Add `index: DuckDbIndex | None = None` as a class attribute on `_State` (alongside `state`, `planner`, `tail`).

(c) Add a module-level lock + accessor near `_State` (above `create_app`):
```python
_index_lock = threading.Lock()


def _build_index() -> DuckDbIndex:
    s = get_settings()
    return DuckDbIndex(
        db_path=s.duckdb_path(),
        jsonl_dir=s.staging_jsonl_dir(),
        iceberg_warehouse=s.iceberg_warehouse,
    )


def _get_index() -> DuckDbIndex:
    """Process-wide DuckDbIndex (double-checked locking). One instance memoizes
    the DuckDB connection + FTS-build signature and serializes access behind its
    own RLock, so FTS is built once and concurrent searches don't collide."""
    idx = _State.index
    if idx is not None:
        return idx
    with _index_lock:
        if _State.index is None:
            _State.index = _build_index()
        return _State.index
```

(d) In each of the six endpoints, replace the per-request construction:
```python
        s = get_settings()
        idx = DuckDbIndex(
            db_path=s.duckdb_path(),
            jsonl_dir=s.staging_jsonl_dir(),
            iceberg_warehouse=s.iceberg_warehouse,
        )
```
with:
```python
        idx = _get_index()
```
(Keep any *other* use of `s`/`get_settings()` an endpoint still needs — e.g. `/search` reads `s.search_default_fields`, `s.search_default_mode`, `s.search_max_results`, and `/inspect` etc. do not need `s` once the index construction is gone. Leave a `s = get_settings()` only where `s` is still referenced.)

(e) Route `/captures/{capture_id}/related` through the new method — drop the `from awareness.storage.duckdb_index import find_related_captures` local import and the raw `conn = idx.connect()`:
```python
    @app.get("/captures/{capture_id}/related")
    def capture_related(capture_id: str, limit: int = Query(12, ge=1, le=50)) -> dict[str, Any]:
        idx = _get_index()
        siblings = idx.related(capture_id, limit=limit)
        return {"capture_id": capture_id, "siblings": siblings}
```

(f) Close the singleton on shutdown — in the `lifespan` `finally:` block (after the tail stop / background-task cancellation), add:
```python
            if _State.index is not None:
                _State.index.close()
                _State.index = None
```

- [ ] **Step 4: Confirm PASS:** `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_api_index_singleton.py -q`
- [ ] **Step 5: Full-suite gate:** `PYTHONPATH=src .venv/bin/python -m pytest -q -m "not slow and not smoke"` (expect 265 = 263 + 2). If any existing test imported `find_related_captures` from server or relied on per-request index construction, READ it and adjust to the singleton, recording it under Deviations.
- [ ] **Step 6: Ruff:** `.venv/bin/python -m ruff check src/awareness/api/server.py tests/unit/test_api_index_singleton.py` — no NEW errors (note: an unused `find_related_captures` import removed in (e) should clear, not add, a warning).
- [ ] **Step 7: Commit:**
```bash
git add src/awareness/api/server.py tests/unit/test_api_index_singleton.py
git commit -m "fix(api): share one process-wide DuckDbIndex across requests"
```

---

## Plan-level self-review checklist

- [ ] Full suite green (expect 265).
- [ ] All six endpoints use `_get_index()`; no endpoint still constructs `DuckDbIndex(...)` per request.
- [ ] `_get_index()` is double-checked-locked and returns a single instance under thread contention (proven by `test_get_index_is_threadsafe_single_instance`).
- [ ] `/related` no longer touches the raw connection — it calls the lock-guarded `idx.related(...)`.
- [ ] Lifespan shutdown closes the index and resets `_State.index` to None.
- [ ] No NEW ruff errors on the two touched source files.

## Spec coverage note

Delivers HANDOFF item 2's "FTS index process-wide singleton + serialized rebuild" (the concurrency/availability core of Cycle 1 P3b). The serialized rebuild is inherited free: one shared instance already gates `_ensure_fts()` behind its `RLock` and rebuilds only on a changed source signature. Remaining P3b items (inclusive end-of-day across `/captures`/`/search`/`/inspect`/`/counts`, pagination corruption, phrase/prefix/fuzzy) are out of scope for this plan and stay on the backlog.
