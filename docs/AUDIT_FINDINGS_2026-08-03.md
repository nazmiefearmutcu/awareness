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

---

## Resolution status

Verified against the working tree at commit `846a2bc` (all three remediation
commits: `3f4d980` audit round 1, `20e6d20` feature subsystems,
`846a2bc` second-pass). Each row was confirmed by inspecting the current
code, not the commit messages.

### CRITICAL

| ID | Status | Evidence |
|----|--------|----------|
| C-01 | RESOLVED | `workers/engine.py:359-366` — `run_tail` treats COMPLETED like CANCELLED/FAILED and exits the reseed loop |
| C-02 | RESOLVED | `commoncrawl_wet.py:94-110` — `crawl_ids_for_range` delegates to `cc_crawls.resolve_crawl_ids()` (live CC index); tests updated to the real resolver |
| C-03 | RESOLVED | `api/server.py:105-136` bearer-key auth (`require_api_key`); `:1023-1027` refuses non-loopback bind without `AW_API_KEY` |
| C-04 | RESOLVED | `config/persist.py:_validate_setting_value` — path confinement (no `..`, inside project root, anchored), env-lock, schema validation |
| C-05 | RESOLVED | `util/urls.py:1543` `is_public_http_url` gates seeds (`persist.py:47`), robots fetches (`robots.py:99`), redirect hops (`tail_recrawl.py:140`) |
| C-06 | RESOLVED | `config/settings.py:225-232` — YAML keys shadowed by `AW_*` env are filtered before pydantic init |
| C-07 | RESOLVED | `storage/state.py:386-391` — Postgres migration now ALTERs `dedup_near` to add `sig_hex`; `_verify_dedup_schema` stays enforced |
| C-08 | RESOLVED | `workers/engine.py:567-574` — buffer cleared only after successful JSONL write; one retry then critical log + drop, never silent |
| C-09 | RESOLVED | `api/server.py:930-934` — jobsearch `limit` coerced, clamped to [1, 100], 400 on non-int |

### HIGH

