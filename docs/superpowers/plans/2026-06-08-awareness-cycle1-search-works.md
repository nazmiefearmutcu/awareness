# Awareness Cycle 1 — Plan 3: Search That Works (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A terminal `awareness search <term>` returns every relevant capture across the FULL corpus and the FULL time range, ranked — fixing the query-side half of the "bitcoin returned 2 results" symptom.

**Architecture:** Four focused fixes: (1) index compressed `.jsonl.gz` chunks; (2) rebuild the FTS index when the corpus *content* changes (not only its row count); (3) make field-eligibility order-insensitive and multi-word fallback OR-by-default so the same query returns the same set on either path; (4) remove the silent 30-day default window from the CLI search and show the active window. All in `storage/duckdb_index.py` and `cli/main.py`.

**Tech Stack:** Python 3.13, DuckDB (+fts extension), Typer CLI, pytest.

**Standard test command:** `PYTHONPATH=src .venv/bin/python -m pytest -q -p no:cacheprovider`
**Baseline at plan start:** 214 passing after Plan 2.

**Scope source:** spec workstream **D** (terminal-search subset). Audit: `docs/superpowers/audit/2026-06-08-awareness-audit.json`.
**Deferred to Plan 3b (noted):** FTS index process-wide singleton + serialized rebuild (per-request rebuild & concurrent `/search` write-write-conflict — API-side); captures-view resilience to a structurally-missing column (latent today — `as_iceberg_row` always writes all 29 fields); inclusive end-of-day across endpoints; SPA/API search-default unification; pagination corruption; phrase/fuzzy modes.

---

### Task 1: Index compressed `.jsonl.gz` chunks

**Why:** `_source_signature` and `_refresh_views` glob `*.jsonl`, so when `jsonl_compress` is on the corpus is written as `.jsonl.gz` and is **invisible** to search — every query returns nothing. DuckDB's `read_json_auto` reads gzip by extension, so widening the glob to `*.jsonl*` fixes it. (Audit: `bug:jsonl-gz-corpus-invisible-to-search`.)

**Files:**
- Modify: `src/awareness/storage/duckdb_index.py` (`_source_signature` ~line 122; `_refresh_views` ~line 151)
- Test: `tests/unit/test_duckdb_gz.py` (create)

- [ ] **Step 1: Write the failing test** — create `tests/unit/test_duckdb_gz.py`:

```python
from __future__ import annotations

import gzip
import json

from awareness.storage.duckdb_index import DuckDbIndex


def _write_gz_chunk(jsonl_dir, row: dict) -> None:
    day = jsonl_dir / "captures" / "2026" / "06" / "08"
    day.mkdir(parents=True, exist_ok=True)
    with gzip.open(day / "chunk-0001.jsonl.gz", "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def test_gz_chunks_are_indexed(tmp_path) -> None:
    jsonl_dir = tmp_path / "jsonl"
    _write_gz_chunk(
        jsonl_dir,
        {
            "doc_id": "d1",
            "capture_id": "c1",
            "url": "http://example.test/a",
            "canonical_url": "http://example.test/a",
            "domain": "example.test",
            "title": "Bitcoin rally",
            "text": "bitcoin surged today",
            "language": "en",
            "fetch_ts": "2026-06-08T00:00:00+00:00",
        },
    )
    idx = DuckDbIndex(db_path=tmp_path / "idx.duckdb", jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    rows = idx.execute("SELECT count(*) AS n FROM captures")
    assert rows[0]["n"] == 1
    res = idx.search("bitcoin", limit=10)
    assert res["total"] >= 1
    idx.close()
```

- [ ] **Step 2: Run, confirm FAIL** (count is 0 — the `.jsonl.gz` chunk is not globbed):
`PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_duckdb_gz.py -q`

- [ ] **Step 3: Implement.** In `src/awareness/storage/duckdb_index.py`, change both globs from `"*.jsonl"` to `"*.jsonl*"`:
  - In `_source_signature` (~line 122): `for p in captures_root.rglob("*.jsonl"):` → `for p in captures_root.rglob("*.jsonl*"):`
  - In `_refresh_views` (~line 151): `existing = list(captures_root.rglob("*.jsonl")) if captures_root.exists() else []` → `existing = list(captures_root.rglob("*.jsonl*")) if captures_root.exists() else []`

  Verify there are no OTHER `rglob("*.jsonl")` occurrences that should change: `grep -n 'rglob' src/awareness/storage/duckdb_index.py`. Change the two that feed the captures views; leave anything unrelated.

