# Awareness — Session Handoff (2026-07-13)

Paste this into a fresh session to continue continuous-loop development seamlessly.

Also read:

- `docs/superpowers/loop/CONTINUOUS_LOOP.md` — rotation + task table
- `docs/superpowers/loop/STATE.json` — machine-readable loop cursor (may lag latest commits)

---

## CURRENT STATE (continuous loop — read this first)

| | |
|---|---|
| **Branch** | `loop/continuous-dev` |
| **Base** | synced from `origin/main` **`a0ba789`** (Merge PR #22 feat/benchmarks) |
| **HEAD tip** | ~`5dafef7` (check `git rev-parse --short HEAD`) |
| **Ahead of main** | **~70 commits** (`origin/main..HEAD` was **71** at last handoff refresh) |
| **Pushed?** | **No** — local only; do not push unless user asks |
| **Unit suite** | **~480+ collected** (`tests/unit` collects **481**; gate: `not slow and not smoke`) |
| **Stop** | User says stop / `touch .ralph/STOP` / explicit cancel |

### What this branch fixed / shipped (verify: `git log origin/main..HEAD`)

**API / index**

- **DuckDbIndex process-wide singleton restored** (`_get_index` + lock; reset after path-related settings changes)
- Staging: **index `.jsonl.gz`** + **exclude `.tmp`** chunks from DuckDB globs
- Captures list: **`unique=content|group`** collapse param
- CLI: export captures to JSONL with optional unique fold
- **Persisted FTS restore + append-only `captures_idx`** (C3-T1) — no full rebuild every open
- BM25 field avg-length **memoized per index signature**

**Dedup / re-fetch prevention**

- **RSS/GDELT unified tail partition keys** (no double-fetch across feed sources)
- **URL fetch gate** before `tail_recrawl` HTTP when canonical URL already fetched
- **Stronger news URL canonicalization** — strip `www.`, normalize trailing path slashes, expand tracking-param strip (`utm_*` + common ad/click IDs) so fetch-gate / tail keys collapse article variants
- **32×4 SimHash banding live** (pigeonhole guarantee for Hamming ≤24 / `DEFAULT_NEAR_THRESHOLD`)
- **Tight near-dup skip-store** at Hamming ≤12 (optional drop before persist + metrics)
- **Union-find parent resolution** for near-dup clusters (transitive fold; related captures share parent)

**Search**

- Collapse results by `parent_doc_or_dup_group`
- **Phrase quotes** (`"exact phrase"` → `mode=phrase`)
- Empty-result **diagnostics** + SPA hints (mode/corpus/window surfaced)
- **OR multi-term prefix** fallback for consistent auto mode
- **FTS rebuild on content signature** (not row-count alone) — detects same-count content swap
- Order-insensitive FTS field eligibility + BM25F re-rank path wired
- **Optional `published_ts` recency boost** in ranking (settings-gated; deterministic when on)
- **Domain facets** on search results (`facets.sources`) — CLI summary + SPA source chips
- **Pagination correctness** after collapse + re-rank
- **Inclusive end-of-day** date windows
- **Long-lived DuckDB connection reuse** under lock (search path)
- NULL-fill missing staging columns so sparse JSONL cannot kill the `captures` view

**Scrape / feeds / news (recent wave)**

- **News/RSS extract floor** — honor `text_min_chars` / max on tail + warc_repair; lower floor via `text_min_chars_news` (default **80**) so short news stubs are kept
- **Transient HTTP retries** in tail_recrawl and feeds (shared retry path)
- Non-200 feed/sitemap fetches **logged** (no silent empty)
- **Loud zero-task backfill warning** with per-source reasons
- Consistent **`user_agent` from settings** across fetch paths
- **Process-wide global fetch concurrency cap**
- Robots **crawl-delay** honored under per-domain concurrency
- **Sitemap discovery** from robots `Sitemap:` directives
- Stable ordered `seen_urls` window for feed checkpoints

**Common Crawl WET**

- **Streaming WET parse** with bounded memory (C3-T3)
- **Mid-shard resume** via checkpoint `last_record_id`
- `cc_wet_max_shards_per_crawl` so WET adapter registers; eTLD+1 domain filter

**Also landed on this branch (bugfixes / UX / ops)**

- LID `detect_langs` + confidence gate restored; gdrive binary multipart; idf hook on `simhash128`; CLI search-window defaults; version **0.2.0** + banding docs; SPA mode controls, term highlight, settings/dashboard KPIs for fetch-skip / tight-near-dup; default hide-duplicates on Captures browse; Redis lock tests skip when unavailable; clearer near-dup worker logs with Hamming distance; etc.

### Remaining (good next picks)

Loop Cycle 3+ and older backlog (not exhaustive):

1. **Plan 4b UX** — quickstart, empty-state “why”, broader text_min/max knobs where still missing
2. **Search leftovers** — optional fuzzy; more facet dimensions; tune recency defaults with corpus measure
3. **Systems remainder** — pooled httpx polish, Iceberg compaction, crash-safe flush, `/metrics` export
4. **Math follow-ups** — threshold toward calibrated 36 only with banding + benchmark re-measure; corpus-IDF store for `simhash128` idf hook
5. **Cycle 3 product** — SPA Settings editable; fetch_ts/observed_ts/published_ts disentangle; JSONL+Iceberg double-count reconcile
6. **Ship decision** — branch is ~70 ahead of main and **unpushed**; merge/push only with user consent

> Note: `loop/CONTINUOUS_LOOP.md` / `STATE.json` may lag HEAD (still mention ~55 commits / pending streaming WET). Prefer this HANDOFF + `git log origin/main..HEAD` for truth. Streaming WET + WET resume, news floor, URL canon, retries, facets, and recency are **done** on tip.

### Environment & first commands

```bash
cd /Users/nazmi/Desktop/awareness
git status && git branch --show-current   # expect loop/continuous-dev, not pushed
git log --oneline origin/main..HEAD | head -25
PYTHONPATH=src .venv/bin/python -m pytest -q -m "not slow and not smoke"   # ~480+ unit; can take minutes offline (DuckDB INSTALL)
# unit-only:
PYTHONPATH=src .venv/bin/python -m pytest -q tests/unit
```

- Repo: `/Users/nazmi/Desktop/awareness`. Python 3.13 in `.venv` (uv-managed).
- **Always run tests with `PYTHONPATH=src`** (editable install `.pth` is unreliable here).
- If import fails: `uv pip install -e '.[dev]'` then still use `PYTHONPATH=src`.
- Lint: `.venv/bin/python -m ruff check <file>`. Project does **not** require repo-wide ruff-clean; add no new errors.
- Method: continuous loop rotation — bug hunt → search → dedup → features; subagent-driven TDD; **commit per task; do not push unless asked**.
- Loop docs: `docs/superpowers/loop/`. Plans/specs/audit: `docs/superpowers/{plans,specs,audit}/`.

### Gotchas (still true)

- JSONL fixtures: full canonical columns remain the norm (`tests/unit/test_search_matching.py`); missing cols are NULL-filled but don’t rely on that in tests.
- `benchmarks.bench_simhash` is offline and uses the real `DedupEngine` — re-measure after banding/threshold changes.
- **Don’t raise merge threshold** without raising banding (`bands-1 ≥ threshold`) and re-benchmarking.
- Cross-cutting seams: `util/http.py` for fetchers; `_resolve_search_window` for CLI search defaults; `util/urls.py` for news URL identity / fetch gate.
- News extracts use **`text_min_chars_news`** (default 80), not the long-form `text_min_chars` floor — short RSS stubs are intentional.

---

## Historical handoff (2026-06-08)

The sections below describe the earlier `feat/cycle1-make-it-work` remediation wave.
Treat **numbers, branch names, and “remaining” lists as historical** unless re-verified;
the continuous-loop table above is authoritative for current branch state.

### Product context

You are continuing a large, multi-cycle remediation of the **`awareness`** engine
(`/Users/nazmi/Desktop/awareness`) — a single-process tool that ingests the public text web
(Common Crawl / FineWeb / RSS / GDELT) → extracts → SimHash-dedups → stores to
Iceberg/DuckDB/JSONL → serves a Typer CLI + FastAPI SPA. A 73-agent audit found
**39 verified bugs + 70 improvements**. Two user symptoms — *"search bitcoin → 2
results"* and *"no idea how to scrape the internet"* — were fixed in Cycle 1 and further
hardened on `loop/continuous-dev`.

### Method (how this work has been executed)

`superpowers:subagent-driven-development`: a fresh implementer subagent per task,
**strict TDD** (write the failing test first → confirm it fails for the stated reason →
apply the fix → confirm green → run the full-suite gate → commit), then the controller
runs the suite as an objective gate and dispatches an adversarial reviewer for
substantive tasks. Plans are pre-written, fully-specified TDD task lists under
`docs/superpowers/plans/`. Specs under `docs/superpowers/specs/`. Audit under
`docs/superpowers/audit/` (the JSON has every bug's file:line + suggested fix).

### What's DONE (committed historically + re-landed/extended on loop branch)

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
- **Benchmark re-measured + README updated**: ran `benchmarks.bench_simhash`
  offline against the real 32×4 engine — precision still **1.00**, default F1 0.848/recall
  0.736, tuned (Hamming≤32) F1 0.973/recall 0.947. README near-dup numbers are CURRENT.
- **P3 BM25F re-ranking** (`storage/duckdb_index.py`): DuckDB FTS scores title+text as one
  blob, so a pure `_rerank` re-orders the top-`max_results` BM25 candidates by
  **independent multiplicative factors** — title field-boost, length damping, optional
  recency (OFF by default). Plan `plans/2026-06-08-awareness-cycle2-bm25f-ranking.md`.

**Cycle 1 — P3b search availability (process-wide index singleton):**
- **Index singleton** (`api/server.py`, `storage/duckdb_index.py`): shared `_get_index()`
  (double-checked locking); FTS builds once behind memoization + `RLock`. Restored again
  on the continuous-loop branch after main drift.

**Cycle 2 — P4 language detection & WET quality:**
- **Confidence-aware LID + Gopher/C4 WET filter** (`normalize/text.py`, `normalize/quality.py`,
  `sources/commoncrawl_wet.py`): sub-0.50 confidence → `None`; `gopher_quality()` only on
  `lang=="en"` (post-LID). No new dependency.

### What REMAINS (historical list — re-check against CURRENT section)

The recommended immediate move is one of: **(a) finish/merge the branch**
(needs user consent — outward-facing), or **(b) continue** the continuous loop with
items under **Remaining** above. Historical detail:

1. ~~**Cycle 2 P3 — BM25F ranking.**~~ **✅ DONE**
2. **Cycle 1 P3b remainder:** ~~singleton / FTS signature / end-of-day / phrase / collapse~~
   largely **✅ on loop branch**. Still open: pagination corruption; optional fuzzy.
3. ~~**Cycle 2 P4** — LID + WET quality.~~ **✅ DONE**
4. **Cycle 2 P6 (systems)** — persisted/incremental FTS; streaming WET parse; pooled httpx;
   Iceberg compaction; crash-safe flush; `/metrics` export.
5. **Cycle 1 Plan 2b (scraping hardening):** limiter race; robots crawl-delay + UA; seed
   discovery; GDELT slot math ✅ verified clean; job-wide fan-out budget.
6. **Cycle 1 Plan 4b:** tail_recrawl/warc_repair honor text_min/max_chars + charset;
   one-command `quickstart`; zero-task backfill warning; search empty-state (partially improved).
7. **Math follow-ups:** raise `DEFAULT_NEAR_THRESHOLD` toward calibrated 36 only with banding
   + benchmark; corpus-IDF store; ~~union-find~~ **✅ DONE on loop branch**.
8. **Cycle 3:** SPA Settings editable; fetch_ts/observed_ts/published_ts disentangle; URL
   canonicalization tightening; JSONL+Iceberg double-count reconcile.
