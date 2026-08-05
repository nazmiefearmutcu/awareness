# R3-W2 — FTS delta append fast path (iteration 1 probe)

- **Date**: 2026-08-04
- **Machine**: macOS arm64, Python (venv `/tmp/awareness-fresh/.venv`), DuckDB (project lock)
- **Repo**: `awareness` @ commit `a84a2ab`
- **Probe**: `/tmp/r3w2perf.py` (deterministic, 20k base + 1k delta corpus;
  `_FTS_COALESCE_WINDOW_SECONDS = 0.0` so the actual maintenance path is
  measured, `_FTS_SHARD_MAX_ROWS` raised so the 1k delta stays below any
  shard promotion)
- **Scope**: `src/awareness/storage/duckdb_index.py` — the Round-3
  iteration-1 delta-append fast path in `_try_incremental_fts_append`
  (pure-addition batches INSERT into the FTS index instead of a full
  rebuild; edits still full-rebuild)

---

## 1. What was measured

The iteration-6 (W25) coalescing already deferred FTS rebuilds, but every
rebuild — even an incremental append — was a full `create_fts_index` over
the whole inverted index (~7.5 s @100k, the top recommendation target).
Round 3 iteration 1 added a genuine delta path: pure-addition batches insert
only the new rows into `fts_main_captures_idx`, leaving the existing index
in place. The probe measures:

1. (a) delta maintenance — 1k-doc append + refresh, search-wall time, with
   the FTS-internal delta rebuild extracted from the `fts.build_seconds`
   histogram;
2. (b) warm search over the merged shards;
3. (c) the edit path — a same-day rewrite (same ids, changed content)
   must still full-rebuild and must not serve stale text.

## 2. Numbers

| Measurement | Result |
|---|---:|
| Corpus | 20,000 base + 1,000 delta (21,000 total) |
| Cold full build (20k) | 2.243 s |
| (a) Delta maintenance (1k append + refresh), search-wall | 1.650 s |
| (a) FTS-internal delta rebuild only | **0.137 s** |
| Delta vs full build | **~16.4× faster** |
| (b) Warm search (merged shards), mean of 10 | 130.78 ms |
| (c) Edit batch → full rebuild | 2.952 s (still works; stale text gone) |

The delta path was confirmed taken (`_fts_incremental_appends == 1`,
`_fts_full_rebuilds == 1` for the whole run) and the edit path confirmed to
full-rebuild (`_fts_full_rebuilds == 2` at exit).

## 3. Interpretation

The FTS-internal maintenance cost dropped from one full 2.24 s rebuild to a
0.137 s delta (~16×) for a pure-addition batch — the shard-append machinery
keeps the incremental INSERT bounded by the batch, not the corpus. The
remaining 1.65 s search-wall delta maintenance is dominated by the
materialized-corpus refresh of the changed day, not the FTS index; at 100k
corpus scale the ratio targets the ~7.5 s full-build baseline. Edit batches
still pay the full rebuild, as designed — edits are rare by design and
correctness (never stale text) is asserted in the probe.

## 4. Notes

- Deterministic corpus (fixed word list + content hashes), single machine;
  absolute times drift with hardware, the delta-vs-full ratio is the stable
  headline.
- Covered in-suite by `tests/unit/test_fts_delta_append.py` and
  `tests/unit/test_fts_shard_append.py` (295 + 307 lines).
