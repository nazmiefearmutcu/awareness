# Awareness — Cycle 1 Design: "Make It Actually Work"

**Status:** Approved (design) · 2026-06-08
**Scope:** Roadmap Phases 1–3 (fix all 39 verified bugs · make scraping pull real data · make
search work). Mathematical & systems-engineering upgrades (Phases 4–5) are a **separate
follow-up spec (Cycle 2)**, to be written immediately after this one lands. Data-integrity &
polish (Phase 6) is Cycle 3.
**Source of truth for per-item detail:** [`../audit/2026-06-08-awareness-audit.json`](../audit/2026-06-08-awareness-audit.json)
(every bug: file:line, repro, suggested fix; every improvement: rationale, effort).
**Human-readable findings:** [`../audit/2026-06-08-awareness-findings.md`](../audit/2026-06-08-awareness-findings.md).

## 1. Problem & Goal

`awareness` ingests the public text web (Common Crawl, FineWeb, RSS/sitemap, GDELT), extracts
main text, dedupes (SimHash), stores to Iceberg/DuckDB/JSONL, and serves a CLI + FastAPI SPA.
Today it has two user-visible failures, root-caused by a 73-agent audit:

1. **"Searching *bitcoin* returns only 2 results / search is broken."** Downstream symptom of:
   an **empty corpus** (scraping doesn't pull data), a **silent 30-day CLI search window**,
   **`.jsonl.gz` chunks invisible to the index**, and FTS/prefix semantic drift.
2. **"The app has no idea how to scrape the internet."** Root cause: `crawl_ids_for_range()`
   **fabricates non-existent Common Crawl IDs** (odd-ISO-week heuristic) → most fetches 404 →
   silent zero docs. Compounded by 1-shard-per-crawl defaults, an inert FineWeb source, and
   swallowed network errors.

**Goal (testable success criteria):**
- From a fresh clone, a single documented command ingests **real** public-web text within
  minutes (verifiable against a mocked-network fixture in CI, and manually against the live web).
- `awareness search <term>` returns **all** relevant captures across the **full** corpus,
  ranked, with the **active time window shown**; CLI, API, and SPA return the **same** results
  for the same query.
- **All 39 verified bugs fixed**, each with a regression test.
- A failed/empty ingest or search **says why** (no silent no-ops).
- CI proves both headline symptoms stay fixed (end-to-end smoke).

## 2. Constraints & Decisions

- **Architecture envelope:** user chose "whatever works best." We nonetheless keep the
  single-process, one-SQL-engine-over-one-lake design where it is sound, and only add weight
  where it buys correctness.
- **D1 — Search engine: fix DuckDB FTS, do not swap engines.** Make the index a process-wide
  singleton, rebuild on a content signature (not row count), build out-of-band on flush.
  (Persisted/incremental FTS is a Cycle-2 optimization, not required here.)
- **D2 — Search window:** remove the hidden 30-day default → default **all-time**, **always
  print the active window** in results, add a `--last <duration>` convenience flag. Trades no
  silent behavior for another.
- **D3 — Crawl-ID resolution:** fetch the authoritative `collinfo.json`, TTL-cache it, and
  **ship a bundled fallback snapshot in the same change** so offline/blocked users still get
  real IDs. Select crawls whose `[from,to]` window overlaps the requested range.
- **D4 — Job ownership first:** define one job-ownership/leasing contract **before** layering
  the atomic-claim and reaper fixes on top (three code paths currently drain the same job).
- **D5 — Breadth co-lands with safety:** any increase in shards/fan-out lands in the **same
  change** as the per-domain rate-limiter fix + a job-wide document/sub-task budget + retries,
  so an inert scraper never becomes an abusive one.
- **D6 — No silent failures:** discovery/fetch failures and zero-yield jobs surface as typed,
  logged, user-visible signals — never a green no-op.

## 3. Workstreams

Each workstream is an independently understandable unit with a clear boundary. Audit refs in
`[brackets]` map to entries in the audit JSON. A bug may be touched by one workstream and
regression-tested there.

### A. Ingestion correctness — "scrape real data" (`sources/`)
- Real crawl-ID resolver from `collinfo.json` + TTL cache + bundled fallback; select by
  time-window overlap. Unblocks WET, CC-Index, and FineWeb at once.
  `[imp:fix-commoncrawl-crawl-id-mapping, bug:cc-crawl-id-odd-week-heuristic-wrong]`
- Domain filter normalized through `domain_of()` on both sides (subdomain requests stop
  dropping everything). `[bug:wet-domain-filter-etld1-drops-subdomain-requests]`
- WET shards-per-crawl configurable, default > 1, spread-sampled across the shard list.
  `[imp:expose-cc-wet-shard-count, imp:cc-wet-shard-count default]`
- FineWeb: move `datasets` to an optional `[hf]` extra, drop FineWeb from defaults, and **fail
  loudly** when selected without the dep. `[imp:install-fineweb-deps-or-warn-loudly]`
- WET per-shard checkpoint so a retry resumes instead of re-parsing/re-emitting record 0.
  `[bug:wet-shard-no-checkpoint-full-reparse-on-retry]`
- Feeds cursor: order-preserving dedup (no set-slice loss/re-emit).
  `[bug:feeds-checkpoint-set-slice-loses-seen-urls]`
- Seed discovery from a bare domain: robots.txt `Sitemap:` directives + feed autodiscovery.
  `[imp:default-seed-discovery-from-domain]`
- **Blind-spot check (fold in):** verify GDELT slot/time math is not a *second* fabricated-id
  bug (same class as the CC odd-week defect); fix if so. Lightly verify `warc_repair` WARC
  parsing and the `cc_index → warc_repair` sub-partition path.
- **Boundary:** a `SourceAdapter` yields real captures or raises a typed, logged failure. No
  silent zero-yield.

### B. Fetch reliability layer — "fetch safely & robustly" (`util/`, shared)
- One shared, pooled `httpx` client with timeouts; all fetchers use it.
  `[imp:scraping-robustness, throughput-resource (client reuse only; HTTP2/global-concurrency
  tuning deferred to Cycle 2)]`
- Retries with exponential backoff + `Retry-After` handling (429/503).
  `[imp:add-http-retries-backoff-respect-retry-after, imp:reliability-scraping]`
- Per-domain rate-limiter delay race fixed (inter-fetch delay actually holds under concurrency).
  `[imp:fix-perdomain-limiter-delay-race]`
- Job-wide document/sub-task budget to bound discovery fan-out.
  `[imp:gdelt-and-fanout-cap-defaults]`
- SSRF check (`is_public_http_url`): move blocking `getaddrinfo` off the event loop + cache,
  **without** opening a DNS-rebinding TOCTOU (resolve-then-connect; revalidate redirect hops;
  block private/link-local/IPv6 ULA). `[bug:tail-blocking-dns-in-event-loop]` + blind-spot.
- **Blind-spot check (fold in):** honor robots.txt crawl-delay and keep the User-Agent used by
  robots/limiter identical to the one sent on fetches — **before** raising shard breadth.
- **Boundary:** every network fetch goes through this layer; **breadth increases land with the
  limiter fix + budget** (D5).

### C. Job lifecycle & state integrity — "no lost work" (`workers/`, `tail/`, `storage/state`)
- Single job-ownership/leasing contract (resolve CLI worker / API-spawned worker / standalone
  worker triple-drain). **Designed first** (D4).
- DB-level atomic `claim_pending_tasks` (SQLite WAL + `busy_timeout` + a real transaction /
  conditional UPDATE), **not** an in-process lock.
  `[bug:nonatomic-claim-pending-tasks]`
- Orphaned-`RUNNING` reaper (startup + periodic) → requeue to `PENDING`.
  `[bug:orphaned-running-tasks-never-requeued]` — **co-lands with** the COMPLETED-on-stop fix.
- Don't mark a job `COMPLETED` on graceful stop with `PENDING`/`RUNNING` tasks remaining.
  `[bug:premature-job-completed-on-stop]`
- Attempt-bounded, backoff-delayed task retries (`next_attempt_at`) instead of immediate
  re-PENDING busy-retry. `[imp:backoff-on-task-retry, imp:reliability-backpressure]`
- Fix `JobStatus` NameError on the tail resume path (deterministic crash).
  `[bug:tail-engine-jobstatus-nameerror]`
- **Blind-spot check (fold in):** state-DB concurrency for `increment_job_counters`,
  `complete_task`, `fail_task`, `add_tasks` (WAL, `busy_timeout`, transaction isolation;
  stress for "database is locked" / lost counter updates).
- **Boundary:** every task is claimed **exactly once**, never lost; a job is `COMPLETED` only
  when truly drained.

### D. Corpus visibility & search correctness — "search finds everything" (`storage/duckdb_index`, `cli`, `api`)
- Index `.jsonl.gz` chunks (`rglob('*.jsonl*')`) in both the source-signature and view builder.
  `[bug:jsonl-gz-corpus-invisible-to-search]`
- Remove the silent 30-day CLI window → default all-time, **always print active window**, add
  `--last`. `[bug:cli-search-default-30day-window-hides-results,
  bug:search-default-restricts-last-30-days]` (D2)
- Date-only `end` treated as inclusive end-of-day across `/captures`, `/search`, `/inspect`,
  `/counts`. `[bug:end-date-filter-excludes-whole-day]`
- `captures` view resilient to a structurally missing column (dynamic NULL-fill) so one bad
  chunk can't break all queries. `[bug:captures-view-build-aborts-on-any-missing-column]`
- FTS rebuild keyed on **content signature**, not row COUNT; build out-of-band on flush.
  `[bug:fts-index-rebuild-keyed-on-rowcount-serves-stale-results, imp:auto-build-fts-and-widen-default-mode]`
- FTS index a **process-wide singleton** + serialized rebuilds → fixes per-request rebuild and
  concurrent `/search` write-write-conflict 500s.
  `[bug:fts-index-rebuilt-every-request, bug:concurrent-search-write-write-conflict]`
- Unify multi-word semantics: **OR-by-default + relevance ranking**; order-insensitive
  `fts_eligible`; stem-root substring covers inflections; auto-mode robust to
  punctuation/operator queries.
  `[bug:fts-or-vs-prefix-and-semantics-inconsistent, bug:fts-eligibility-field-order-sensitive,
  bug:prefix-fallback-stem-substring-misses-inflections,
  imp:search-or-semantics-and-relevance, imp:search-default-mode-auto-when-fts-strips-query]`
- Snippet/highlight matches the stem roots search actually used (incl. non-word-boundary,
  multibyte). `[bug:prefix-mode-highlights-never-render, imp:search-snippet-highlight-on-stems-and-multibyte]`
- Date-parse robustness: reject unparseable `--start` (no silent widen); catch `ParserError`
  on `--end`; accept "N days ago" symmetrically.
  `[bug:relative-start-parse-silently-drops-filter, bug:search-end-parse-crash,
  bug:coerce-relative-end-rejects-days-ago]`
- `/captures` & `/inspect` search route through the real engine (no raw-ILIKE divergence).
  `[imp:captures-list-search-ilike-bypasses-ranking]`
- Pagination correctness: interactive paging past end, non-interactive page cap, SPA offset
  corruption, dead `(capped)` indicator, stable pagination + cached total.
  `[bug:interactive-paging-past-end-loops-on-last-page, bug:noninteractive-search-caps-at-page-limit,
  bug:search-pagination-corrupts-past-max-results, bug:capped-indicator-never-triggers,
  imp:search-pagination-stable-and-no-recount]`
- Ingest-time keyword filter loosened to stem/prefix (so "bitcoins"/"bitcoin's" match).
  `[imp:keyword-filter-word-boundary-misses-bitcoins]`
- Faceting + empty-state feedback (see E for empty-state).
  `[imp:search-faceting-endpoint-and-cli]`
- **Boundary:** **one config-driven search-default resolver** shared by CLI/API/SPA; identical
  query → identical results. `[imp:unify-search-defaults-config-driven, imp:search-consistency]`
- Phrase / true-prefix (`term*`) / fuzzy modes: include if low-risk on the DuckDB FTS path;
  otherwise defer the fuzzy/trigram part to Cycle 2 and ship phrase+prefix here.
  `[imp:search-phrase-prefix-fuzzy-modes]`

### E. Onboarding / "get data now" (`cli`, `config`)
- One-command `quickstart` that visibly fetches real data (tail + GDELT + small CC) with sane
  defaults. `[imp:sensible-default-backfill-quickstart, imp:onboarding/ux]`
- Loud warning when a backfill plans **zero** tasks, with per-source reasons.
  `[imp:ingestion-no-source-no-tasks-guard, imp:fail-loud-on-empty-discovery,
  imp:observability-robustness]`
- Search empty-state explains **why** (empty corpus vs over-filtered window).
  `[imp:search-empty-state-and-mode-feedback, imp:ux/empty-states]`
- Source-aware `text_min_chars` so short news/feed articles aren't dropped.
  `[imp:lower-rss-min-chars-for-feeds, imp:scraping-yield]`
- **Boundary:** a new user reaches first results without reading source code; failures are
  loud and self-explaining.

### F. Test foundation & regression net (`tests/`)
- **Coverage map first:** which verified bugs have tests, which existing tests are weak/over-
  hedged (`test_planner.py` count-only assertion; `test_dedup.py` `NEAR|EXACT|NEW` hedge;
  `test_search_matching.py` `_FULL_KEYS` workaround).
- Strengthen those weak tests (e.g. `test_planner` must assert generated IDs intersect a known
  real-crawl set).
- **One regression test per verified bug.**
- Deterministic adapter tests by mocking the network at the shared httpx-client boundary, plus
  a small real-crawl fixture.
- **End-to-end smoke:** empty data dir → `quickstart` (mocked network) → `search <term>`
  returns the seeded doc across the full window. Proves both headline symptoms fixed.
- **Boundary:** CI is the proof both symptoms stay fixed.

### G. Schema & misc correctness (bug-level) (`dedup`, `schemas`, `storage`, `processing`)
- `near_dup_hash` declared `BigInteger` (32-bit Integer overflows on Postgres / NULLs in
  DuckDB). `[bug:near-dup-hash-int32-overflow-on-postgres, imp:near-dup-hash-bigint-overflow-null]`
- Gate near-dup merge on a minimum shingle/token count for short docs (length-sensitivity).
  `[bug:fixed-hamming-threshold-length-sensitive-inconsistent-merging]`
- `sig_hex` migration: re-inspect columns after the `ALTER` and **raise loudly** if still
  missing (stop swallowing). `[bug:swallowed-sig-hex-migration-leaves-writes-broken]`
- Google Drive upload reads **bytes** (not `read_text`, which crashes on `.jsonl.gz`) and sends
  a correct Content-Type. `[bug:gdrive-upload-readtext-fails-on-gzip-and-sends-invalid-json]`
- `tail_recrawl` / `warc_repair` honor `text_min_chars`/`text_max_chars` and declared
  charset/BOM when decoding. `[bug:tail-recrawl-ignores-text-min-max-chars-settings,
  imp:tail-recrawl-no-content-type-charset-decode]`
- Distinguish transient HTTP failures from genuine 404s in discovery adapters (don't swallow).
  `[bug:cc-and-discovery-network-failures-silently-swallowed,
  imp:observability/scraping-ux, imp:observability-reliability]`

## 4. Sequencing (dependency-ordered)

1. **F — test foundation + coverage map** so every later fix is provable.
2. **C — crash/data-loss** (job lifecycle + `JobStatus` NameError) and **D**'s
   captures-view-resilience. *Co-land:* COMPLETED-on-stop + orphaned-reaper.
3. **A + B — make scraping real:** crawl-ID resolver first (unblocks 3 adapters), then the
   fetch layer. *Co-land:* breadth + limiter + budget (D5).
4. **D — make search work:** needs data flowing (from 3) to validate. *Co-land:* FTS singleton
   + write-conflict fix.
5. **E — onboarding** ties it together; the end-to-end smoke (F) closes both symptoms.

**Hard co-landing constraints (do not split across tasks):** (a) premature-COMPLETED-on-stop +
orphaned-RUNNING reaper; (b) FTS singleton + write-write-conflict; (c) shard/fan-out breadth +
per-domain limiter + job budget.

## 5. Testing Approach

- TDD per task (failing test → fix → green). Standard command:
  `PYTHONPATH=src .venv/bin/python -m pytest -q -m "not slow and not smoke"`.
- Mock the network at the shared httpx-client boundary for deterministic source tests; keep one
  small real-crawl fixture for the crawl-ID resolver.
- A regression test named for each bug id; strengthen the three weak tests called out in F.
- End-to-end smoke gates the two headline symptoms.
- Baseline at branch start: **193 passing** (`not slow and not smoke`), 0 failures.

## 6. Out of Scope (→ Cycle 2 / Cycle 3)

**Cycle 2 — Mathematical & systems upgrades (the user's emphasis; next spec):** SimHash IDF
weighting; pigeonhole-correct banding; FPR-calibrated Hamming threshold; union-find near-dup
clustering; BM25F field-boost + length/recency ranking; confidence-aware language detection;
benchmark bootstrap CIs + real-text holdout; metrics reservoir sampling + p50/p95/p99;
persisted/incremental FTS; streaming WET parse (bounded memory); pooled-client HTTP2 + global
concurrency tuning; Iceberg re-partition (`month(fetch_ts)+source_type`) + compaction;
crash-safe flush + idempotent Iceberg appends; metrics/`/metrics` export + per-fetch tracing.

**Cycle 3 — Data integrity & polish:** SPA Settings page editable; `fetch_ts`/`observed_ts`/
`published_ts` disentangle; URL canonicalization tightening; JSONL+Iceberg double-count
reconcile; remaining SPA pages review.

## 7. Open Blind Spots Folded Into This Cycle

Folded into the workstream **blind-spot checks** above: GDELT slot-math second-fabricated-id
risk (A); robots crawl-delay + UA consistency before breadth (B); SSRF/DNS-rebinding TOCTOU
when adding the DNS cache (B); state-DB concurrency beyond `claim_pending_tasks` (C); test
coverage map before refactoring (F). Remaining lower-risk blind spots (PyIceberg
append/catalog locking; JSONL staging growth/rotation; `metadata.duckdb` growth; Pydantic
validation + caps on new knobs; SPA Jobs/tail pages) are noted for Cycle 2/3 and called out in
the plan as explicit "verify, don't assume" items.
