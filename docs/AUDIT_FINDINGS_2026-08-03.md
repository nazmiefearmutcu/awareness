# Awareness — Bug Hunt & Red Team Findings Register

Generated: 2026-08-03 · Baseline: full test suite green (190+ test files), ruff/mypy not yet run
Repo: /tmp/awareness-fresh (fresh clone of nazmiefearmutcu/awareness@c8ee587)

## CRITICAL

| ID | Area | File:line | Issue |
|----|------|-----------|-------|
| C-01 | CLI/Tail | cli/main.py:2439-2454, workers/engine.py:335, tail/engine.py:135 | `tail stop` is a no-op: job set COMPLETED but run_tail breaks only on CANCELLED/FAILED; reseed loop re-arms; detached daemon never observes DB state → tails fetch forever |
| C-02 | Sources | commoncrawl_wet.py:87-101 | `crawl_ids_for_range` fabricates odd-week crawl IDs → ~90% of real crawls missed; `resolve_crawl_ids()` (cc_crawls.py:145) exists but is dead code; test codifies broken expectation |
| C-03 | API | server.py:223-270, runbook.md:103 | Entire control plane unauthenticated; remote binding (0.0.0.0) is documented; PUT /settings/config + POST /backfill + POST /tail/start fully exposed |
| C-04 | API | server.py:718-746, schema.py:374-378 | Unauthenticated config write = arbitrary file write (tail_seed_file/data_dir as free text) + credential capture |
| C-05 | API | server.py:742-746, tail/engine.py:124-190 | SSRF chain: seed URLs only http(s)-prefix validated → attacker injects 169.254.169.254 / internal URLs → crawler fetches → content readable via /search (exfil) |
| C-06 | Config | config/settings.py:30-42,195-199 | YAML overrides beat env vars (pydantic-settings init-args highest priority) — documented precedence inverted; operator AW_* overrides silently ignored |
| C-07 | Storage | state.py:347-362 | Non-SQLite migration omits dedup_near.sig_hex → _verify_dedup_schema raises → legacy Postgres permanently fails init |
| C-08 | Workers | workers/engine.py:543-565,625-638 | `_flush` clears buffer before write; JSONL failure drops docs silently; iceberg-only mode deletes only copy on append failure |
| C-09 | API | server.py:794, models.py:95 | /jobsearch/search raw limit → pydantic ValidationError → 500 (also limit="abc" ValueError → 500) |

## HIGH

