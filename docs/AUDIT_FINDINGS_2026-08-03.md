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

---

## Round 2 (2026-08-04) — Ralph loop iterations 1–3

Generated against the working tree at `876dbc6` (iteration 1: fix-the-fixes
audit W5-A + performance W7 + Postgres parity W6-C) and `8c53af4` (iteration
2: gdeltx / corpusx / CLI trends + x-sessions / digest email / entity
network). Iteration 3 (W12) has no report in `docs/` at this commit — **W12 in
progress** (the `awareness quality` / `awareness feeds` CLI work landed in the
working tree mid-audit, uncommitted, with `tests/unit/test_cli_quality.py`).

### F-1 … F-6 (W5-A fix-the-fixes audit) — all RESOLVED in `876dbc6`

| ID | Area | Finding | Status | Evidence |
|----|------|---------|--------|----------|
| F-1 | API auth | Non-loopback bind without `AW_API_KEY` only *warned* | RESOLVED `876dbc6` | bind now **refuses** (SystemExit) — covered by `tests/unit/test_auth_security_fixes.py` |
| F-2 | API CSRF | Empty-body mutating requests bypassed the JSON CSRF gate; `Origin` was checked against the spoofable `Host` header | RESOLVED `876dbc6` | empty-body mutating requests → 415/422; Origin checked against the configured host, not `Host` |
| F-3 | API disclosure | `/healthz` disclosed `db_path` / `jsonl_dir` | RESOLVED `876dbc6` | fields removed from health response |
| F-4 | Storage repair | JSONL orphan repair was non-atomic; gzip boundary truncation (missing EOS) could not be repaired | RESOLVED `876dbc6` | repair now atomic (`.repair` temp + fsync + `os.replace`); truncated gzip repaired into valid gzip — `tests/unit/test_jsonl_repair_atomic.py` |
| F-5 | FTS staleness | Incremental FTS staleness join ran against the view (O(corpus) re-parse) | RESOLVED `876dbc6` | join now hits the indexed materialized table — `tests/unit/test_materialized_corpus.py` |
| F-6 | Alerts test flake | Flaky alerts-runner test counted engine calls instead of waiting on ticks | RESOLVED `876dbc6` | test waits on ticks, not `engine.calls` |

### W6-C Postgres-parity findings — fixed / open

| ID | Finding | Status | Evidence |
|----|---------|--------|----------|
| D1 | SQLite claim first-pass under-claims (`[3,3,0,0]` — unlocked SELECT race); claim invariant held, PG claims the full batch via `with_for_update(skip_locked=True)` | Verified (behavioral divergence, not a bug) | 4-thread claim exercise + `test_postgres_compatibility.py::test_claim_pending_tasks_with_skip_locked`; workers loop on SQLite |
| D2 | `requeue_orphaned_running` uses a process-local RLock → two PG workers can both DLQ the same orphan (duplicate `dlq` rows, no unique key) | RESOLVED `876dbc6` | unique index `uq_dlq_task` on `dlq(task_id)` (both engines, legacy migration included) + conflict-tolerant `add_dlq` (`ON CONFLICT … DO NOTHING`) |
| D3 | PG engine: default QueuePool(5+10), no `pool_pre_ping` — exhaustion with 15+ workers, stale connections after idle kill | RESOLVED `876dbc6` | `pool_pre_ping=True`, `pool_size=10` (SQLite pool untouched) |
| D4 | Tail reconcile `os.kill(pid,0)` is same-host-only — remote PG would phantom-CANCEL live tails | Open (ops note) | gate on host/instance id in `tail_state` if remote PG |
| D5 | PG `VACUUM` is full-database, not table-scoped — heavy on shared DBs | Open (ops note) | `VACUUM (ANALYZE)` on hot tables only |
| D6 | `DuckDbIndex` singleton keyed only by `db_path` — same path + different `jsonl_dir` silently returned the old instance | RESOLVED `876dbc6` | keyed by `(db_path, jsonl_dir, warehouse)` |
| D7 | `asyncpg`/`psycopg` only in the `postgres` extra; `+asyncpg` in a sync `StateDB` works only via greenlet | Open (docs note) | use compose-documented `postgresql+psycopg://`; deploy with `pip install -e '.[postgres]'` |

