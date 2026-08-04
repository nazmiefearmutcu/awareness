# Architecture

## Goal

Build a **public text internet awareness engine** with two modes:

- **BODY** — backfill historical public text from a chosen start date up to now.
- **TAIL** — capture newly published public text from `start_time` until you stop it.

Storage is text-only. Everything else (URLs, timestamps, hashes, provenance,
language) is metadata.

## Why a layered tier strategy

A monolithic crawler is the wrong shape for the public text web because:
- It scales linearly with politeness, not with hardware.
- It cannot meaningfully backfill years of history within a session.
- It duplicates work that organizations like Common Crawl and HuggingFace
  have already done at very high cost.

We use three tiers:

| Tier | Adapters | Role |
| --- | --- | --- |
| **A — Historical bulk text** | `commoncrawl_wet`, `cc_index`, `fineweb` | Cheap, parallel, partitioned text-first corpora |
| **B — Live discovery surfaces** | `feeds` (RSS/Atom/Sitemap), `tail_recrawl`, `gdelt` | URL-level new-content discovery + polite fetch |
| **C — Targeted repair** | `warc_repair` | Byte-range WARC fetches for specific records |

Each tier produces the **same `DocCapture` envelope**, so the downstream
pipeline (dedup, storage, query) is unified.

## Control flow

```
                     ┌───────────────┐
              CLI ──►│   Planner     │     translates BackfillRequest
              API ──►│               │     into source-native partitions
                     └──────┬────────┘
                            │  TaskState rows in state DB
                            ▼
                     ┌───────────────┐
                     │ Worker Engine │     pool of asyncio workers
                     │  (bounded)    │     dedup → flush → checkpoint
                     └──────┬────────┘
                            │  PartitionSpec
                            ▼
                     ┌───────────────┐
                     │ Source        │     each yields DocCapture
                     │ Adapter       │     and may enqueue sub-partitions
                     └──────┬────────┘
                            │  DocCapture
                            ▼
                     ┌───────────────┐
                     │ Dedup Engine  │     content_hash + simhash + url
                     └──────┬────────┘
                            │
                            ▼
                ┌───────────────┐    ┌────────────────┐
                │ JSONL staging │ ─► │ Iceberg (Parq) │
                │ (atomic)      │    │ optional sink  │
                └───────┬───────┘    └────────────────┘
                        │
                        ▼
                ┌───────────────┐
                │   DuckDB      │   range query + counts + inspect
                └───────────────┘
```

### Sub-partitioning

Discovery adapters (CC index, feeds, GDELT) don't yield text directly. Their
`run_partition()` populates `context.extras["enqueue"]` with new
`PartitionSpec` records. After the task completes, the worker's
`enqueue_subpartitions()` call adds them as new pending tasks. This keeps the
data plane simple — only one queue, one worker pool, one schema.

### Resume and idempotence

- Tasks are uniquely keyed on `(job_id, partition_key)`.
- Adapters' `run_partition()` accept a `context.checkpoint` dict; they may
  read it (e.g. `row_index`, `seen_urls`) and write to it during the run.
- On task completion, the checkpoint is persisted in the state DB.
- Failed tasks are re-queued with the same partition_key; on `attempts >=
  max_retries`, the task is dead-lettered to the DLQ table.

## Storage layers

| Layer | Where | Used for |
| --- | --- | --- |
| Staging | `data/jsonl/captures/Y/M/D/*.jsonl` | Atomic source-of-truth |
| Durable | `data/iceberg/awareness/captures/` | Iceberg table for analytics |
| State | `data/state/awareness.sqlite` | Jobs, tasks, manifests, dedup |
| Query | `data/duckdb/metadata.duckdb` | DuckDB view over JSONL + (Iceberg) |
| Cache | `data/warc/` | WARC/WET shards while parsing |
| DLQ | `data/dlq/` and `dlq` table | Repeatedly-failing payloads |

The JSONL staging is written first **and is always consistent on disk** even
if PyIceberg fails. The compaction path can lift JSONL → Iceberg later.

