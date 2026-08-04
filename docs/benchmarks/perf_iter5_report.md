# Awareness — Iter-5 perf probe: materialized-corpus follow-up @ 100k docs (`daacf9b`)

- **Date**: 2026-08-04
- **Machine**: macOS arm64, Apple M1 8-core, 16 GiB RAM (sysctl:
  `hw.memsize=17179869184`), Python 3.13.9, DuckDB 1.5.3
- **Repo**: `awareness` @ commit `daacf9b` (`feat: search IDF diagnostics,
  alert multi-webhook/Slack + import/export, E2E smoke; fix: W16 findings`)
  — i.e. the commit **after** the materialized-corpus change (`876dbc6`)
- **Round-1 baseline**: `perf_100k_2026-08-04.md` @ `876dbc6`, same
  machine/venv, same corpus generator (seed 4242)
- **Corpus**: 100,000 docs, 90 days (2026-05-06…08-03), day-partitioned
  `captures/YYYY/MM/DD/chunk-*.jsonl` (100 docs/chunk ≈ 1,000 files),
  `_write_doc` column pattern from `tests/unit/test_analytics_engine.py`.
  Export byte-identity (6,728,235 B) confirms corpus determinism vs round 1.
- **Method**: `time.perf_counter`, median of 3 for warm paths; cold paths
  single-shot. Fresh DuckDB file, fresh process. Probe scripts and
  `results.json` retained in the audit workspace.

> Companion report: the top-3 recommendations in section 3 were
> implemented during iteration 6 (W25) and re-measured in
> [`perf_iter6_report.md`](perf_iter6_report.md) — incremental
> materialize (~196×), the directory-mtime signature guard (92 ms → 0.22 ms),
> and FTS coalescing. No iteration-7/8 perf report exists as of this writing.

---

## 1. Comparison table (Round 1 → Now @ 100k docs)

| Operation | Round 1 (`876dbc6`) | Now (`daacf9b`) | Δ | Verdict |
|---|---:|---:|---:|---|
| health_snapshot — first call (view build + materialize + COUNT) | 2.317 s | **2.525 s** (2.083–2.525 across 2 runs) | +9% | ~flat; now includes full-table materialize (replaces per-query JSONL re-parse) |
| health_snapshot — warm | — | **0.038 s** | new | — |
| COUNT(*) over captures | 135 ms @20k (W13, pre-mat) / 2.3 ms @20k (W13, post-mat) | **0.7 ms** @100k | — | materialized table: count is free |
| search "bitcoin" — first call (cold FTS build) | 12.475 s | **10.701 s** (build log 10.16 s + 0.54 s query) | −14% | win — FTS builds from materialized table, not 1,000 JSONL files |
| search "bitcoin" mode=auto — warm ×3 median | 0.558 s | **0.565 s** | +1% | flat (noise) |
| search prefix mode ("bitco") — warm ×3 median | — | **0.765 s** | new | slower than FTS (ILIKE scan) |
| search substring mode ("bitcoin") — warm ×3 median | — | **0.794 s** | new | slower than FTS (ILIKE scan) |
| term_frequency_over_time("bitcoin", 30d) | 1.308 s | **0.405 s** | **3.2× faster** | win (24,390 matching rows, 31 buckets) |
| detect_spikes("bitcoin", 30d) | 1.219 s | **0.412 s** | **3.0× faster** | win (0 spikes, flat series as in round 1) |
| extract_from_corpus(limit_docs=1000) | 1.555 s | **1.195 s** | 1.3× faster | win, but Python-regex bound (100 entities) |
| domain_rank(limit=20) | 1.614 s | **0.098 s** | **16.4× faster** | big win — multi-pass table scans |
| term_sentiment_over_time("bitcoin", 30d) | 1.970 s | **0.901 s** | **2.2× faster** | win — was just under the 2 s line |
| story_origins("bitcoin", 30d) | 1.199 s | **0.078 s** | **15.4× faster** | big win (0 stories, synthetic flat corpus — expected) |
| export_llm_dataset(limit=10000) | 1.496 s | **0.506 s** | **3.0× faster** | **verified: 10,000 rows**, 6,728,235 B (byte-identical to round 1) |
| search_with_diagnostics — warm ×3 median | — | **0.600 s** | +35 ms vs `search()` | +6% overhead (IDF diagnostics dict), acceptable |
| AlertEngine.evaluate_rules() — 5 rules (3 term_count + 2 term_spike) | — | **0.972 s** | new | ≈140 ms/rule — full-table regexp COUNT scan |
| write-batch refresh (append 500 docs → next health_snapshot) | 0.825 s @20k (W13) | **2.436 s** @100k | ~3× of W13's 20k | scales sub-linearly but is the #1 write hotspot |
| FTS refresh after write batch (first search post-append) | — | **7.4–7.9 s** (incremental append: delta INSERT + full `create_fts_index`) | new | deferred to next search; query after refresh: 0.43 s |
| persisted-FTS reopen (close → reopen → warm search) | — | **3.08 s** (FTS restore 0.0006 s + full rematerialize ~3 s) | new | restore itself is instant; reopen pays materialize |