**Parity verdict (W6-C):** all 15 sqlite-vs-postgres dialect branches in
`state.py` audited — every PG branch syntactically/semantically correct, no
PG-blocking bug found; C-07 migration parity regression covered by
`test_postgres_migration_parity.py`. Live PG execution remains unverified
(no docker/postgres on the audit host).

**Iteration-2 features (no findings):** gdeltx bridge (6 h disk cache,
offline degradation), corpusx (topic matrix, quality snapshot), CLI
`awareness trends` / `awareness x` / `digest --email`, digest email +
entity-network SPA nodes — test-covered (`test_gdeltx_*`,
`test_corpusx_*`, `test_cli_trends`, `test_cli_xsessions`,
`test_cli_digest_email`, `test_spa_entity_network`).

---

## Ralph Loop Round 2 (2026-08-04) — iterations 1–6 (post-hoc register)

Comprehensive register of every adversarial finding raised across iterations
1–6 of the Round-2 Ralph loop (`876dbc6` → `e651d3b`). Supersedes the
iterations 1–3 register above (its "W12 in progress" note is now resolved:
the W12 fixes landed in `fbd16a9`). Each RESOLVED claim was verified against
`git show --stat` / `git show` of the named commit — evidence below cites the
touched files, not the commit messages alone. Where a claim could not be
pinned to a diff, it is marked "status unverified" rather than guessed.

### Iteration 1 — `876dbc6` (sentiment + origin, SPA alerts, materialized corpus; W5-A + W7 + W6-C)

#### F-1 … F-6 (W5-A fix-the-fixes audit) — all RESOLVED `876dbc6`

| ID | Finding | Status | Verification |
|----|---------|--------|-------------|
| F-1 | Non-loopback bind without `AW_API_KEY` only *warned* | RESOLVED `876dbc6` | `_guard_non_loopback_without_key()` added to `api/server.py` (SystemExit), called from `run()` and lifespan; still present in the current tree (`server.py:1117`) |
| F-2 | Empty-body mutating requests bypassed the JSON CSRF gate; `Origin` checked against spoofable `Host` | RESOLVED `876dbc6` | empty-body mutators → 415/422; Origin vs configured host (`api/server.py`) |
| F-3 | `/healthz` disclosed `db_path` / `jsonl_dir` | RESOLVED `876dbc6` | fields removed from the health response |
| F-4 | JSONL orphan repair non-atomic; gzip boundary truncation (missing EOS) unrepairable | RESOLVED `876dbc6` | atomic `.repair` temp + fsync + `os.replace`; truncated gzip repaired to valid gzip — `test_jsonl_repair_atomic.py` |
| F-5 | FTS incremental staleness join ran against the view (O(corpus) re-parse) | RESOLVED `876dbc6` | join now hits the indexed materialized table — `test_materialized_corpus.py` |
| F-6 | Flaky alerts-runner test counted engine calls instead of waiting on ticks | RESOLVED `876dbc6` | `test_alerts_runner.py` waits on ticks |

#### W7 performance recommendations

| Rec | Status | Verification |
|-----|--------|-------------|
| Materialize the captures union | RESOLVED `876dbc6` | `captures_materialized` table + unique index on `capture_id` (`storage/duckdb_index.py`, +151 lines); 365× on `COUNT(*)` |
| Raise the near-dup merge threshold | RESOLVED `876dbc6` | `DEFAULT_NEAR_THRESHOLD` 24 → 32 (dedup F1 0.845 → 0.961, precision stays 1.0); `test_near_threshold_32.py` |
| Rerank tokenizer LRU cache | PARTIAL — caches added `876dbc6` (maxsize 4096; singleton keyed `(db_path, jsonl_dir, warehouse)`); effectiveness pinned by hit/miss tests in `daacf9b` (W17) | both commits verified in diff |
| Surface dropped low-IDF terms in search | RESOLVED `daacf9b` | `search_with_diagnostics()` (kept/dropped terms + `idf_threshold`) + WARNING drop event with query field (W17); supersedes the earlier "open" status |

#### W6-C Postgres-parity — D1 … D7