- [ ] **Step 4: Confirm PASS:** `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_duckdb_gz.py -q`
- [ ] **Step 5: Full-suite gate:** `PYTHONPATH=src .venv/bin/python -m pytest -q -m "not slow and not smoke"`
- [ ] **Step 6: Commit:**
```bash
git add src/awareness/storage/duckdb_index.py tests/unit/test_duckdb_gz.py
git commit -m "fix(search): index compressed .jsonl.gz chunks (widen glob to *.jsonl*)"
```

---

### Task 2: Rebuild FTS when corpus content changes (not just row count)

**Why:** `_ensure_fts` skips rebuilding when `count == self._fts_built_for_count`, even if the on-disk content changed (same row count, different docs) — serving stale full-text results. Key the rebuild on the content signature instead. (Audit: `bug:fts-index-rebuild-keyed-on-rowcount-serves-stale-results`.)

**Files:**
- Modify: `src/awareness/storage/duckdb_index.py` (`_ensure_fts`, ~lines 307-358)
- Test: `tests/unit/test_duckdb_fts_freshness.py` (create)

- [ ] **Step 1: Write the failing test** — create `tests/unit/test_duckdb_fts_freshness.py`:

```python
from __future__ import annotations

import json

from awareness.storage.duckdb_index import DuckDbIndex


def _write_chunk(jsonl_dir, name: str, doc_id: str, text: str) -> None:
    day = jsonl_dir / "captures" / "2026" / "06" / "08"
    day.mkdir(parents=True, exist_ok=True)
    row = {
        "doc_id": doc_id, "capture_id": doc_id, "url": f"http://example.test/{doc_id}",
        "canonical_url": f"http://example.test/{doc_id}", "domain": "example.test",
        "title": text, "text": text, "language": "en",
        "fetch_ts": "2026-06-08T00:00:00+00:00",
    }
    (day / name).write_text(json.dumps(row) + "\n", encoding="utf-8")


def test_fts_reflects_new_content_at_same_row_count(tmp_path) -> None:
    jsonl_dir = tmp_path / "jsonl"
    _write_chunk(jsonl_dir, "a.jsonl", "d1", "alpha unique-alpha-term")
    idx = DuckDbIndex(db_path=tmp_path / "idx.duckdb", jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    assert idx.search("unique-alpha-term", limit=10)["total"] >= 1

    # Replace the single chunk with different content — SAME row count (1).
    (jsonl_dir / "captures" / "2026" / "06" / "08" / "a.jsonl").unlink()
    _write_chunk(jsonl_dir, "b.jsonl", "d2", "beta unique-beta-term")

    assert idx.search("unique-beta-term", limit=10)["total"] >= 1, "FTS must reflect new content"
    assert idx.search("unique-alpha-term", limit=10)["total"] == 0, "stale content must be gone"
    idx.close()
```

- [ ] **Step 2: Run, confirm FAIL** (the beta search returns 0 / alpha still returns 1 — stale index):
`PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_duckdb_fts_freshness.py -q`