| ID | Area | File:line | Issue |
|----|------|-----------|-------|
| H-01 | Workers | workers/engine.py:263-284, state.py:726-744 | Drain detection defeats retry backoff: failed task in 30s backoff → 3 empty polls → job COMPLETED, retry never runs |
| H-02 | Workers | workers/engine.py:188-199, state.py:661-702 | Crash→quick restart strands RUNNING tasks (900s lease), false COMPLETED; no running==0 drain guard |
| H-03 | Workers | workers/engine.py:286-352 | run_tail never requeues orphans; resumed tail leaves stale RUNNING tasks unclaimed |
| H-04 | Workers | tail/engine.py:212-231 | stop() on drain timeout abandons still-running worker task (no cancel+await) |
| H-05 | Workers | state.py:528-543 | Reseed re-arms RUNNING tasks → duplicate execution, double counters; next_attempt_at not cleared on re-arm |
| H-06 | Workers | workers/engine.py:439-447 | Loose NEAR_DUP counted as both emitted and dedup-dropped (double-count) |
| H-07 | Workers | workers/engine.py:371-377,493-506 | tasks_failed never incremented; no_adapter DLQ path misses job counter |
| H-08 | Workers | state.py:684-688 | Orphan-dead-lettered tasks never enter DLQ → unreplayable |
| H-09 | Workers | tail/daemon.py, cli/main.py:2123 | DatabaseReaper absent from tail daemon + backfill run |
| H-10 | Storage | duckdb_index.py:919-985 | FTS incremental append serves stale content after capture_id update (no content-change detection) |
| H-11 | Storage | jsonl.py:69-95,113-119 | Truncated gzip orphan temps DELETED → fsynced records lost |
| H-12 | Storage | duckdb_index.py:734-742 | captures view missing capture_id dedup when Iceberg disabled (default!) |
| H-13 | Sources | commoncrawl_wet.py:295-319,351-363 | Shard download swallows transient failures → shard skipped, task COMPLETED |
| H-14 | Sources | commoncrawl_wet.py:323-333 | Stop during download → task COMPLETED, never resumed |
| H-15 | Sources | fineweb.py:178-192 | load_dataset failure swallowed → partition COMPLETED, never retried |
| H-16 | Sources | cc_index.py:67-68 | CDX query ignores from/to → captures outside backfill window |
| H-17 | Sources | cc_index.py:65-93 | No pagination (≤200 cap), no retry/rate-limit handling |
| H-18 | Sources | cc_wet.py:280,498; fineweb.py:169,214 | Language filters case/format-sensitive → valid BCP-47 --lang drops everything |
| H-19 | API | server.py:378,437,462,775 | CSRF via text/plain JSON (CORS-safelisted, no preflight) — state-changing POSTs executable cross-origin |
| H-20 | API | server.py:383-390 | /backfill bad sources/end_str → unhandled ValueError → 500 |
| H-21 | API | server.py:712-716,284-285 | /settings/schema + /healthz leak config values + credentials (redis_url, state_db_url) |
| H-22 | API | server.py:258-259,408-423 | Background tasks cancelled without await; same job double-runnable |
| H-23 | Dedup | dedup/engine.py:81 vs 110-111 | EXACT_DUP/REVISION assign non-root parent_doc_or_dup_group (UF rooting omitted) |
| H-24 | Dedup | state.py:1036-1046,184 | Near-dup candidate retrieval truncates at 1024 rows/band → silent recall loss as corpus grows |
| H-25 | Warc | warc_repair.py:67 | 200 to byte-range request parses WRONG record (full-file payload, first record) — data corruption |
| H-26 | Ratelimit | ratelimit.py:56-74 | Cancellation over-releases semaphore → per-domain concurrency cap decays |
| H-27 | HTTP | util/http.py:371-381 | Retryable responses never closed → connection pool starvation |
| H-28 | HTTP | util/http.py:456-473,516-526 | Mislabeled UTF-8 bodies decode as mojibake (latin-1 strict-decode succeeds; detector unreachable) |
| H-29 | Lock | util/lock.py:63-67 | RedisLock no socket timeout → blackholed Redis hangs forever |
| H-30 | Logging | obs/logging.py:13-24 | _CONFIGURED guard makes settings-driven log config a no-op; log file never created |
| H-31 | Config | persist.py:125-172, settings.py:141-179 | data_dir=/dev/null bricks app permanently; tail_seed_file arbitrary YAML read/write |
| H-32 | Config | settings.py:85 | extract_concurrency dead knob (never read); backoff_base_sec/max_retries/contact_email also dead |

## MEDIUM (selection)