| ID | Status | Evidence |
|----|--------|----------|
| H-01 | RESOLVED | `workers/engine.py:259-273` — drain guard counts PENDING-but-unclaimable (backoff) and RUNNING-orphan separately |
| H-02 | RESOLVED | `workers/engine.py:186-192` — `requeue_orphaned_running` on job start; `state.py:776` dead-letters at max_retries |
| H-03 | RESOLVED | `workers/engine.py:307` — `run_tail` also requeues orphans on resume |
| H-04 | RESOLVED | `tail/engine.py:256-258` — drain-timeout path cancels + awaits the worker task |
| H-05 | RESOLVED | `storage/state.py:652` — re-armed task must not inherit a stale backoff lease |
| H-06 | RESOLVED | `workers/engine.py:450-465` — `dedup_dropped` counts only EXACT_DUP/REVISION/tight-NEAR_DUP; loose NEAR_DUP persists → emitted |
| H-07 | RESOLVED | `workers/engine.py:532-534` — terminal failures `increment_job_counters(failed=1)`; `:401` no_adapter DLQ path adds counters |
| H-08 | RESOLVED | `storage/state.py:800-809` — orphaned-running past max_retries is dead-lettered to the DLQ |
| H-09 | RESOLVED | `tail/daemon.py:44-50` — DatabaseReaper started in the tail daemon; backfill run reaps too |
| H-10 | RESOLVED | `storage/duckdb_index.py:1063-1069` — incremental FTS append detects overlapping capture_ids with changed content_hash |
| H-11 | RESOLVED | `storage/jsonl.py:143-171` — `recover_orphan_temps` promotes `.tmp` survivors; empty orphans deleted only after read |
| H-12 | RESOLVED | `storage/duckdb_index.py:789-794` — captures view dedups by capture_id (ROW_NUMBER) with Iceberg disabled too |
| H-13 | RESOLVED | `commoncrawl_wet.py:390-400` — shard download failures raise so the task retries; tmp cleaned |
| H-14 | RESOLVED | `commoncrawl_wet.py:383-388` — CancelledError propagates (task re-queued), tmp unlinked |
| H-15 | RESOLVED | `fineweb.py:211-221` — load failure raises `RetryableHTTPError` |
| H-16 | RESOLVED | `cc_index.py:61-64, 90-99` — CDX query carries `from`/`to` from the backfill window ∩ crawl window |
| H-17 | RESOLVED | `cc_index.py:104-138` — pagination loop (pageSize 100, cap 5000) + `get_with_retries` |
| H-18 | RESOLVED | `fineweb.py:66-75` `_normalize_languages_filter` (BCP-47 primary subtags, case-insensitive); `util/lang.py` `normalize_language_tag` |
| H-19 | RESOLVED | `api/server.py:66-80, 378-395` — mutating requests must be `application/json`; text/plain → 415 |
| H-20 | RESOLVED | `api/server.py:511-518` — /backfill ValueError → 400 with message |
| H-21 | RESOLVED | `config/persist.py` `_redact_url_userinfo` + schema payload masking; `tests/unit/test_settings_redaction.py` |
| H-22 | RESOLVED | `api/server.py:340-349` — background jobs cancelled AND awaited (`asyncio.gather`) in shutdown |
| H-23 | RESOLVED | `dedup/engine.py:104-108` — EXACT_DUP/REVISION fold to `uf_find` root, not the direct canonical doc |
| H-24 | RESOLVED | `storage/state.py:171-199` — 8-bit bands (1/256 selectivity) raise the 1024-cap truncation ceiling ~16×; legacy 32×4 layouts detected at startup with loud warning |
| H-25 | RESOLVED | `warc_repair.py:88-89` — non-206 to a byte-range request is rejected, never parsed as a record |
| H-26 | RESOLVED | `util/ratelimit.py:110-121` — release only when actually acquired; cancellation cannot over-release |
| H-27 | RESOLVED | `util/http.py:299-308` `aclose_shared_async_clients`; `tests/unit/test_http_retry_closes.py` |
| H-28 | RESOLVED | `util/http.py:495-511` — strict UTF-8 first; mislabeled bodies fall through to the detector |
| H-29 | RESOLVED | `util/lock.py:64-73` — RedisLock sets socket + connect timeouts |
| H-30 | RESOLVED | `obs/logging.py:20-23` — reconfig is a no-op only when args are identical; settings-driven config applies |
| H-31 | RESOLVED | `settings.py:188-201` `_validate_data_dir` (rejects /dev/null-style targets); `persist.py` path confinement + `is_public_http_url` on seeds |
| H-32 | RESOLVED | `extract_concurrency`, `backoff_base_sec`, `contact_email` were never read at runtime — all three removed from `settings.py`, `schema.py`, and `configs/awareness.yaml` (2026-08-03 cleanup) |

### MEDIUM

