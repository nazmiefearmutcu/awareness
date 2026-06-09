# Awareness — Session Handoff (2026-06-08)

Paste this into a fresh session to continue the "awareness" remediation seamlessly.

---

## TL;DR for the next session

You are continuing a large, multi-cycle remediation of the **`awareness`** engine
(`~/Desktop/awareness`) — a single-process tool that ingests the public text web
(Common Crawl / FineWeb / RSS / GDELT) → extracts → SimHash-dedups → stores to
Iceberg/DuckDB/JSONL → serves a Typer CLI + FastAPI SPA. A 73-agent audit found
**39 verified bugs + 70 improvements**. Two user symptoms — *"search bitcoin → 2
results"* and *"no idea how to scrape the internet"* — are **both fixed and verified**.

All work is on branch **`feat/cycle1-make-it-work`** (off `main` at `69614e9`),
**not yet merged** (needs user consent — outward-facing). **283 tests green.**

Also read the persistent memory: `~/.claude/projects/-Users-nazmi/memory/awareness-cycle1-progress.md`.

## Environment & commands

- Repo: `/Users/nazmi/Desktop/awareness`. Python 3.13 in `.venv` (uv-managed).
- **Run tests with `PYTHONPATH=src`** — the uv editable `.pth` is unreliable in this
  env; `PYTHONPATH=src` is deterministic:
  - Gate: `PYTHONPATH=src .venv/bin/python -m pytest -q -m "not slow and not smoke"` → **240 passing** (baseline was 193; +47 new tests).
  - If `awareness` won't import: `uv pip install -e '.[dev]'` (then still use `PYTHONPATH=src`).
- Lint: `.venv/bin/python -m ruff check <file>`. **The project does NOT enforce
  ruff-clean** — the codebase carries many pre-existing `PLC0415` (intentional inline
  imports) and `S608` (code-derived SQL f-strings). Rule: add no NEW errors; match the
  file's own convention for `# noqa` (some inline imports are noqa'd, some S608 are not).
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

## Method (how this work has been executed)