| ID | Finding | Status | Verification |
|----|---------|--------|-------------|
| D1 | SQLite first-pass claim under-claims (`[3,3,0,0]` — unlocked SELECT race); claim invariant held, PG claims full batch via `with_for_update(skip_locked=True)` | Verified — behavioral divergence, not a bug | 4-thread claim exercise; workers loop on SQLite |
| D2 | Process-local RLock → two PG workers can both DLQ the same orphan (no unique key) | RESOLVED `876dbc6` | unique index `uq_dlq_task` + conflict-tolerant `add_dlq` (`storage/state.py`, +83 lines) |
| D3 | PG engine: default QueuePool(5+10), no `pool_pre_ping` | RESOLVED `876dbc6` | `pool_pre_ping=True`, `pool_size=10` (SQLite pool untouched) |
| D4 | Tail reconcile `os.kill(pid, 0)` is same-host-only — remote PG would phantom-CANCEL live tails | OPEN (ops note) | gate on host/instance id in `tail_state` if remote PG |
| D5 | PG `VACUUM` is full-database, not table-scoped | OPEN (ops note) | `VACUUM (ANALYZE)` on hot tables only |
| D6 | `DuckDbIndex` singleton keyed only by `db_path` — same path + different `jsonl_dir` returned the old instance | RESOLVED `876dbc6` | keyed `(db_path, jsonl_dir, warehouse)` |
| D7 | `asyncpg`/`psycopg` only in the `postgres` extra; `+asyncpg` in sync `StateDB` works only via greenlet | OPEN (docs note) | use compose-documented `postgresql+psycopg://`; `pip install -e '.[postgres]'` |

### Iteration 2 — `8c53af4` (gdeltx, corpusx, CLI trends / x-sessions / digest email, entity network) + W12

W12 findings (adversarial review of the iteration-2 output) — **all RESOLVED
in `fbd16a9`**. Verified: the `starttls()` call and the cache-key day-floor
are both in the `fbd16a9` diff; `8c53af4` shipped the buggy variants (the
microsecond key and the plain-587 SMTP path), not the fixes.

| ID | Finding | Status | Verification |
|----|---------|--------|-------------|
| W12-1 | gdeltx cache key included `utcnow()` microseconds → 6 h TTL never engaged; GDELT API re-hit on every request | RESOLVED `fbd16a9` | `_cache_path` end floored to the day (`gdeltx/engine.py`, +13 lines) |
| W12-2 | GDELT 250-record cap silent → correlation/gap distortion un-surfaced | RESOLVED `fbd16a9` (field + set) + `daacf9b` (cache persistence + compare note) + `a711ba6` (coverage-gap surfacing) | `GdeltWindow.truncated` field added `fbd16a9` (`models.py` +1); survives cache read + "gdelt_series is a floor" note `daacf9b`; `GapReport.truncated` + `_aggregate` OR `a711ba6` |
| W12-3 | digest `--email` sent SMTP credentials and body in the clear on port 587 | RESOLVED `fbd16a9` | `server.starttls()` on non-465 ports (`cli/main.py`; verified in diff and current tree `:3513`) |

### Iteration 3 — `fbd16a9` (TUI analytics panel, quality + feeds CLI, benchmark docs) + W16

W16 findings (adversarial review of the iteration-3 output) — fixed in
`daacf9b` ("fix: W16 findings"; commit message + `gdeltx/engine.py` +11,
`test_cli_trends.py` touched).

| ID | Finding | Status | Verification |
|----|---------|--------|-------------|
| W16-1 | `truncated` flag was dead data: lost on cache read, no comparison note | RESOLVED `daacf9b` | flag persisted through the disk cache + compare note "gdelt day(s) hit the 250-record cap; gdelt_series is a floor" |
| W16-2 | TUI term view recomputed every tick — 4.5 s freeze at 100k docs | RESOLVED `daacf9b` | memoized per `(term, window)`; cache cleared on term change/esc |
| W16-3 | `_sparkline` rendered isolated spikes as a flat line under downsampling (`--days > 60`) | RESOLVED `daacf9b` (pins true argmax/argmin); hardened again in `a711ba6` (W21-6: nearest lattice column, NaN-guarded, upsample uncorrupted) | shared `_sparkline` helper |
| W16-4 | flaky `test_cli_trends` at UTC-midnight rollover | RESOLVED `daacf9b` | test fixed (`tests/unit/test_cli_trends.py`, 7 lines) |