| ID | Area | File:line | Issue |
|----|------|-----------|-------|
| M-01 | CLI | main.py:2008 | --source CC-WET/FineWeb/GDELT (help's own examples) crash with traceback |
| M-02 | CLI | main.py:2884-2896 | counts "total" is a list not int (also server.py:564) |
| M-03 | CLI | main.py:2016,2795,2844,3681,3940 | Unparseable --end → raw ValueError traceback in 5 commands |
| M-04 | CLI | main.py:2145-2212,2348-2435 | backfill run / tail start hang at exit until Enter (executor thread blocked on readline) |
| M-05 | CLI | main.py:4394-4403 | export --format txt overwrites files on duplicate doc_id |
| M-06 | Jobsearch | rank.py:50-59 | _token_in_field is pure substring matching ("ai" matches "email") |
| M-07 | Jobsearch | sources.py:374-385 | dedupe_jobs over-dedupes by company::title (drops distinct postings) |
| M-08 | Jobsearch | server.py:446-447 | /tail/start gdelt_max_urls no upper bound (schema cap bypassable) |
| M-09 | Feeds | feeds.py:705-806 | Sitemap recursion fetches child sitemap URLs without is_public_http_url (SSRF-lite) |
| M-10 | Robots | robots.py:304-326 | crawl_delay sync DB call in async context |
| M-11 | Robots | robots.py:131,283; ratelimit.py:31-39 | Unbounded caches (_entries, _slots) memory leak |
| M-12 | Robots | robots.py:174,207-214 | 200-empty-body recorded as http_error (metric mislabel) |
| M-13 | Feeds | feeds.py:668,751 | retry_exhausted hardcodes status_class=5xx |
| M-14 | GDELT | gdelt.py:96-97 | plan() truncates backfills to 8 slots (2h) |
| M-15 | Tail | tail_recrawl.py:143-153,287-315 | Redirect targets bypass target domain robots/crawl-delay |
| M-16 | Xscraper | store.py:208-241 | store_tweets partial-write on session mismatch (latent, orphaned) |
| M-17 | Xscraper | store.py:122-143,177-196 | started_at stamped while queued; never updated on transition |
| M-18 | Xscraper | store.py:190 | error COALESCE prevents clearing |
| M-19 | Dedup | dedup/engine.py:73 | near_threshold clamped only at 0 — banding guarantee breakable (>31) |
| M-20 | Dedup | state.py:961-969 | upsert_dedup IntegrityError fallback returns was_new without insert |
| M-21 | Dedup | state.py:986-1024 | 32 commits per doc (band index) |
| M-22 | Dedup | state.py:1048-1090 | uf_find is a read that writes + commits |
| M-23 | Dedup | state.py:1041-1045 | Legacy sig_hex NULL rows never match (hamming128 on 64-bit) |
| M-24 | Filters | filters.py:92 | regex `.*` + match_all → filter trivially true |
| M-25 | Filters | filters.py:69-72 | Bad regex silent fallback can drop everything |
| M-26 | Schema | schemas/doc.py:119-128 | _ensure_utc only coerces datetime objects — naive strings slip through |
| M-27 | URLs | urls.py:457-479 | Alias-host strip can collapse real domains (m.me→me, www.com→com) |
| M-28 | Storage | duckdb_index.py:627-665 | One corrupt JSONL chunk bricks ALL queries (view creation outside try) |
| M-29 | Storage | duckdb_index.py:699-746 | Union failure swallowed while signature marked fresh → missing captures view persists |
| M-30 | Storage | duckdb_index.py:595-606 | Remote (s3/gcs) warehouse excluded from source signature → FTS never rebuilds |
| M-31 | Gdrive | gdrive.py:149-201 | Upload no retry, whole chunk in memory, no 401 refresh-retry |
| M-32 | Time | timeutil.py:23-29 | "1 day ago" → None (plural-only regex) |
| M-33 | API | server.py:432 | GET /jobs limit unbounded |
| M-34 | Config | cli/main.py:4742-4770 | config set skips env-lock + schema validation |
| M-35 | HTTP | http.py:299-302 | Retry-After clamped to 30s; no jitter |
| M-36 | Storage | iceberg.py:61-67 | Nanosecond timestamps abort Iceberg append |
| M-37 | Jobsearch | engine.py:76-82 | "enriched" metric counts any job with description |

## Verified OK (no bug — notable checks)
- Atomic JSONL/fsync/orphan-recovery (plain path)
- Atomic task claims (SQLite/Postgres), backoff math, GDELT slot advance
- Simhash128 banding invariant (default config), calibration math (exact integer CDF)
- XSS posture: zero innerHTML, href allow-listing, test-enforced
- /search SQL injection boundary (whitelisted fields, bound params)
- Canonical_url wrapper unwraps: refuse_hosts loop protection
- TopicFilter wired before dedup (integration test proves)