**Flags (>2 s warm): none.** The only >2 s paths are cold/refresh: health
cold 2.5 s, cold FTS build 10.7 s, write-refresh 2.4 s, FTS refresh
post-write 7.4–7.9 s, process-reopen 3.1 s. Every steady-state analytical
op ≤ 1.2 s at 100k docs; search stays ~0.57 s.

---

## 2. Hotspot analysis (stage-level decomposition, same process/DB)

**Cold health_snapshot (2.525 s)** — `_refresh_views` breakdown via stage
probe:
| Stage | Time | Share |
|---|---:|---:|
| staging view (`read_json_auto` over 91 date globs, union_by_name) | 0.392 s | 15% |
| `CREATE OR REPLACE TABLE captures_materialized` (incl. ROW_NUMBER dedup) | ~1.7 s | 67% |
| captures view + COUNT + signature walk + connect | ~0.4 s | 16% |

**Warm search "bitcoin" (0.565–0.687 s)** — `cProfile` + standalone SQL
decomposition:
| Stage | Time | Share |
|---|---:|---:|
| scored BM25F candidate query (18 cols, score formula, LIMIT 200) | 0.187 s | 33% |
| FTS-join `COUNT(DISTINCT capture_id)` | 0.075 s | 13% |
| `_source_signature()` — glob+stat walk of 1,081 JSONL files, **per call** | 0.092 s | 16% |
| stem / num_docs / dict-df / avg-length queries | 0.020 s | 4% |
| Python `_rerank` (title-frac, phrase-frac, final_score over 200 rows) | 0.009 s | 2% |
| unaccounted (DuckDB native row materialization, plan variance, payload build) | ~0.18 s | 32% |

Note: the FTS path is a hand-written join over
`fts_main_captures_idx.{docs,terms,dict}` + Python BM25F formula
(duckdb_index.py:1758), not the built-in `bm25f` scalar. The standalone
queries measure 260 ms but the full call is 565–687 ms — run-to-run
variance lives in DuckDB-native execution of the two big queries plus the
per-call Python signature walk.

**Write path at 100k** — append 500 docs (one chunk, new day dir):
1. next `health_snapshot`: **2.436 s** = full `CREATE OR REPLACE TABLE`
   rebuild + dedup (W13's 825 ms was @20k; ~3× over 5× corpus growth —
   sub-linear but still a full-table rebuild per batch).
2. FTS is marked stale (`fts_built=False`); the **next search** pays
   `_try_incremental_fts_append`: the 500-row delta `INSERT … WHERE NOT
   EXISTS` is trivial, but DuckDB FTS has no partial-update API, so `PRAGMA
   create_fts_index` rebuilds the whole inverted index: **7.38–7.89 s**
   (retest: search wall 8.32 s = 7.89 s append + 0.43 s query).
   Post-refresh warm search: **0.43 s**.

**Alerts (0.972 s / 5 rules)** — each term_count is one `regexp_matches`
COUNT over `captures` (24 h window); each term_spike adds a 7-day baseline
query → 7 scans ≈ 140 ms/rule. Regex scan bound, not index bound (no regex
index exists).

**Persisted-FTS reopen (3.08 s)** — the on-disk fingerprint restore itself
is **0.0006 s** (`duckdb_fts_index_restored`, saves the full 10.2 s build —
the C3-T1 feature works). The 3 s remainder is the per-process in-memory
`_views_signature` (duckdb_index.py:503) forcing a full rematerialize on
the fresh connection — the same 2.5 s cold-health cost, not FTS.

