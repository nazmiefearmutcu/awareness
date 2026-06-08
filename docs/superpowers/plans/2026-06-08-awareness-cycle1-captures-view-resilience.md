# Awareness Cycle 1 — Plan 3b: Captures-View Resilience (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A single JSONL chunk that is missing a column must NOT take down all search. Build the `captures` view's staging projection defensively — coalesce any absent canonical column to a typed NULL — so one malformed/legacy chunk can't Binder-error the view out of existence and cascade "captures does not exist" to every query.

**Why (confirmed real):** `_refresh_views` builds `staging_captures_raw` via `read_json_auto(..., union_by_name=true)`, whose columns are the UNION of keys actually present across the chunk files. The `captures` view then SELECTs a fixed 29-column list from it; if a chunk lacks a key, DuckDB raises a Binder Error, which is caught and logged — leaving `captures` undefined, so EVERY subsequent `search()`/`/captures`/`/counts`/`/inspect` query fails with `Catalog Error: Table with name captures does not exist`. Verified end-to-end during Cycle-1 Plan 3 (a minimal-row chunk reproduced the cascade). (Audit: `bug:captures-view-build-aborts-on-any-missing-column`.)

**Tech Stack:** Python 3.13, DuckDB, pytest.

**Standard test command:** `PYTHONPATH=src .venv/bin/python -m pytest -q -p no:cacheprovider`
**Baseline at plan start:** 239 passing.

**Spec:** workstream D resilience item (deferred from Plan 3).

---

### Task 1: Defensive (NULL-filling) staging projection for the `captures` view

**Files:**
- Modify: `src/awareness/storage/duckdb_index.py` (`_refresh_views`; add a module-level projection helper + the canonical column spec)
- Test: `tests/unit/test_captures_view_resilience.py` (create)

- [ ] **Step 1: Write the failing test** — create `tests/unit/test_captures_view_resilience.py`:

```python
from __future__ import annotations

import json

from awareness.storage.duckdb_index import DuckDbIndex


def test_missing_column_does_not_break_captures(tmp_path) -> None:
    jsonl_dir = tmp_path / "jsonl"
    day = jsonl_dir / "captures" / "2026" / "06" / "08"
    day.mkdir(parents=True)
    # A chunk MISSING many canonical columns (no parent_doc_or_dup_group,
    # http_status, source_*, etc.) — exactly what a legacy/alt-tool/partial
    # writer could produce. Must not take down the whole captures view.
    row = {
        "doc_id": "d1", "capture_id": "c1",
        "url": "http://e.test/a", "canonical_url": "http://e.test/a",
        "domain": "e.test", "title": "Bitcoin", "text": "bitcoin is here",
        "language": "en", "fetch_ts": "2026-06-08T00:00:00+00:00",
    }
    (day / "c.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    idx = DuckDbIndex(db_path=tmp_path / "i.duckdb", jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    # Before the fix this raises CatalogException ("captures does not exist")
    # because the view failed to build on the missing columns.
    rows = idx.execute("SELECT count(*) AS n FROM captures")
    assert rows[0]["n"] == 1
    res = idx.search("bitcoin", limit=10)
    assert res["total"] >= 1
    # The absent column is present as NULL, not missing.
    got = idx.execute("SELECT parent_doc_or_dup_group FROM captures LIMIT 1")
    assert got[0]["parent_doc_or_dup_group"] is None
    idx.close()
```

- [ ] **Step 2: Run, confirm FAIL** (CatalogException — the captures view never built):
`PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_captures_view_resilience.py -q`

- [ ] **Step 3: Implement.** In `src/awareness/storage/duckdb_index.py`:

(a) Add a module-level canonical-column spec + projection helper (near the top, after the `DEFAULT_SEARCH_*` constants). The column ORDER must exactly match the existing iceberg-union SELECT (doc_id … terms_note_if_relevant):