- [ ] **Step 3: Implement.** In `_ensure_fts`, REMOVE the row-count shortcut that returns without rebuilding. Find this block (~lines 330-332):
```python
        if count == self._fts_built_for_count:
            self._fts_built_signature = self._views_signature
            return True
```
and DELETE it. The fast-path at the top of the method (`if self._fts_built_signature is not None and self._fts_built_signature == self._views_signature: return True`) already short-circuits when nothing changed; removing the count shortcut means any signature change triggers a real rebuild. Keep `self._fts_built_for_count = count` in the rebuild branch (it's still set for logging).

- [ ] **Step 4: Confirm PASS:** `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_duckdb_fts_freshness.py -q`
- [ ] **Step 5: Full-suite gate** (existing `test_search_matching.py` must still pass).
- [ ] **Step 6: Commit:**
```bash
git add src/awareness/storage/duckdb_index.py tests/unit/test_duckdb_fts_freshness.py
git commit -m "fix(search): rebuild FTS on content-signature change, not just row count"
```

---

### Task 3: Order-insensitive field eligibility + OR-by-default multi-word fallback

**Why:** `fts_eligible = list(cols) == list(DEFAULT_SEARCH_FIELDS)` is order-sensitive: `--fields text,title` (reversed) silently drops the ranked BM25 path. And the prefix fallback ANDs all tokens while FTS ORs them, so a multi-word query returns different sets depending on which path runs. Make eligibility a set comparison and the prefix fallback OR-by-default for consistent recall. (Audit: `bug:fts-eligibility-field-order-sensitive`, `bug:fts-or-vs-prefix-and-semantics-inconsistent`.)

**Files:**
- Modify: `src/awareness/storage/duckdb_index.py` (`search`, the `fts_eligible` line ~455 and the prefix token loop ~504-511)
- Test: `tests/unit/test_search_consistency.py` (create)

- [ ] **Step 1: Read** `search()` lines ~447-536 to confirm exact current text of: (a) `fts_eligible = list(cols) == list(DEFAULT_SEARCH_FIELDS)`; (b) the prefix branch that builds per-root `where.insert(i, "(" + " OR ".join(...) + ")")` — note that each root is a SEPARATE `where` entry, which AND-joins them via `" AND ".join(where)`.

- [ ] **Step 2: Write the failing test** — create `tests/unit/test_search_consistency.py`:

```python
from __future__ import annotations

import json

from awareness.storage.duckdb_index import DuckDbIndex


def _write(jsonl_dir, doc_id: str, title: str, text: str) -> None:
    day = jsonl_dir / "captures" / "2026" / "06" / "08"
    day.mkdir(parents=True, exist_ok=True)
    row = {
        "doc_id": doc_id, "capture_id": doc_id, "url": f"http://x.test/{doc_id}",
        "canonical_url": f"http://x.test/{doc_id}", "domain": "x.test",
        "title": title, "text": text, "language": "en",
        "fetch_ts": "2026-06-08T00:00:00+00:00",
    }
    (day / f"{doc_id}.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")


def _idx(tmp_path):
    jsonl_dir = tmp_path / "jsonl"
    _write(jsonl_dir, "d1", "Bitcoin news", "bitcoin price moved")
    _write(jsonl_dir, "d2", "Ethereum update", "ethereum staking grew")
    return DuckDbIndex(db_path=tmp_path / "idx.duckdb", jsonl_dir=jsonl_dir, iceberg_warehouse=None)


def test_reversed_field_order_still_ranks(tmp_path) -> None:
    idx = _idx(tmp_path)
    res = idx.search("bitcoin", mode="auto", fields=["text", "title"], limit=10)
    assert res["total"] >= 1
    assert res["ranked"] is True  # reversed default fields must still take the BM25 path
    idx.close()


def test_multiword_prefix_fallback_is_or(tmp_path) -> None:
    idx = _idx(tmp_path)
    # Force the prefix path; a two-term query should OR (match either term),
    # returning BOTH docs rather than only rows containing both.
    res = idx.search("bitcoin ethereum", mode="prefix", limit=10)
    assert res["total"] == 2
    idx.close()
```

- [ ] **Step 3: Implement** in `search()`:

(a) Make eligibility order-insensitive. Change:
```python
            fts_eligible = list(cols) == list(DEFAULT_SEARCH_FIELDS)
```
to:
```python
            fts_eligible = set(cols) == set(DEFAULT_SEARCH_FIELDS)
```

(b) Make the prefix fallback OR the tokens. In the prefix branch (the `else` after `if mode == "substring" or not terms:`), the loop currently appends each root as its own `where` entry (AND-joined). Replace that loop so all roots are combined into a SINGLE OR group. Change:
```python
                else:
                    # Match every token's stem-root anywhere in the chosen fields.
                    roots = self._stem_roots(conn, terms)
                    snippet_terms = roots or terms
                    for i, root in enumerate(roots):
                        key = f"r{i}"
                        params[key] = f"%{root}%"
                        where.insert(i, "(" + " OR ".join(f"{c} ILIKE ${key}" for c in cols) + ")")
                    used_mode = "prefix"
```
to:
```python
                else:
                    # Match ANY token's stem-root in ANY chosen field (OR-by-default,
                    # matching the FTS path so multi-word recall is consistent).
                    roots = self._stem_roots(conn, terms)
                    snippet_terms = roots or terms
                    or_clauses: list[str] = []
                    for i, root in enumerate(roots):
                        key = f"r{i}"
                        params[key] = f"%{root}%"
                        or_clauses.extend(f"{c} ILIKE ${key}" for c in cols)
                    if or_clauses:
                        where.insert(0, "(" + " OR ".join(or_clauses) + ")")
                    used_mode = "prefix"
```

- [ ] **Step 4: Confirm PASS:** `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_search_consistency.py -q`
- [ ] **Step 5: Full-suite gate** (existing `test_search_matching.py` must still pass; if a test asserted the old order-sensitive or AND behavior, read it — the new behavior is the intended one; update only if the old assertion encoded the bug, and note under Deviations).
- [ ] **Step 6: Commit:**
```bash
git add src/awareness/storage/duckdb_index.py tests/unit/test_search_consistency.py
git commit -m "fix(search): order-insensitive field eligibility; OR-by-default prefix fallback"
```

---

### Task 4: Remove the silent 30-day window from CLI search; show the active window

**Why:** The CLI `search` command defaults `--start` to `"30 days ago"`, silently hiding everything older — the single most direct cause of "bitcoin returned only 2 results". Default to all-time, resolve the window through one helper, and print the active window so the scope is never hidden. (Audit: `bug:cli-search-default-30day-window-hides-results`.)

**Files:**
- Modify: `src/awareness/cli/main.py` (`search` command, signature ~line 2595, window resolution ~line 2628-2630, result header ~line 2655)
- Test: `tests/unit/test_search_window.py` (create)

- [ ] **Step 1: Read** `cli/main.py` lines ~2592-2660 to confirm the `search` signature, the `start_dt = to_utc(start)` / `end_dt = coerce_relative_end(end)` lines, and the non-interactive result header. Confirm `to_utc` and `coerce_relative_end` are imported (line 50).

- [ ] **Step 2: Write the failing test** — create `tests/unit/test_search_window.py`:

```python
from __future__ import annotations

from datetime import datetime

from awareness.cli.main import _resolve_search_window


def test_empty_start_means_all_time() -> None:
    start_dt, end_dt = _resolve_search_window("", "now")
    assert start_dt is None  # all-time, no lower bound
    assert end_dt is not None


def test_all_keyword_means_all_time() -> None:
    start_dt, _ = _resolve_search_window("all time", "now")
    assert start_dt is None


def test_explicit_start_is_parsed() -> None:
    start_dt, _ = _resolve_search_window("2026-01-01", "now")
    assert isinstance(start_dt, datetime)
    assert start_dt.year == 2026 and start_dt.month == 1
```

- [ ] **Step 3: Run, confirm FAIL** (`ImportError: cannot import name '_resolve_search_window'`):
`PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_search_window.py -q`

- [ ] **Step 4: Implement** in `src/awareness/cli/main.py`:

(a) Add a module-level helper (place it near the other search helpers, e.g. just above the `search` command):
```python
def _resolve_search_window(start: str, end: str) -> tuple[datetime | None, datetime | None]:
    """Resolve the (start, end) search window. An empty/"all"/"all time" start
    means NO lower bound (search the entire corpus) instead of the old silent
    30-day default that hid most captures."""
    s = (start or "").strip().lower()
    start_dt = None if s in ("", "all", "all time", "alltime", "any") else to_utc(start)
    end_dt = coerce_relative_end(end)
    return start_dt, end_dt
```
(Ensure `datetime` is imported at the top of `cli/main.py`; if not, add `from datetime import datetime`. Check first.)

(b) Change the `--start` default in the `search` command signature from:
```python
    start: str = typer.Option("30 days ago", "--start", help="Start date range"),
```
to:
```python
    start: str = typer.Option("", "--start", help="Start date range (empty = all time; e.g. '30 days ago', '2026-01-01')"),
```

(c) Replace the window resolution lines:
```python
    start_dt = to_utc(start)
    end_dt = coerce_relative_end(end)
```
with:
```python
    start_dt, end_dt = _resolve_search_window(start, end)
```

(d) Surface the active window in the non-interactive header. Immediately BEFORE the `rprint(f"[bold cyan]Search Results for:...` line, add:
```python
        window = f"{start_dt.date() if start_dt else 'all time'} → {end_dt.date() if end_dt else 'now'}"
        rprint(f"[dim]Window: {window}[/dim]")
```

- [ ] **Step 5: Confirm PASS:** `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_search_window.py -q`
- [ ] **Step 6: Full-suite gate.**
- [ ] **Step 7: Commit:**
```bash
git add src/awareness/cli/main.py tests/unit/test_search_window.py
git commit -m "fix(cli): default search to all-time (drop hidden 30-day window) and show active window"
```

---

## Plan-level self-review checklist

- [ ] Full suite green after all tasks.
- [ ] A fresh corpus written compressed is searchable; `search bitcoin` with no `--start` searches all time.
- [ ] `ruff check` introduces no NEW errors in touched files.
- [ ] Deferred to Plan 3b recorded: FTS singleton + write-conflict (API), captures-view resilience, inclusive end-of-day, SPA/API search-default unification, pagination, phrase/fuzzy.

## Spec coverage map (workstream D subset)

| Item | Task |
|---|---|
| Index `.jsonl.gz` | 1 |
| FTS rebuild on content signature | 2 |
| Order-insensitive field eligibility | 3 |
| OR-by-default multi-word | 3 |
| Remove 30-day CLI window + show window | 4 |
| FTS singleton/write-conflict, captures-view resilience, end-of-day, API/SPA unify, pagination | Deferred → Plan 3b |