**extract_from_corpus (1.195 s)** — fetch of 1,000 docs is fast now; the
time is pure-Python regex entity extraction over 1,000 docs (round-1 same
shape). Only 1.3× improved because the Python loop, not the data access, is
the floor.

---

## 3. Top-3 recommendations

1. **Incremental materialize instead of full-table rebuild per write
   batch** — `src/awareness/storage/duckdb_index.py:914`
   (`_materialize_captures`) runs `CREATE OR REPLACE TABLE … AS SELECT`
   over the whole corpus on every signature change. At 100k that is the
   2.436 s write-refresh, ~2/3 of cold-health (2.5 s) and ~3 s of the
   reopen cost. `captures_materialized` already has a unique index on
   `capture_id` (duckdb_index.py:946+): a delta path (`INSERT` new
   `capture_id`s from the staging view + stale-row handling by
   `content_hash` compare, mirroring the FTS append's H-10 check at
   duckdb_index.py:1170) turns the batch cost into ~50 ms-scale operations
   that scale with the write, not the corpus.

2. **Cut the per-query `_source_signature()` walk** —
   `src/awareness/storage/duckdb_index.py:652` glob+stats all ~1,081 JSONL
   files on **every** entry into `_connection_context`
   (duckdb_index.py:519), i.e. every search, health_snapshot, and analytics
   call: ~92 ms (16% of warm search, 0.3–0.5× of every sub-second analytics
   op). Guard the full walk behind a cheap outer check (e.g. `captures/`
   root dir mtime, or a per-day-dir mtime set compared only when
   `max(mtimes)` changed), or drop the walk to a
   `health_snapshot`/refresh cadence. ~90 ms off every warm op.

3. **Defer/coalesce the FTS inverted-index rebuild** —
   `src/awareness/storage/duckdb_index.py:1205` (`PRAGMA create_fts_index`
   in `_try_incremental_fts_append`): the delta INSERT is cheap but the
   whole inverted index is rebuilt per write batch (~7.5 s @100k), and the
   cost lands on the **first search after the batch** (8.3 s wall
   measured). Options: (a) coalesce tail batches into fewer FTS refreshes
   (batch watermark), (b) run the rebuild off the hot path (background
   thread / reopen time) so the next query never eats 8 s, (c) shard the
   FTS index by day/month so a batch only rebuilds one shard. Secondary:
   cold FTS build (10.7 s, −14% vs round 1) is the same rebuild — an async
   build + `fts_built` flag would hide it from the first query too.

---

## 4. Verdict

**The materialization win held at 100k.** Every warm analytical op
improved: term scans 1.31/1.22 → 0.41 s (3×), export 1.50 → 0.51 s (3×),
sentiment 1.97 → 0.90 s (2.2×), and the multi-pass heavyweights collapsed —
domain_rank 1.61 → 0.10 s (16.4×), story_origins 1.20 → 0.08 s (15.4×).
`COUNT(*)` is 0.7 ms vs W13's 135 ms pre-materialization at 5× fewer rows.
Warm search is unchanged at ~0.57 s and **no warm op exceeds 2 s** — round
1's only steady-state flag (sentiment 1.97 s) is gone. The trade is now
explicit: materialization moved the JSONL re-parse cost out of every query
and into a full-table rebuild per write (2.4 s @100k, was 825 ms @20k) plus
a deferred full FTS rebuild (7.5 s) that the first post-batch search pays.
For a write-light ingest (1,000-chunk corpora, batch appends) that trade
clearly paid; the remaining >2 s paths are all cold/refresh, and the next
levers are the incremental materialize + FTS coalescing above — not the
query path. Cold FTS build improved 14% (10.7 s), persisted-FTS restore is
effectively free (0.6 ms) after process restart, and the only new
regression-class item is the per-query 92 ms signature walk, which is pure
Python overhead independent of corpus size.

---

*Artifacts*: probe scripts (`perf_test_iter5.py`, `hotspot_probe.py`,
`fts_stage_probe.py`, `exact_sql_probe.py`, `append_retest.py`,
`cprof_search.py`), `results.json`, `run.log`; DuckDB metadata + export
retained in the audit workspace, corpus cleaned up.