`superpowers:subagent-driven-development`: a fresh implementer subagent per task,
**strict TDD** (write the failing test first → confirm it fails for the stated reason →
apply the fix → confirm green → run the full-suite gate → commit), then the controller
runs the suite as an objective gate and dispatches an adversarial reviewer for
substantive tasks. Plans are pre-written, fully-specified TDD task lists under
`docs/superpowers/plans/`. Specs under `docs/superpowers/specs/`. Audit under
`docs/superpowers/audit/` (the JSON has every bug's file:line + suggested fix).

## What's DONE (committed, green)

**Cycle 1 — "make it work" (Phases 1-3 of the roadmap):**
- **P1 Reliability** (`storage/state.py`, `workers/engine.py`, `tail/engine.py`): SQLite
  WAL+busy_timeout; `JobStatus` import fix; **atomic `claim_pending_tasks`** (no
  cross-process double-claim); orphaned-RUNNING reaper; don't mark job COMPLETED on stop;
  backoff retries (`next_attempt_at`).
- **P2 Scraping** (`sources/`, `util/http.py`): **real crawl-ID resolver**
  `sources/cc_crawls.py` (collinfo.json + bundled fallback) replacing the odd-ISO-week
  heuristic that fabricated non-existent crawl IDs; shared retrying HTTP `util/http.py`
  (`get_with_retries`, `RetryableHTTPError`); eTLD+1 domain-filter fix; configurable
  shards-per-crawl (setting `cc_wet_max_shards_per_crawl`, default 4); FineWeb fail-loud
  (`FineWebDependencyMissing`); transient-vs-404 discovery.
- **P3 Search** (`storage/duckdb_index.py`, `cli/main.py`): index `.jsonl.gz` (glob
  `*.jsonl*`); FTS rebuild on content-signature not row-count; order-insensitive field
  eligibility + OR-by-default prefix fallback; **removed the silent 30-day CLI window**
  via `_resolve_search_window` (all-time default + shows active window).
- **P3b** (`storage/duckdb_index.py`): captures-view resilience — `_staging_projection`
  /`_CAPTURE_COLUMNS` NULL-fill absent columns (via DESCRIBE) so one sparse chunk can't
  Binder-error the whole `captures` view out of existence.
- **P4 Schema/storage** (`storage/state.py`, `storage/gdrive.py`): `near_dup_hash`
  BigInteger; loud `_verify_dedup_schema` post-migration check; binary-safe gdrive
  multipart upload (`_build_multipart_body`/`_file_mime`, read_bytes).

**Cycle 2 — math & systems (the user's special-interest area; Phases 4-5):**
- **P1 Dedup banding** (`storage/state.py`): re-banded **16×8 → 32×4** so the Manku/Jain
  pigeonhole guarantee (`bands-1=31`) covers the default merge threshold (24) — near-dups
  within the threshold are now retrieved **exactly**, not probabilistically.
  `DEFAULT_NEAR_THRESHOLD=24` named in `dedup/engine.py`; invariant test locks
  `bands-1 ≥ threshold`.
- **P2 Calibration + IDF** (`dedup/calibration.py`, `util/hashing.py`): exact-binomial FPR
  calibration (`fpr_at_threshold`, `calibrate_threshold`; `calibrate_threshold(128,1e-6)=36`
  vs the conservative default 24 → recall headroom); optional `idf` callable hook in
  `simhash128` (backward-compatible).
- **P5a Metrics** (`obs/metrics.py`): Vitter Algorithm-R reservoir sampling (was biased
  first-256) + p50/p95/p99.
- **Benchmark re-measured + README updated** (commit `ef591bd`): ran `benchmarks.bench_simhash`
  offline against the real 32×4 engine — precision still **1.00**, default F1 0.848/recall
  0.736, tuned (Hamming≤32) F1 0.973/recall 0.947. README near-dup numbers are CURRENT.
- **P3 BM25F re-ranking** (`storage/duckdb_index.py`, commits `6796330`/`0b360af`/`9951f1a`):
  DuckDB FTS scores title+text as one blob, so a pure `_rerank` re-orders the top-`max_results`
  BM25 candidates by **independent multiplicative factors** — title field-boost
  (`1+Wt·title_hit_frac`), length damping ∈`[floor,1]`, optional recency (OFF by default;
  injected/derived ref-time ⇒ deterministic, never the wall clock). The FTS path now fetches a
  candidate window by raw BM25, re-ranks, then slices `[offset:offset+limit]`;
  `total`/`mode`/`ranked`/row-schema/`max_results`-cap are all unchanged, and it degrades to
  pure BM25 order when factors are neutral. Plan `plans/2026-06-08-awareness-cycle2-bm25f-ranking.md`;
  19 new tests (16 pure + 3 integration); 3-lens adversarial review + final review APPROVED.

**Cycle 1 — P3b search availability (process-wide index singleton):**
- **Index singleton** (`api/server.py`, `storage/duckdb_index.py`, commits `7305194`/`8e860a6`):
  the API was building a NEW `DuckDbIndex` per request, so the FTS index rebuilt every `/search`
  and concurrent searches collided on DuckDB's single-writer lock during the rebuild. Now one
  shared instance via `_get_index()` (double-checked locking on `_State.index` + `_index_lock`,
  closed on lifespan shutdown) serves all six endpoints; FTS builds once (it already memoizes
  `_fts_built_signature` + serializes behind its `RLock`). Added lock-guarded
  `DuckDbIndex.related()` so `/related` no longer runs a query on the raw conn outside the lock.
  Plan `plans/2026-06-09-awareness-cycle1-fts-singleton.md`; 6 new tests; review APPROVED.
  (Note: the full non-slow suite can take minutes when offline — `connect()` does network
  `INSTALL iceberg/fts` per fresh index; unrelated to this change.)

**Cycle 2 — P4 language detection & WET quality:**
- **Confidence-aware LID + Gopher/C4 WET filter** (`normalize/text.py`, `normalize/quality.py`,
  `config/settings.py`, `sources/commoncrawl_wet.py`, commits `33cab11`/`574def9`/`f53ff55`/`6d333da`/`b5832b1`):
  `detect_language` now uses langdetect's `detect_langs()` and suppresses sub-`0.50`-confidence
  guesses to `None` (ambiguous text no longer gets a confidently-wrong label); `detect_language_conf`
  returns `(lang, conf)`. New pure `gopher_quality()` (Gopher/C4 heuristics) drops
  boilerplate/symbol-spam WET records, gated by `settings.wet_quality_filter` (default on) with a
  `cc_wet.quality_filtered{crawl_id}` metric. **Adversarial review caught a real plan bug:** the
  English-leaning filter originally ran BEFORE LID and would have dropped non-English text — fixed
  to run downstream of LID and only judge `lang=="en"` (German-canary test pins it). **No new
  dependency** (langdetect already present; backend stays swappable). 25 new tests; final review APPROVED.

## What REMAINS (specced, prioritized — pick up here)

The recommended immediate move is one of: **(a) finish/merge the branch**
(`superpowers:finishing-a-development-branch` — Cycle 1 + Cycle 2 math core is a
shippable independent block, needs user consent), or **(b) continue** with:

1. ~~**Cycle 2 P3 — BM25F ranking.**~~ **✅ DONE** (commits `6796330`/`0b360af`/`9951f1a`).
   Also this session: item 5's **GDELT slot-math** was verified — **✅ clean, no 2nd fabricated-ID
   bug** (details under item 5). Recommended next pick: item 2 (FTS singleton — API-side availability).
2. **Cycle 1 P3b remainder (search availability/correctness):** ~~FTS index process-wide
   singleton + serialized rebuild~~ **✅ DONE** (commits `7305194`/`8e860a6`). **STILL REMAINING
   in P3b:** inclusive end-of-day across `/captures`,`/search`,`/inspect`,`/counts`; pagination
   corruption; phrase/prefix/fuzzy.
3. ~~**Cycle 2 P4** — confidence-aware language detection + Gopher/C4-style WET quality filter.~~
   **✅ DONE** (commits `33cab11`/`574def9`/`f53ff55`/`6d333da`/`b5832b1`). Future: stronger LID
   backend (fastText/CLD3) behind the same `detect_language*` surface; quality filter for non-WET sources.
4. **Cycle 2 P6 (systems)** — persisted/incremental FTS; streaming WET parse (bounded
   memory); pooled httpx client + global fetch concurrency; Iceberg re-partition
   `month(fetch_ts)+source_type` + compaction; crash-safe flush + idempotent appends;
   `/metrics` export + per-fetch tracing.
5. **Cycle 1 Plan 2b (scraping hardening, before any breadth increase):** per-domain
   rate-limiter delay race; robots crawl-delay + UA consistency; seed discovery
   (sitemaps/robots); **GDELT slot/time-math** ✅ VERIFIED CLEAN (2026-06-08): the flooring
   `minute−(minute%15)` lands exactly on real GDELT slots {00,15,30,45} (e.g. `…003000.gkg.csv.zip`),
   missing slots 404→logged not fatal, and `latest_gkg_slot` is unit-tested — so this is NOT a
   2nd fabricated-ID bug. Only remaining nit: `_quarter_hours` (backfill range-walker) shares the
   identical flooring but lacks a direct test (cheap regression-lock). Job-wide fan-out budget.
6. **Cycle 1 Plan 4b:** tail_recrawl/warc_repair honor text_min/max_chars + charset;
   one-command `quickstart`; zero-task backfill warning; search empty-state "why".
7. **Math follow-ups:** raise `DEFAULT_NEAR_THRESHOLD` toward the calibrated 36 — BUT this
   needs the banding raised to keep `bands-1 ≥ threshold` (would need ~37 bands) AND a
   benchmark re-measure; **deliberately held** per the audit's risk note (don't change
   dedup grouping before the benchmark harness validates it). Also: corpus-IDF
   document-frequency store to feed the new `simhash128` idf hook; union-find canonical
   cluster resolution (transitive near-dup folding; needs a doc_id→canonical store).
8. **Cycle 3:** SPA Settings editable; fetch_ts/observed_ts/published_ts disentangle; URL
   canonicalization tightening; JSONL+Iceberg double-count reconcile.

## Gotchas / conventions

- **Tests that write JSONL fixtures** historically needed all 29 canonical columns (the
  captures view bound them) — P3b now NULL-fills missing ones, but full-row fixtures
  remain the norm (copy the key set from `tests/unit/test_search_matching.py`).
- **`benchmarks.bench_simhash` is fully offline** (synthetic corpus, fixed seeds) and runs
  the REAL `DedupEngine` — use it to re-measure near-dup numbers after any
  banding/threshold change. Other `benchmarks/bench_*.py` need network/extras (`'.[bench]'`).
- **Don't raise the dedup merge threshold** without raising banding to preserve the
  pigeonhole invariant AND re-measuring the benchmark (the invariant test will fail otherwise).
- The two cross-cutting seams introduced: `util/http.py` (all fetchers should route
  through it) and the search-default resolver (`_resolve_search_window` in `cli/main.py`).

## First commands for the new session

```bash
cd /Users/nazmi/Desktop/awareness
git status && git log --oneline -8
PYTHONPATH=src .venv/bin/python -m pytest -q -m "not slow and not smoke"   # expect 283 passed (can take minutes offline: connect() does network INSTALL iceberg/fts)
ls docs/superpowers/plans/   # the executable TDD plans
```
Then either invoke `superpowers:finishing-a-development-branch` (to merge) or write/execute
the next plan via `superpowers:subagent-driven-development`.