## Identity & dedup

- `doc_id = xxhash3_128(canonical_url + content_hash)`
- `capture_id = xxhash3_128(doc_id + observed_ts + source_locator)`
- `content_hash = xxhash3_64(normalize(text))`
- `near_dup_hash = simhash128(text)` — used with a 32×4-bit band index
  (Manku/Jain pigeonhole) for O(1)-per-band lookup of near-duplicates.

Dedup labels captures `NEW`, `REVISION`, `EXACT_DUP`, `NEAR_DUP`. The worker
skips durable storage for `EXACT_DUP` / `REVISION` and for tight `NEAR_DUP`
(Hamming ≤ 12); looser `NEAR_DUP` and `NEW` still persist for provenance.
Downstream readers fold captures into canonical documents with
`WHERE doc_id = parent_doc_or_dup_group`. Search collapses hits by
`content_hash` so top-K never shows syndicated copies twice.

## Parallelism

- **Source-level**: one adapter per source kind.
- **Shard-level**: each adapter emits multiple PartitionSpecs (e.g. one per
  CC shard, one per feed, one per GDELT 15-min slot).
- **Time-partition**: backfills enumerate ISO weeks → CC crawl IDs and 15-min
  GDELT slots.
- **Task-level**: the worker engine runs N partitions concurrently via an
  `asyncio.Semaphore(concurrency)`.
- **Per-domain politeness**: `PerDomainLimiter` enforces concurrency and
  spacing per registered domain. robots.txt crawl-delay overrides the global
  default when present.
- **Pipeline-stage**: extraction runs inside `loop.run_in_executor` so the
  event loop is never blocked by heavy parsing.

## Compliance boundaries

- **Public-only**: every adapter targets public, openly-accessible surfaces
  (Common Crawl, FineWeb, public RSS/Atom, public sitemaps, GDELT).
- **Robots.txt**: enforced before live fetches via `RobotsCache`. Disallowed
  URLs return `RobotsDecision.DISALLOWED` and skip persistence.
- **No login / no paywall**: there is no credential store; nothing
  authenticates.
- **No private APIs**: only public, documented endpoints.
- **No binary persistence**: HTML / WARC bytes live only in transient caches;
  durable storage is text + metadata.

## Failure model

| Failure | Where | Behavior |
| --- | --- | --- |
| Adapter exception | `_run_task` | task → PENDING (retry), DLQ at max_retries |
| JSONL write fail | `_flush` | logged warning; in-memory buffer cleared |
| Iceberg append fail | `_flush` | logged warning; JSONL remains source of truth |
| HTTP timeout/5xx | adapter | per-task retry with backoff |
| robots.txt disallow | adapter | capture skipped, counter incremented |
| Stop signal | engine | drain buffer → close writers → exit |

## Optional production stack

The compose file `ops/compose/docker-compose.yml` runs:
- **Postgres** → state DB (swap `AW_STATE_DB_URL`)
- **MinIO** → S3-compatible warehouse (swap `AW_ICEBERG_WAREHOUSE`)
- **Redpanda** → event bus for multi-process workers (future)
- **ClickHouse** → analytics over the same Parquet files

Code paths are identical; only env vars differ.

## Feature subsystems

Read-side subsystems layered on the capture corpus, served by the same
single-process FastAPI app (no extra services):

- **Analytics** (`/analytics/*`): term frequency, spikes, top terms, domain /
  language breakdowns, co-occurrence — pure reads over the DuckDB index,
  available whenever the index is ready (503 otherwise).
- **Alerts** (`/alerts/*`): SQLite rule store (`<data_dir>/alerts.db`), a
  rolling 7-window spike baseline, webhook delivery with retry. The
  **AlertRunner** loop evaluates rules periodically inside the API process
  when `AW_ALERTS_AUTOSTART=1` (default off); it is idempotent (start/stop
  safe), isolates per-tick errors, and shares one process-wide `AlertStore`
  connection closed on shutdown. `awareness alerts check` evaluates on
  demand without the runner.
