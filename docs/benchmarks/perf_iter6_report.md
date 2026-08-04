# W25 — Awareness: top-3 perf fixes (incremental materialize, signature guard, FTS coalescing)

- **Date**: 2026-08-04
- **Machine**: macOS arm64 (Apple M1), Python 3.13.9, DuckDB 1.5.3, venv `/tmp/awareness-fresh/.venv`
- **Repo**: `awareness` @ commit `a711ba6` (W23 benchmark baseline), working tree with user WIP (savedsearch)
- **Scope**: `src/awareness/storage/duckdb_index.py` (only src file changed); new tests + targeted updates to 4 existing tests.

---

## 1. Changes (file:line)

All production changes are in `src/awareness/storage/duckdb_index.py`:

| Change | Location |
|---|---|
| FTS coalescing window constant (`30.0s`, 0 disables) | `duckdb_index.py:286` |
| Instance state: `_signature_guard`, `_materialize_full_builds`, `_materialize_delta_applies`, `_fts_dirty_since`, `_fts_coalesce_window`, `_fts_coalesced_skips` | `duckdb_index.py:511-531` |
| Dir-mtime guard walk (root/year/month/day + iceberg metadata dir) | `_captures_dir_summary` `duckdb_index.py:675` |
| Guarded signature (returns `(sig, guard)`; skips the per-file walk on guard hit) | `_source_signature` `duckdb_index.py:715` |
| Old per-file walk (unchanged body) | `_walk_source_signature` `duckdb_index.py:733` |
| Guard + signature + `_fts_dirty_since` committed together on successful refresh | `_refresh_views_if_stale` `duckdb_index.py:779` |
| `_refresh_views(conn, new_sig=...)` threads the fresh signature into materialize | `duckdb_index.py:794` |
| Delta dispatch inside materialize; full-rebuild fallback intact | `_materialize_captures` `duckdb_index.py:1014` |
| Signature diff + delta decision (pure-addition only) | `_try_delta_materialize` `duckdb_index.py:1092` |
| Delta scan of changed files + conflict check + deduped INSERT | `_delta_insert_changed_files` `duckdb_index.py:1141` |
| FTS dirty marker cleared on rebuild | `_mark_fts_ready` `duckdb_index.py:1354` |
| Coalescing defer inside `_ensure_fts` (restore path still free/first) | `duckdb_index.py:1517` (defer block ~1550-1567) |

New tests:
- `tests/unit/test_materialize_incremental.py` (8 tests)
- `tests/unit/test_signature_guard.py` (6 tests)
- `tests/unit/test_fts_defer.py` (8 tests)

Existing tests updated (only to disable the coalescing window — they pin the pre-W25 immediate-rebuild FTS contract; all other assertions untouched):
- `tests/unit/test_fts_incremental.py`, `tests/unit/test_fts_content_change.py`, `tests/unit/test_duckdb_fts_freshness.py`, `tests/unit/test_search_features.py` — each gets an `@pytest.fixture(autouse=True) _disable_fts_coalescing` that sets `_FTS_COALESCE_WINDOW_SECONDS = 0.0` (window 0 = rebuild on the next search, exactly the old behavior). `test_materialized_corpus.py` needed **no** changes (delta path preserves its row-set semantics exactly).

Note: `tests/unit/test_spa_alerts_view.py` shows a diff but it belongs to the user's pre-existing savedsearch WIP, not this work.

---

## 2. Delta-vs-full-rebuild decision logic (`_try_delta_materialize` + `_delta_insert_changed_files`)

The materialized table must always equal the deduped view row set (`ROW_NUMBER() OVER (PARTITION BY capture_id ORDER BY fetch_ts DESC, capture_id ASC)`, newest fetch_ts wins, NULL ids dedup to one row). The delta path therefore runs **only when the diff is provably PURE ADDITION**:

1. **Delta eligible only when**: staging-only branch (`from_union=False` — the Iceberg union branch always full-rebuilds, since rows may have been REPLACED there), the old + fresh signatures are both known, and `captures_materialized` already exists with the canonical 29-column projection.
2. **Removed chunk** (a path in `prev_sig[0]` absent from `new_sig[0]`) → full rebuild. Deletions cannot be delta'd without a tombstone, and removal can also flip dedup winners.
3. **Changed/new files** = paths whose `(mtime_ns, size)` differ between the signatures. Only those files are scanned (`read_json_auto([...], union_by_name=true, ignore_errors=true)`, mirroring the M-01 tolerant fallback; any read error → full rebuild).
4. **NULL capture_id in the batch** → full rebuild (the ROW_NUMBER dedup collapses NULLs to one row; the delta INSERT cannot express that safely).
5. **Overlap conflict** — any batch row whose capture_id already exists in the table with a different `content_hash`/`fetch_ts`/`title`/`text` (`IS NOT DISTINCT FROM` comparisons, same H-10 semantics as the FTS stale check) → full rebuild. This covers re-fetch/update of an existing id and the "newest fetch_ts wins" edge (a newer row for an existing id would otherwise be a duplicate).
6. **Pure addition** → deduped `INSERT ... WHERE rn = 1 AND capture_id NOT IN (existing)` using the same `_staging_projection` + ROW_NUMBER as the full path, then re-assert the unique index and recreate the `captures` view. Re-inserting identical rows (crash-recovery re-append of the same chunk) is a no-op via the NOT IN guard.
7. Any `duckdb.Error` anywhere → `False` → the existing full `CREATE OR REPLACE TABLE` path runs unchanged (including the M-02 fallback). The temporary `staging_captures_delta` view is always dropped so the persisted .duckdb file never accumulates scratch views.