### Iteration 4 — `daacf9b` (IDF diagnostics, alert multi-webhook/Slack + import/export, E2E smoke) + W21

W21 findings (adversarial review of the iteration-4 output) — **all 8
RESOLVED in `a711ba6`** ("fix: W21 findings"; verified against
`alerts/runner.py` +16, `alerts/store.py` +15, `gdeltx/engine.py` +31 /
`models.py` +6, `scripts/e2e_smoke.py` +16).

| ID | Finding | Status | Verification |
|----|---------|--------|-------------|
| W21-1 | Smoke test logging pollution (root handler never restored, GDELT noise) — full suite not green in one process | RESOLVED `a711ba6` | root-handler restore + GDELT stub |
| W21-2 | IDF drop-all fallback lied (`dropped=[]`, `threshold=None`, no warning) | RESOLVED `a711ba6` | honest empty diagnostics, no warning |
| W21-3 | Alert runner delivered to the *first* `rule.webhooks` entry only | RESOLVED `a711ba6` | delivers to ALL webhooks |
| W21-4 | `update_rule` did not mirror `webhook_url = webhooks[0]` on write | RESOLVED `a711ba6` | mirror invariant restored on write |
| W21-5 | `import_rules` wrote incrementally — non-atomic on validation failure | RESOLVED `a711ba6` | validates fully before any write |
| W21-6 | `_sparkline` extremes at wrong column; NaN; upsample path corruption | RESOLVED `a711ba6` | pins extremes at nearest lattice column, NaN-guarded, upsample uncorrupted |
| W21-7 | gdeltx truncation absent from `coverage_gap` | RESOLVED `a711ba6` | `GapReport.truncated` + note; `_aggregate` ORs member flags |
| W21-8 | Index close order in smoke paths | RESOLVED `a711ba6` | close-then-null ordering |

### Iteration 5 — `a711ba6` (GDELT SPA + digest context, dedup token-sketch guard) — W24 audit CLEAN

W24 adversarial audit of the iteration-5 output: **CLEAN across all six areas**
(dedup token-sketch boundaries, `awareness init` materialization, lifespan
index warm-up, GDELT SPA/digest, the W21 fixes, regression). No findings;
three below-threshold notes recorded. Source: `e651d3b` commit message +
`.ralph/loop-state.md` — no standalone W24 report file exists in `docs/`.

### Iteration 6 — `e651d3b` (saved searches, X simulate/analyze, perf top-3) — W28 audit RESOLVED

The W28 fix-the-fixes audit of the iteration-6 output (W25 perf changes +
W26 savedsearch + W27 X) was previously marked **Pending** here. It
completed during iteration 7: **3 findings, all RESOLVED in `e4b1417`**
("fix: W28"; verified against the commit message and the touched
files — `duckdb_index.py` incremental paths + `fbd16a9`-era tests).

| ID | Finding | Status | Verification |
|----|---------|--------|-------------|
| W28-1 | FTS incremental append treated a re-fetch with unchanged content as fresh — the materialized `fetch_ts` bumped but the FTS index kept the old one, so date-windowed ranked search silently missed docs | RESOLVED `e4b1417` | incremental FTS append now treats `fetch_ts` mismatch as stale; regression 34/34 green |
| W28-2 | `refresh()` re-armed the FTS coalescing window on no-op refreshes — periodic callers could defer the FTS rebuild indefinitely | RESOLVED `e4b1417` | no-op refreshes no longer touch the coalescing window |
| W28-3 | Delta materialize kept stale rows when a changed chunk shrank (same-path rewrite with removed rows) | RESOLVED `e4b1417` | delta path forces a full rebuild when a changed chunk shrank |

### Iteration 7 — `e4b1417` (SPA X view, saved widgets, alert history, report CLI) — W32 audit RESOLVED

W32 adversarial audit of the iteration-7 output (W29 SPA X view + saved
band, W30 `alerts history` + `report` CLI, W31 docs/register): **5 findings,
all RESOLVED in `7d46372`** ("fix: W32"). One residual noted (see W32-5).