- **Entities / source-intel / consume**: heuristic NER aggregation, domain
  quality + replication scoring, and LLM-export / weekly-digest generation.
  The digest is available as an API endpoint (`/consume/digest[/markdown]`)
  and as the `awareness digest` CLI (`--days --markdown --json --out`); both
  share `awareness.consume.digest`.

### Iteration 2 (2026-08-04)

Read-side extensions from the Round-2 loop (`876dbc6`, `8c53af4`), still one
FastAPI process, still zero extra services.

- **Sentiment** (`/sentiment/*`): a finance lexicon (189 pos / 251 neg) with
  negation and intensity scoring over the captured text. Exposes per-term
  sentiment over time (`/sentiment/term`) and a market-heat snapshot
  (`/sentiment/heat` — volatility + 7-day trend). Pure Python reads over the
  DuckDB index; `awareness.sentiment.{engine,lexicon,router}`.
- **Origin** (`/origin/*`): breaking-news origin tracking built on the dedup
  groups — first publisher and lead minutes per story (`/origin/stories`),
  plus a publisher-firsts ranking (`/origin/publishers`). Identifies who
  broke a story and how long until the replicas followed,
  `awareness.origin.{engine,router}`.
- **GDELT bridge** (`/gdelt/*`): a DOC 2.0 cross-reference for the local
  corpus. Per-day external article counts are cached on disk (6 h TTL); every
  GDELT failure degrades to an empty series with a structured-log warning,
  never an exception, so the bridge is safe when offline. Serves
  local-vs-GDELT correlation (`/gdelt/compare`) and coverage-gap detection
  (`/gdelt/gaps`), `awareness.gdeltx.{engine,router}`.
- **Corpus intelligence** (`/corpus/*`): a term × domain topic matrix
  (`/corpus/topic-matrix`) and a corpus-quality snapshot (`/corpus/quality` —
  duplicate / near-dup ratios, language rollup, capture rate per day),
  `awareness.corpusx.{engine,router}`.
- **Materialized corpus table**: the deduped `captures` union is materialized
  into a real `captures_materialized` table with a unique index on
  `capture_id`; every query (COUNT, search, facets, FTS staleness joins) runs
  against indexed table storage instead of re-parsing JSONL per query (365×
  on `COUNT(*)`). Refresh semantics: a **full rebuild on source-signature
  change** only — the `captures` view reads the table, so the query surface
  stays byte-identical for callers. JSONL remains the durable staging layer.
- **Entity-network SPA**: the dashboard gained an entity network band — a
  concentric root + ring layout computed in pure JS, SVG edges, click-to-
  rebuild from `/entities/co-occurring` — plus an Alerts view (rules CRUD,
  active toggle, test-run, firings log) and feed-health KPIs.
- **CLI**: `awareness trends` (zero-filled series, z-score spike marks,
  `--chart` sparkline, `--sentiment` column), the `awareness x` group
  (sessions / show / create over the X-scraper store), and
  `awareness digest --email` (SMTP delivery via `--smtp-*` flags or
  `SMTP_*`/`EMAIL_FROM` env, graceful failure).

### Iteration 3–6 (2026-08-04)

Performance, persistence, and X-surface extensions from the rest of the
Round-2 loop (`fbd16a9` → `e651d3b`), still one FastAPI process, still zero
extra services.

- **Incremental materialization + signature guard + FTS coalescing**: the
  deduped `captures_materialized` refresh is now delta-first — a pure
  addition batch INSERTs only the changed chunks (verified by a signature
  diff), falling back to a full rebuild on removal/edits/Iceberg unions,
  which cut a 20k-doc refresh from 123 ms to 0.6 ms (~196×). A 3-level
  directory-mtime guard (`_captures_dir_summary`) short-circuits the per-file
  signature walk when nothing changed (92 ms → 0.22 ms @100k). Because
  DuckDB FTS has no partial-update API, dirty indexes defer their rebuild
  inside a 30 s coalescing window (module constant, 0 disables for tests),
  degrading search to the table-backed prefix/substring path until N batches
  coalesce into one rebuild. Details in
  [`docs/benchmarks/perf_iter6_report.md`](benchmarks/perf_iter6_report.md).