```python
# Canonical captures columns in UNION order. The staging projection is built
# from this so a JSONL chunk missing any of these still yields a buildable
# `captures` view (absent columns become typed NULLs) instead of Binder-erroring
# the whole view out of existence.
_CAPTURE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("doc_id", "VARCHAR"), ("capture_id", "VARCHAR"), ("parent_doc_or_dup_group", "VARCHAR"),
    ("source_type", "VARCHAR"), ("source_name", "VARCHAR"), ("source_locator", "VARCHAR"),
    ("source_shard", "VARCHAR"), ("source_offset_or_record_id", "VARCHAR"),
    ("discovery_channel", "VARCHAR"), ("job_id", "VARCHAR"), ("batch_id", "VARCHAR"),
    ("ingest_version", "VARCHAR"), ("url", "VARCHAR"), ("canonical_url", "VARCHAR"),
    ("domain", "VARCHAR"),
    ("fetch_ts", "TIMESTAMPTZ"), ("observed_ts", "TIMESTAMPTZ"),
    ("published_ts", "TIMESTAMPTZ"), ("last_modified", "TIMESTAMPTZ"),
    ("content_type", "VARCHAR"), ("http_status", "INTEGER"), ("etag", "VARCHAR"),
    ("title", "VARCHAR"), ("text", "VARCHAR"), ("language", "VARCHAR"),
    ("content_hash", "VARCHAR"), ("near_dup_hash", "BIGINT"),
    ("robots_decision", "VARCHAR"), ("terms_note_if_relevant", "VARCHAR"),
)
_TS_COLUMNS = frozenset({"fetch_ts", "observed_ts", "published_ts", "last_modified"})


def _staging_projection(present: set[str]) -> str:
    """SELECT-list over ``staging_captures_raw`` that NULL-fills absent columns
    and TRY_CASTs timestamps / near_dup_hash, matching the iceberg-union order."""
    parts: list[str] = []
    for name, typ in _CAPTURE_COLUMNS:
        if name not in present:
            parts.append(f"NULL::{typ} AS {name}")
        elif name in _TS_COLUMNS:
            parts.append(f"TRY_CAST({name} AS TIMESTAMPTZ) AS {name}")
        elif name == "near_dup_hash":
            parts.append(f"TRY_CAST({name} AS BIGINT) AS {name}")
        else:
            parts.append(name)
    return ",\n                      ".join(parts)
```

(b) In `_refresh_views`, after `staging_captures_raw` is created (both the file-backed and the empty branch), introspect its columns and build the projection BEFORE the captures view block:
```python
        present = {
            str(r[0]) for r in conn.execute("DESCRIBE staging_captures_raw").fetchall()
        }
        staging_proj = _staging_projection(present)
```
(Place this right before the `iceberg_ok = False` line.)

(c) Use `staging_proj` for the STAGING side in both branches. In the iceberg-union `captures_raw_union` SELECT, replace the first (staging) SELECT's explicit column list — the lines from `doc_id, capture_id, parent_doc_or_dup_group,` through `terms_note_if_relevant` that read `FROM staging_captures_raw` — with `{staging_proj}` (an f-string), keeping `FROM staging_captures_raw`. Leave the SECOND SELECT (`FROM iceberg_captures_raw`) unchanged (Iceberg tables always have the full schema). The two CREATE VIEW statements that embed staging columns must become f-strings; mark them `# nosemgrep` like the neighbours (only whitelisted, code-derived column names are interpolated — never request input).

In the staging-only `else` branch, replace the explicit column list in `CREATE OR REPLACE VIEW captures AS SELECT … FROM staging_captures_raw;` with `SELECT {staging_proj} FROM staging_captures_raw;` (f-string, `# nosemgrep`).

- [ ] **Step 4: Confirm PASS:** `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_captures_view_resilience.py -q`
- [ ] **Step 5: Full-suite gate:** `PYTHONPATH=src .venv/bin/python -m pytest -q -m "not slow and not smoke"`. All existing search/duckdb tests must still pass (full-schema chunks project identically to before — the only behavioral change is that missing columns now NULL-fill instead of erroring). The `test_search_matching.py` fixtures that supply all 29 keys must produce identical results.
- [ ] **Step 6: Commit:**
```bash
git add src/awareness/storage/duckdb_index.py tests/unit/test_captures_view_resilience.py
git commit -m "fix(search): NULL-fill missing columns so one bad chunk can't break captures view"
```

---

## Plan-level self-review checklist

- [ ] Full suite green; the missing-column chunk is queryable (Task 1 test).
- [ ] Full-schema chunks behave identically (existing search tests unchanged).
- [ ] `ruff check src/awareness/storage/duckdb_index.py` introduces no NEW errors.
- [ ] Closes the Plan-3 deferred captures-view-resilience item (audit `bug:captures-view-build-aborts-on-any-missing-column`).