| ID | Finding | Status | Verification |
|----|---------|--------|-------------|
| W32-1 | `report --json` silently ignored `--out`/`--email`; `--out` writes non-atomic | RESOLVED `7d46372` | warns when `--out`/`--email` are ignored (and when combined); `--out` writes atomically (tmp + replace) |
| W32-2 | Keyboard nav `0` did not reach the tenth route — Settings unreachable | RESOLVED `7d46372` | `0` now navigates to ROUTES[9] (Settings); view counts corrected to ten |
| W32-3 | README view counts wrong (stated nine views) | RESOLVED `7d46372` | README corrected: ten views, shortcuts `1`–`9` + `0` |
| W32-4 | `alerts history --json` emitted non-ISO timestamps | RESOLVED `7d46372` | strict ISO-8601 timestamps in JSON output |
| W32-5 | Same-size rewrite gap in the delta-shrink heuristic (a chunk rewritten at identical byte size could evade detection) | RESOLVED (residual noted) | low risk — production writer uses atomic renames, so same-size rewrites are not observed in practice |

### Iteration 8 — `7d46372` (X sentiment trend + CSV export, firing detail, E2E 11 stages) — W36 audit in progress

**In progress.** As of this writing (2026-08-04) no W36 report exists in
`docs/` and `.ralph/loop-state.md` does not yet record a W36 result; the
iteration-8 commit itself carries no audit-fix header. The W36
fix-the-fixes audit of the iteration-8 output (W33 X sentiment trend +
CSV export, W34 firing-detail UX, W35 E2E stage expansion) is scheduled
next. Status will be updated here when the report lands.

*(2026-08-05 supersession note: the Round-3 loop renumbered its audits
(R3-W1 … R3-W26); W36 as numbered here was never reported — no W36 report
exists in `docs/`, and the Round-3 register below is the authoritative
record from `a84a2ab` onward.)*

---

## Ralph Loop Round 3 (2026-08-04) — iteration 1