- **Saved-search store**: `awareness/savedsearch/` is a SQLite-backed store
  of named queries — CRUD + pin + run under `/saved/*`, the `awareness
  saved list|add|rm|run` CLI group, and a SPA Saved view with a save control
  on the search box; runs reuse the same search path as ad-hoc queries, so
  saved results stay byte-identical to typing the query.
- **X simulation + analysis**: `xscraper/simulate.py` generates deterministic
  tweets for a session from a `(seed, n_tweets)` pair — seeded RNG, template
  pool drawn from the sentiment lexicon, no network — stored through the
  regular `store_tweets` path so PK dedup still applies. `xscraper/analyze.py`
  aggregates a session into author counts, top terms, per-tweet lexicon
  sentiment, a per-day timeline, and engagement totals (zeroed dict on empty
  sessions). Both surface as `POST /x/sessions/{id}/simulate`,
  `GET /x/sessions/{id}/analysis`, and the `awareness x simulate|analyze`
  CLI.
- **Dedup token-sketch guard**: `dedup_near` rows now carry a token-set
  sketch (`token_hash` + unique-token count) and a band candidate merges only
  when the sketches agree — count-ratio ≤ `NEAR_DUP_MAX_TOKEN_COUNT_RATIO`
  (0.5), and exact `token_hash` match when both docs are short
  (`NEAR_DUP_SHORT_DOC_MAX_TOKENS` = 200). This kills boilerplate-template
  merges (distinct articles sharing a footer) while genuine near-dups and
  legacy NULL-sketch rows (Hamming-only) still merge.
- **IDF diagnostics surface**: `search_with_diagnostics()` returns
  `kept_terms` / `dropped_terms` / `mode` / `idf_threshold`, and dropping a
  query term below `search_idf_threshold` (default 1.0) emits a WARNING with
  the query field — the search API and CLI can explain why a term
  contributed nothing, instead of silently pruning it.

### Iterations 7–8 (2026-08-04)

SPA, export, and verification extensions from the last two Round-2
iterations (`e4b1417`, `7d46372`), still one FastAPI process, still zero
extra services.

- **X sentiment trend + CSV export**: `xscraper/analyze.py` now aggregates a
  per-day `sentiment_trend` (positive/negative counts + mean score per day)
  alongside the existing author/term/timeline/engagement blocks, and
  `export_tweets_csv()` writes a session's tweets to CSV atomically with
  proper quoting. Both surface through `GET /x/sessions/{id}/analysis`,
  `GET /x/sessions/{id}/tweets.csv` (download attachment), and the
  `awareness x analyze` / `awareness x export` CLI — `x analyze` prints the
  daily trend as a sparkline.
- **SPA firing detail + saved band**: the Alerts view's firing log rows are
  expandable (rule_id, count/threshold, local + UTC timestamps, view-rule
  highlight) with the window raised from 20 to 50 firings and a Refresh
  button; the dashboard gained a Saved-search band with chips and inline run
  results, and the standalone Saved view sits behind the same store
  (`saved_searches.db`).
- **E2E smoke 11 stages**: `scripts/e2e_smoke.py` grew from 8 to 11 stages
  — saved-search CRUD + run, X create/simulate/analyze/tweets, and the
  `report` + `alerts history` CLI paths were added after the original
  digest/export tail; the wrapper asserts each new stage and still exits
  non-zero on the first failure with no network.
- **Iteration-8 hardening**: FTS incremental append treats `fetch_ts`
  mismatch as stale (W28), no-op refreshes stop re-arming the coalescing
  window, and delta materialize forces a full rebuild when a changed chunk
  shrank — the three W28 correctness fixes that closed the iteration-6
  audit, recorded in
  [`AUDIT_FINDINGS_2026-08-03.md`](AUDIT_FINDINGS_2026-08-03.md).