Correctness invariants verified by tests: dedup row-for-row parity with the view, updated-capture full rebuild (no duplication), removal full rebuild, identical re-append no-op, NULL-id safety, within-batch dedup to newest fetch_ts, unique index preserved.

## 3. Signature guard design (`_captures_dir_summary` + `_source_signature`)

- `_captures_dir_summary()` walks only directories: root `captures/`, then year → month → day dirs, recording `(name, mtime_ns)` per dir (plus the local iceberg metadata dir mtime). Chunk writes commit via **atomic rename inside the day dir** (jsonl.py), which bumps that day dir's mtime — so a new chunk in an *existing* day dir is detected (a years+months-only walk would miss it; the task's "2-level" suggestion was evaluated and rejected for correctness, since appends land in existing day dirs). Cost scales with calendar dirs (~95 stats @100k docs), not file count.
- `_source_signature()` returns `(signature, guard)`. On a guard hit (dir mtimes unchanged since the last commit) it returns the cached `_views_signature` without the ~1,081-file walk. Both are committed **together** only after a successful refresh, so an M-02 failed refresh never caches a guard that would short-circuit the retry (covered by `test_guard_does_not_commit_on_failed_refresh`).
- In-place overwrite of a finalized chunk (not used by the writer — it always renames) would be invisible to the guard, but the guard only caches what the full walk already treated as unchanged (`path, mtime_ns, size`), so the delta is never *less* correct than the previous full-rebuild change detection.

## 4. FTS coalescing design (`_ensure_fts` + `_fts_dirty_since`)

DuckDB FTS has no partial-update API — `create_fts_index` rebuilds the whole inverted index (~7.5s @100k) even for the incremental `captures_idx` delta INSERT. Targeted UPDATE of `fts_main_captures_idx.{docs,terms,dict}` is unsupported, so per the spec we **defer** instead:

- A successful views refresh stamps `_fts_dirty_since = time.monotonic()`.
- In `_ensure_fts`: if a *previously built* index is stale for the new signature and `now - dirty_since < _FTS_COALESCE_WINDOW_SECONDS` (default 30.0, module constant; 0 disables), the rebuild is deferred: returns `False`, the search falls back to the existing table-backed prefix/substring path (`mode="fts"` degrades to prefix exactly as it does for unavailable FTS). Content is correct because the materialized `captures` table already holds the new rows.
- The next write batch resets the window → N batches inside the window coalesce into **one** rebuild. The first search after the window elapses pays exactly one rebuild (full or the H-10-guarded incremental append), then searches are warm.
- Never defers the cold path: a never-built index (`_fts_built_signature is None`) always restores/builds immediately, and the free persisted-FTS restore is attempted before the defer branch (it can't succeed when the corpus changed, so ordering is equivalent).
- `_mark_fts_ready` clears the dirty marker; `health_snapshot` reports `fts_built: False` during the defer window (accurate).
- Observability: `_fts_coalesced_skips` counter + `duckdb_fts_coalesced` log event.

## 5. Perf numbers (20k docs, 100 docs/chunk, day-partitioned; median of 3; `/tmp/w25perf.py`)

| Measurement | Before (W23 report @20k/100k) | After @20k |
|---|---:|---:|
| Refresh after +100-doc append (delta) | 825 ms @20k (W13, full rebuild) / 2.436 s @100k | **0.6 ms** (0.7 ms retest) |
| Refresh after chunk REMOVAL (full-rebuild path, in-script baseline) | — | 123 ms |
| Delta/full speedup | — | **~196x** |
| Signature check, no corpus change (guarded) | 92 ms @100k per-call walk (16% of warm search) | **0.22 ms** (vs 3.6 ms full walk @20k; guard is dir-count-bound, ~1 ms est. @100k) |
| First post-batch search, inside 30s window | 7.4–7.9 s FTS rebuild + 0.43 s query (first search paid it) | **13 ms** (fallback; 0 rebuilds, 1 coalesced skip) |
| Search after window expiry (simulated) | — | 195 ms — exactly one incremental rebuild (200-row delta INSERT + create_fts_index @20k) |
| Subsequent warm search | 565 ms @100k | 45 ms @20k |

All targets met: delta refresh ≈ 50 ms-scale (well under at 20k; scales with the batch, not the corpus), signature check < 5 ms (0.22 ms), FTS coalesced to one rebuild across two batches, zero rebuilds hit the in-window searches.

## 6. Test results

- Task test list (18 files incl. the 3 new ones): **all pass** (1 pre-existing skip: `datasets` not installed in `test_search_features.py`).
- Full suite: **1525 passed, 1 skipped, 0 failed** (`tests/`).
- Ruff: **zero NEW violations** — `duckdb_index.py` is 34 → 34 (rule-by-rule identical to HEAD; the 3 new SQL statements carry `# noqa: S608` on the string-closing line, matching the file's existing suppression pattern); all new test files lint clean; the 4 updated test files introduce no new violations (their pre-existing I001/F401/PLC0415 untouched).