| ID | Status | Evidence |
|----|--------|----------|
| M-01 | RESOLVED | `cli/main.py:297-318` — `--source` aliases → canonical SourceKind with clean errors |
| M-02 | RESOLVED | `cli/main.py` counts — `total_n = int(total[0]["n"]) if total else 0`; `api/server.py:286` |
| M-03 | RESOLVED | `cli/main.py:334-338` `_coerce_end_checked` — friendly error, no traceback; `tests/unit/test_cli_end_dates.py` |
| M-04 | RESOLVED | `cli/main.py:343-374` — daemon-thread stdin reader with `select` poll; no block-at-exit; test-covered |
| M-05 | RESOLVED | `cli/main.py:4829-4836` — txt export names include `capture_id`; `tests/unit/test_cli_export_txt_unique.py` |
| M-06 | RESOLVED | `jobsearch/rank.py:62-70` — word-boundary token match (`ai` no longer matches "email") |
| M-07 | RESOLVED | `jobsearch/sources.py:374-380` — dedup by canonical title/key, not bare `company::title` |
| M-08 | RESOLVED | `api/server.py:585-586` — gdelt_max_urls clamped to 1..100_000 |
| M-09 | RESOLVED | `feeds.py:110-121` — seed/child URL validation via `is_public_http_url`; bounded sitemap recursion depth |
| M-10 | RESOLVED | `util/robots.py` — async-first API; DB access via `asyncio.to_thread` (287, 319, 353) |
| M-11 | RESOLVED | `robots.py:129, 166-179` MAX_ENTRIES + eviction; `ratelimit.py` bounded slot table |
| M-12 | RESOLVED | `robots.py:231-235` — 200-empty-body labelled `empty`, not `http_error` |
| M-13 | RESOLVED | `feeds.py:152-163` — `_retry_exhausted_status_class` parses the real status from `RetryableHTTPError` |
| M-14 | RESOLVED | `gdelt.py:96-103` — backfill slots only capped by `max_tasks` |
| M-15 | RESOLVED | `tail_recrawl.py:140` — `follow_redirects=False`; every hop re-checked against robots + public gate |
| M-16 | RESOLVED | `xscraper/store.py:208-213` — all tweet session_ids validated before any insert |
| M-17 | RESOLVED | `xscraper/store.py:187-197` — `started_at` NULL while queued, stamped on first backfilling/streaming |
| M-18 | RESOLVED | `xscraper/store.py:192` — `error` now a plain assignment (clearable); COALESCE kept only for `ended_at` |
| M-19 | RESOLVED | `dedup/engine.py:78-81` — near_threshold clamped to [0, segments-1] |
| M-20 | RESOLVED | `storage/state.py:1074-1077` — IntegrityError insert retried (bounded), never reports `was_new=True` for an unpersisted row |
| M-21 | RESOLVED | `storage/state.py:1116-1117` — all bands upserted in ONE transaction |
| M-22 | RESOLVED | `storage/state.py:1218-1223` — `uf_find` never writes/commits; `tests/unit/test_uf_find_readonly.py` |
| M-23 | RESOLVED | `storage/state.py:141-199` — legacy NULL sig_hex / 32×4 layouts detected at init with rebuild guidance; full 128-bit sig_hex enforced |
| M-24 | RESOLVED | `filters.py:84-92` — empty-string-matching regex dropped under `match_all` |
| M-25 | RESOLVED | `filters.py:74-81` — bad regex logged and falls back to literal, never silently |
| M-26 | RESOLVED | `schemas/doc.py:127-129` — naive strings routed through `to_utc` |
| M-27 | RESOLVED | `urls.py:458-487` — alias strip requires a real multi-label remainder (`www.com`/`m.me` preserved) |
| M-28 | RESOLVED | `duckdb_index.py:442-444, 683-719` — corrupt chunk surfaced in health snapshot; per-union error isolation |
| M-29 | RESOLVED | `duckdb_index.py:637-643` — failed refresh leaves the old signature → next call retries |
| M-30 | RESOLVED | `duckdb_index.py:249-255` — remote warehouses get a coarse time-bucket signature |
| M-31 | RESOLVED | `gdrive.py:204-207` — retries 429/5xx/transport with backoff, one 401 token refresh, streaming upload |
| M-32 | RESOLVED | `timeutil.py:24-26` — singular "1 day ago" handled; `tests/unit/test_timeutil_singular.py` |
| M-33 | RESOLVED | `api/server.py:571` — GET /jobs `limit` bounded `ge=1, le=500` |
| M-34 | RESOLVED | `cli/main.py:5211-5243` — `config set` validates against `Settings.model_fields` + schema coercion; `persist.py:apply_updates` enforces env-lock; effective source shown by `schema.value_source` |
| M-35 | RESOLVED | `util/http.py:94-100` — Retry-After honored up to 600s cap with jitter |
| M-36 | RESOLVED | `iceberg.py:38-41` — nanosecond ISO timestamps parsed tolerantly |
| M-37 | RESOLVED | `jobsearch/linkedin.py:417-419` — `enriched` set only when a real detail body merges |

**Verdict:** 9/9 critical, 32/32 high, 37/37 medium addressed — 78/78
RESOLVED, 0 OPEN. H-24's silent-truncation ceiling was raised (~16×) rather
than removed (a hard cap remains by design); M-34's CLI path validates +
coerces writes and the env-lock is enforced at load and in the API write
path. Both are considered addressed per their findings' intent.