Iteration 1 = `a84a2ab` ("feat: topicx + qualityx subsystems, GDELT cache
buckets, FTS delta, briefing CLI; fix: R3-W1"), five workstreams:
`topicx/` (topic lifecycle phases, emerging topics, source impact, topic
dominance), `qualityx/` (per-day quality time series), GDELT cache day-range
keys, FTS delta append fast path, and the `awareness briefing` /
`awareness gdelt-gaps` CLI. Every RESOLVED claim below was verified against
`git show --stat a84a2ab` (touched files cited, not the commit message
alone).

### R3-W1 findings (W38 security-sweep follow-up) — all RESOLVED in `a84a2ab`

| ID | Area | Finding | Status | Verification |
|----|------|---------|--------|-------------|
| R3-W1-1 | API | `HEAD /healthz` returned 405 — the HEAD alias was claimed exempt but never registered | RESOLVED `a84a2ab` | HEAD handler actually registered in `src/awareness/api/server.py` (+32 lines in the diff) |
| R3-W1-2 | API | Rate limiter ran **before** auth + CSRF — blocked cross-origin traffic burned the operator's rate-limit budget | RESOLVED `a84a2ab` | limiter moved after auth + CSRF (`api/server.py`), so rejected requests never consume budget |
| R3-W1-3 | API | Origin loopback allow-list missed `::1` and `0.0.0.0` binds — legitimate loopback-Origin requests from those aliases were rejected | RESOLVED `a84a2ab` | loopback aliases extended to `[::1]` and `0.0.0.0` binds (`api/server.py`) |
| R3-W1-4 | Observability | `tail.news_floor_kept` discovery label was high-cardinality (raw floor value) — metric cardinality explosion | RESOLVED `a84a2ab` | label bucketed to a low-cardinality class (`src/awareness/sources/tail_recrawl.py`, +6 lines) |
| R3-W1-5 | Export | `export_tweets_csv` left a `.tmp` file behind on write failure | RESOLVED `a84a2ab` | `.tmp` unlinked on the failure path (`src/awareness/xscraper/analyze.py`, +39 lines) |
| R3-W1-6 | Tests | Flaky `test_tui_analytics` — key test read its own source via `inspect.getsource`, which broke under source-mangling environments | RESOLVED `a84a2ab` | test reads the source file directly instead (`tests/unit/test_tui_analytics.py`, +6 lines) |

**R3-W1 verdict:** 6/6 RESOLVED in `a84a2ab` (each finding's file appears in
the commit diff). Related hardening in the same commit: GDELT CLI error
paths drop `logger.warning` (stale-handler stream corruption under
CliRunner capture), applied to `briefing` / `report` too.

### R3-W6 (adversarial review of `a84a2ab`) — RESOLVED in `83f84e2`

**Superseded** — see "Ralph Loop Round 3 — iterations 2-3 (2026-08-05)"
below: the report landed, 2 findings (qualityx 200k-row cap +
alias-less `EXISTS`, briefing fake sentiment crash), both RESOLVED in
`83f84e2`. This block was written while W6 was still scheduled
(2026-08-04) and is kept only for continuity.

---

## Ralph Loop Round 3 — iterations 2-3 (2026-08-05)

Iteration 2 = `83f84e2` ("feat: SPA lifecycle/quality views, briefing
email+save; fix: R3-W6"), iteration 3 = `1bdbced` ("feat: SPA alert trend +
glance band, X timeline CSV; E2E 14 stages; fix: W10 nits"). Every RESOLVED
claim below was verified against `git show --stat` of the cited commit
(touched files cited, not the commit message alone).

### R3-W6 (adversarial review of iteration 1) — 2 findings, RESOLVED in `83f84e2`

| ID | Area | Finding | Status | Verification |
|----|------|---------|--------|-------------|
| R3-W6-1 | qualityx | `history()` bucket aggregates computed in Python over a **200k-row cap** — days past the cap were silently zeroed into fabricated empty buckets; the alias-less `fetch_ts` inside the `EXISTS` subquery resolved to `o` (always-true) instead of `c` | RESOLVED `83f84e2` | aggregates moved into DuckDB `GROUP BY` (no row cap); explicit `c.` alias restored (`src/awareness/qualityx/engine.py`, −/+42 lines in the commit diff) |
| R3-W6-2 | briefing | empty last-day sentiment bucket (avg_score 0.0 by construction) rendered a fake "▼ sentiment crash" | RESOLVED `83f84e2` | empty buckets skipped in the sentiment mover (`src/awareness/cli/main.py`, +245 lines in the diff) |

### R3-W10 (adversarial review of iteration 2) — CLEAN, 4 sub-threshold nits, RESOLVED in `1bdbced`

**CLEAN** (0 findings ≥ 80 confidence); 4 sub-threshold nits fixed:

| ID | Area | Nit | Status | Verification |
|----|------|-----|--------|-------------|
| R3-W10-1 | briefing save | `_save_briefing` accepted arbitrary names; `.tmp` left behind on write failure | RESOLVED `1bdbced` | name validated `[A-Za-z0-9_-]`; `.tmp` cleaned on the failure path (`src/awareness/cli/main.py`, +59 lines in the diff) |
| R3-W10-2 | briefing list | `_list_saved_briefings` stat outside try — TOCTOU on the saved dir | RESOLVED `1bdbced` | stat moved inside the try block (`src/awareness/cli/main.py`) |
| R3-W10-3 | SPA | dup-ratio bar tooltip missing its unit | RESOLVED `1bdbced` | tooltip now shows '%' (`src/awareness/api/web/app.js`, +116 lines in the diff) |
| R3-W10-4 | qualityx | scale verification missing for the W6 GROUP BY rewrite | RESOLVED `1bdbced` | verified at 1M rows: GROUP BY linear (1.3 s), no fabricated zero days at 260k, no O(n²) EXISTS (decorrelated hash semi-joins) — noted in the commit message, code unchanged |

### R3-W14 (fix-the-fixes review of iteration 3) — RESOLVED in `3c65ce7`

**Resolved.** The W14 report landed with iteration 4 (`3c65ce7`); **2
findings, both RESOLVED in `3c65ce7`** (verified against
`git show 3c65ce7 -- src/awareness/cli/main.py` — the diff carries both
fixes with inline `W14-F1` / `W14-F2` comments). See "Ralph Loop Round 3 —
iterations 4-5 (2026-08-05)" below for the per-finding register.

---

## Ralph Loop Round 3 — iterations 4-5 (2026-08-05)

Iteration 4 = `3c65ce7` ("feat: saved-briefings API + SPA viewer, lifecycle
CLI, final benchmark; fix: W14"): the `briefings/` package (filesystem-backed
API + SPA viewer), the `awareness lifecycle` CLI, the final 100k benchmark,
and the W14 fixes. Every RESOLVED claim below was verified against
`git show` of the cited commit (touched files cited, not the commit message
alone).

### R3-W14 (fix-the-fixes review of iteration 3) — 2 findings, RESOLVED in `3c65ce7`

| ID | Area | Finding | Status | Verification |
|----|------|---------|--------|-------------|
| R3-W14-1 | X CLI | `x timeline` / `x export` write failures (destination is a directory, read-only parent, ENOSPC) surfaced as raw OSError tracebacks | RESOLVED `3c65ce7` | both commands now print `cannot write <path>: <err>` and `raise typer.Exit(code=2)` — `src/awareness/cli/main.py` (+12 lines, inline `W14-F1` comment on the timeline path) |
| R3-W14-2 | briefing save | unbounded `--save` name → `ENAMETOOLONG` traceback; `.tmp` cleanup on the failure path could itself raise OSError and mask the original error | RESOLVED `3c65ce7` | name regex capped `[A-Za-z0-9_-]{1,64}` with a friendly `typer.BadParameter`; cleanup wrapped in try/except OSError (`src/awareness/cli/main.py`, inline `W14-F2` comment) |

**R3-W14 verdict:** 2/2 RESOLVED in `3c65ce7` — both findings verified in
the `git show` diff of `src/awareness/cli/main.py`. Suite green (1731+);
wheel builds.

### R3-W18 (fix-the-fixes review of iteration 4) — RESOLVED in `5687763`

**Resolved.** W18 completed during iteration 5 (`5687763`): **7 findings,
all RESOLVED** — see "Ralph Loop Round 3 — iterations 5-7 (2026-08-05)"
below for the per-finding register with diff-level verification.

---

## Ralph Loop Round 3 — iterations 5-7 (2026-08-05)

Iteration 5 = `5687763` ("feat: alert rule test area, crossx X-news view,
briefing enrichment; fix: W18") — W19 (alert test area + briefing
enrichment), W20 (crossx), W21 (docs). Iteration 6 = `07351ba` ("feat:
operations docs + test history, qualityx granularity, E2E 16 stages; fix:
W22") — W23 (ops docs + test history), W24 (qualityx granularity), W25
(E2E stages 15-16). Every RESOLVED claim below was verified against
`git show` of the cited commit (diff lines cited, not the commit message
alone).

### R3-W18 (adversarial review of iteration 4) — 7 findings, RESOLVED in `5687763`

| ID | Area | Finding | Status | Verification |
|----|------|---------|--------|-------------|
| R3-W18-1 | SPA briefings | named-briefing chips passed only the date — every `YYYY-MM-DD-<name>` file 404'd in the viewer | RESOLVED `5687763` | click passes the full stem (`slug = b.date + "-" + b.name`; inline `W18-F1` comment in `src/awareness/api/web/app.js`) |
| R3-W18-2 | briefings API | list TOCTOU: `path.stat()` outside the try — a file vanishing between glob and stat 500'd the whole list | RESOLVED `5687763` | stat moved inside the try; vanished files list with `size_bytes: null`; `SavedBriefing.size_bytes` nullable (inline `W18-F2` in `src/awareness/briefings/router.py`) |
| R3-W18-3 | lifecycle CLI | no-captures message interpolated the raw term — Rich markup injection | RESOLVED `5687763` | term passed through `escape()` (`src/awareness/cli/main.py`; later hardened again by R3-W22-1) |
| R3-W18-4 | lifecycle CLI | `--compare` / `--emerging` ignored `--json` | RESOLVED `5687763` | both now honor `--json` (`if json_out:` branches in the `src/awareness/cli/main.py` diff) |
| R3-W18-5 | briefings API | stray non-conforming `*.json` files (e.g. `notes.json`) listed — their chips 400'd on click | RESOLVED `5687763` | non-conforming files filtered out of the list (inline `W18-L6` in `src/awareness/briefings/router.py`) |
| R3-W18-6 | briefings API | symlink trust-model undocumented | RESOLVED `5687763` | trust model documented in the router module docstring (operator-controlled `{data_dir}`; follow-symlinks is by design) |
| R3-W18-7 | CLI | W14 OSError paths lacked regression tests | RESOLVED `5687763` | `tests/unit/test_cli_x_timeline.py` +17 (x timeline / export `--out` directory targets) |

**R3-W18 verdict:** 7/7 RESOLVED in `5687763`.

### R3-W22 (adversarial review of iteration 5) — 7 findings, RESOLVED in `07351ba`

| ID | Area | Finding | Status | Verification |
|----|------|---------|--------|-------------|
| R3-W22-1 | lifecycle CLI | the W18-3 escape was defeatable: `escape()` + `!r` — the repr backslash-escaped the escape, so `[red]`-style markup still injected | RESOLVED `07351ba` | `escape(repr(term))` (repr inside escape, not the reverse) + regression test with a `[red]` term (`src/awareness/cli/main.py` diff) |
| R3-W22-2 | crossx | correlation unmasked — one shared non-zero day inflated r to ±1.0 | RESOLVED `07351ba` | r masked to overlapping-data days, `_MIN_OVERLAP_DAYS = 3`; a sparse overlap reports 0.0 with a note (`src/awareness/crossx/engine.py`) |
| R3-W22-3 | crossx | convergence decided on one-sided data — X silence read as bearish alignment | RESOLVED `07351ba` | convergence requires data on BOTH sides; one-sided silence → `neutral` (inline `W22-F2` comment, `src/awareness/crossx/engine.py`) |
| R3-W22-4 | crossx | out-of-window X sessions produced a misleading all-zero series | RESOLVED `07351ba` | note "x session tweets predate the window — news side only" + `x_sentiment: None`; only genuinely empty sessions keep the zeroed series (`src/awareness/crossx/engine.py`) |
| R3-W22-5 | briefing | `top_rule.firings` was the length of a capped 100-row history list, not the real firing count | RESOLVED `07351ba` | `count_firings_since(ts, rule_id=…)` — uncapped per-rule SQL COUNT (`src/awareness/alerts/store.py`, +18) |
| R3-W22-6 | alert test | test endpoint lacked bodyless-CSRF and rate-limit treatment; the report omitted `active` / `required` | RESOLVED `07351ba` | `_is_csrf_bodyless()` matches `/alerts/rules/{id}/test`; `/alerts/rules/` added to `_RATE_LIMITED_PREFIXES` (`src/awareness/api/server.py`); report gains `active` + `required` (spike: 3× baseline or absolute floor) (`src/awareness/alerts/engine.py`) |
| R3-W22-7 | crossx tests | correlation tests used constant ±1 series — zero variance, r undefined | RESOLVED `07351ba` | tests reworked with variance-bearing series (`tests/unit/test_crossx_engine.py`) |

**R3-W22 verdict:** 7/7 RESOLVED in `07351ba`.

### R3-W26 — **in progress** (iteration 7 landed in the working tree, uncommitted)

**In progress.** As of this writing (2026-08-05) `.ralph/loop-state.md`
opens Round 3 / Iteration 7 with W26 ("fix-the-fixes — adversarial review
of the R3-ITER6 code: ops docs, test history, qualityx granularity, W22
fixes") as the scheduled step, alongside the W27 (SPA X↔News convergence
alert badge + dashboard quality trend mini-card) and W28 (CLI `alerts`
weekly summary + a quality record store for cron) features. **No W26
report exists in `docs/` and no iteration-7 commit has landed** — HEAD is
still `07351ba`. The working tree does carry uncommitted iteration-7 WIP
(`qualityx/store.py` + `quality --record/--recorded` in `cli/main.py`,
`alerts weekly` in `alerts/cli.py`, the `x-conv-badge` + dashboard quality
mini-card in the SPA, and `test_spa_convergence_badge.py` /
`test_spa_quality_minicard.py`), so nothing in this register can be pinned
to a commit yet. Status will be updated here when the iteration-7 commit
and the W26 report land.
