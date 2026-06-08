# Awareness — Audit Findings (2026-06-08)

Parallel multi-agent audit: 73 agents, ~5M tokens. **52 candidate bugs → 39 verified**
(13 refuted by adversarial verification), **70 improvement opportunities**. Raw structured
data (every bug with file:line, repro, and suggested fix; every improvement with rationale
and effort): [`2026-06-08-awareness-audit.json`](2026-06-08-awareness-audit.json).

## The two headline symptoms — root-caused

### 1. "Searching *bitcoin* returns only 2 results / search is broken"
This is a **downstream** symptom. Independently confirmed root causes:
- **Empty corpus.** `data/jsonl/` and `data/duckdb/` are empty on disk — because scraping
  doesn't actually pull data (symptom #2). Search can't find what was never ingested.
- **CLI `search` hard-defaults to a 30-day `fetch_ts` window** (`cli/main.py`), silently
  hiding everything older. The API is unbounded — so CLI and API disagree.
- **Compressed `.jsonl.gz` chunks are invisible to the index**: `rglob('*.jsonl')` misses
  them, so a compressed corpus returns nothing.
- Plus FTS-vs-prefix semantics drift (FTS is OR, fallback is AND → different result sets),
  field-order-sensitive ranking, stale FTS index keyed on row-count, and a per-request
  index rebuild that also causes concurrent `/search` to 500 with a write-write conflict.

### 2. "The app has no idea how to scrape the internet"
**Dominant root cause** (verified against live `index.commoncrawl.org`):
- **`crawl_ids_for_range()` fabricates non-existent Common Crawl IDs** via an odd-ISO-week
  heuristic (`sources/commoncrawl_wet.py:87`). Real crawls are spaced ~4–5 weeks apart at
  arbitrary (often even) week numbers. For a typical range it generates 19 IDs of which only
  ~5 are real; the rest 404. This single function feeds WET, CC-Index, **and** FineWeb — so
  all three target phantom crawls and silently emit nothing.
- **WET fetches only 1 shard per crawl by default** (registry uses bare `cls()`) → a trivial
  fraction of the web even when the crawl ID is right.
- **FineWeb is a silent no-op** because the `datasets` package isn't installed.
- **All HTTP/network/404 failures are swallowed** as warning-level no-ops — the job reports
  success with zero docs and no error surfaces to the user.
- **Domain filter compares eTLD+1 against raw user domains** → passing `news.bbc.co.uk`
  drops every record.
- No retries/backoff, no seed discovery (sitemaps/robots), per-domain rate-limiter delay
  race, and a blocking `getaddrinfo` SSRF check stalling the async event loop.

## Verified bugs by area (39)

| Area | High | Med | Low |
|---|---|---|---|
| storage + search | 2 | 4 | 2 |
| orchestration (workers/tail) | 5 | 1 | 0 |
| cli / tui | 1 | 4 | 2 |
| sources / scraping | 2 | 1 | 2 |
| api / spa | 3 | 2 | 0 |
| processing | 1 | 2 | 0 |
| dedup | 0 | 1 | 2 |

Notable crash/data-loss bugs beyond the two headlines:
- `JobStatus` referenced but never imported in `tail/engine.py` → **NameError crash** on
  `tail start --job-id` resume.
- `claim_pending_tasks` is **not atomic** → concurrent workers double-claim the same task.
- Crashed/stopped tasks stuck in `RUNNING` are **never requeued** → permanent work loss.
- `run_job` marks a job **COMPLETED on graceful stop** with PENDING/RUNNING tasks left → lost progress.
- A single JSONL chunk missing a column **breaks the entire `captures` view** → all
  search/inspect/counts queries fail.
- `near_dup_hash` declared 32-bit `Integer` but stores a 64-bit value → **overflow/NULL on
  Postgres** (and NULL-on-read in DuckDB signed BIGINT).

## Improvement opportunities (70) — emphasis areas you asked for

### Mathematics / algorithms
- **SimHash banding doesn't satisfy the pigeonhole guarantee** at the real Hamming threshold
  → recall is lossy by construction; fix the band/threshold relationship for exact retrieval.
- **Hand-picked `Hamming=24`** → replace with a **data-driven, false-positive-rate-controlled**
  calibrated cutoff; gate short docs by minimum shingle count (current threshold is highly
  length-sensitive).
- **IDF-weight SimHash shingles** (true Charikar) instead of only local `1+ln(count)`.
- **Near-dup folding splits clusters** (a doc folded under B still anchors its own group) →
  **union-find / canonical-set** grouping.
- **BM25F**: title field-boost + length-aware + recency prior instead of raw single-blob BM25.
- **Language detection**: confidence-aware LID (fastText `lid.176` / CLD3), length gating,
  trust FineWeb metadata; today an undetected language silently drops the doc.
- **Benchmarks**: bootstrap confidence intervals + a real-text holdout corpus so the
  head-to-head numbers are defensible; fix metrics histogram first-N sampling bias →
  reservoir sampling + true p50/p95/p99.

### Systems engineering
- **Persist + incrementally update the DuckDB FTS index** instead of full rebuild on any change.
- **Stream WET parsing** via a bounded queue instead of buffering the whole shard in a list.
- **Shared pooled httpx client** (keep-alive/HTTP2) + **global fetch/extract concurrency** cap
  + **retries with exponential backoff + Retry-After (429/503)**.
- **Re-partition Iceberg by `month(fetch_ts)+source_type`** + a real **compaction** job to stop
  tiny-file explosion.
- **Crash-safe flush** (durable-before-clear) + **idempotent Iceberg appends**.
- **Export metrics** (`/metrics` + CLI snapshot) + per-fetch structured outcome logs + trace_id.
- **State-DB concurrency**: WAL mode, `busy_timeout`, real transaction isolation, and a single
  **job-ownership/leasing contract** (three code paths currently drain the same job).

## Audit blind spots (flagged by the completeness critic — must check during planning)
- SSRF / **DNS-rebinding TOCTOU** if we add a DNS cache; redirect-hop revalidation for IPv6 ULA/link-local.
- **robots.txt** crawl-delay honoring + User-Agent consistency — verify *before* raising shard breadth.
- **GDELT slot/time math** may be a *second* "fabricated identifier" bug (same class as the CC odd-week bug) — not independently verified yet.
- PyIceberg append/catalog-locking/concurrent-append correctness (only partitioning was examined).
- **Test-suite coverage map**: several tests are weak/over-hedged (`test_planner.py` count-only, `test_dedup.py` `NEAR|EXACT|NEW` hedge) and let bugs pass CI — map coverage before refactoring.
- `warc_repair` WARC parsing + the `cc_index → warc_repair` sub-partition path got minimal scrutiny.
- JSONL staging dir growth/rotation; `metadata.duckdb` growth across per-request connections.
- Pydantic validation + sane caps on the new user-settable knobs (shards-per-crawl, budgets, fineweb-rows).
- SPA Jobs/tail/dedup pages + polling vs the new job states/health fields.
