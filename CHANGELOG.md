# Changelog

All notable changes to Awareness are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); versioning is
[SemVer](https://semver.org/).

## Unreleased / [0.2.1] — 2026-08-04

Round 3, iteration 1 (`a84a2ab`) — topic lifecycle and quality time-series
subsystems, GDELT cache and FTS maintenance fixes, and the briefing CLI. The
R3-W1 adversarial sweep (6 findings) is fully resolved in this release; see
[`docs/AUDIT_FINDINGS_2026-08-03.md`](docs/AUDIT_FINDINGS_2026-08-03.md).

### Features

- **Topic lifecycle** (`/topicx/*`) — per-term lifecycle phases
  (EMERGING / EXPANDING / PEAKING / DECLINING / DORMANT / STABLE from a
  7-day slope + peak, strict precedence), emerging-topics corpus scan,
  replication-weighted source impact, and per-domain topic dominance:
  `/topicx/lifecycle`, `/topicx/emerging`, `/topicx/impact`,
  `/topicx/dominance`.
- **Quality time series** (`/qualityx/*`) — per-day duplicate / near-dup
  ratios, new domains (first-ever capture via `MIN(fetch_ts)`), and capture
  rate, zero-filled over calendar buckets: `/qualityx/history` +
  `/qualityx/current`, plus `awareness quality --history [DAYS]` with a
  sparkline.
- **`awareness briefing`** — one-morning read: z-score movers, top terms,
  new domains, sentiment shift, alert activity, and GDELT coverage gaps
  (`--days/--top/--emerging/--json/--no-gdelt`).
- **`awareness gdelt-gaps`** — the coverage-gap report standalone
  (`--terms a,b --days N --json`).

### Performance

- **GDELT cache day-range keys** — the disk cache key is now
  `floor_to_day(start)` + `floor_to_day(end)` instead of the raw timestamps
  or `window_days`, so identical ranges share one cache entry (the
  cache-bypass where different `window_days` re-hit the GDELT API is closed).
- **FTS delta append fast path** — pure-addition batches INSERT into the
  FTS index instead of rebuilding the whole inverted index; edits/removals
  still full-rebuild (rare). At 20k corpus + 1k delta: FTS-internal delta
  rebuild **0.137 s vs 2.243 s full build (~16×)** —
  [`docs/benchmarks/perf_iter9_report.md`](docs/benchmarks/perf_iter9_report.md).

### Bug fixes

- `HEAD /healthz` no longer 405s — the HEAD alias is actually registered
  (R3-W1-1).
- Rate limiter moved **after** auth + CSRF — blocked cross-origin traffic
  no longer burns the operator's rate-limit budget (R3-W1-2).
- Origin loopback allow-list extended to `[::1]` and `0.0.0.0` binds
  (R3-W1-3).
- `tail.news_floor_kept` discovery label bucketed to low cardinality —
  no metric-cardinality explosion (R3-W1-4).
- `export_tweets_csv` cleans up its `.tmp` file on write failure
  (R3-W1-5).
- GDELT CLI error paths drop `logger.warning` (stale-handler stream
  corruption under CliRunner capture) — briefing/report hardened too.

### Testing

- Suite grew ~107 tests (topicx engine/API, qualityx engine/API, FTS delta
  append, FTS shard append, GDELT cache buckets, briefing + gdelt-gaps +
  quality-history CLI); full suite green (~1,700+).
- Flaky `test_tui_analytics` key test now reads its source file directly
  instead of `inspect.getsource` (R3-W1-6).

### Ralph Loop Round 3 — iterations 2-3 (`83f84e2`, `1bdbced`)

SPA lifecycle/quality views, briefing email + save, X timeline export, E2E
14 stages, and the final 100k-doc benchmark. R3-W6 (2 findings) and R3-W10
(CLEAN + 4 nits) are fully resolved in these commits; see
[`docs/AUDIT_FINDINGS_2026-08-03.md`](docs/AUDIT_FINDINGS_2026-08-03.md).

- **SPA lifecycle band** — topic phase badge, stats, chart, emerging-topic
  chips click-to-analyze, and a source-impact table; **quality history
  band** on the dashboard (dup-% + new-domains charts, per-day table,
  12-tick refresh guard) (`83f84e2`).
- **SPA alert-trend mini-chart** — 14-day zero-filled firing trend in the
  alerts view (pure function, node-tested); **"Today at a glance" band**
  with 24 h alert KPIs + emerging-topic deep links (`1bdbced`).
- **`awareness briefing --email`** — reuses the digest SMTP machinery
  (STARTTLS, env fallbacks, markdown body); **`--save` / `--list-saved`** —
  atomic `{data_dir}/briefings/YYYY-MM-DD[-name].json`, cron-friendly
  `--save --json` (`83f84e2`).
- **X timeline CSV** — shared scoring core (analyze + timeline provably
  agree), `export_timeline_csv` (per-day pos/neg/neutral/avg),
  `awareness x timeline` CLI, `GET /x/sessions/{id}/timeline.csv`, and a
  sentiment-distribution bar in `x analyze` (`1bdbced`).
- **E2E stages 12–14** — topicx lifecycle/emerging/impact, qualityx
  history/current, briefing `--save/--json`; 14/14 stages PASS (`1bdbced`).

**Fixes (R3-W6, 2 findings):** qualityx history bucket aggregates now
computed directly in DuckDB (`GROUP BY`, no 200k-row cap — old days were
silently zeroed past the cap) with the explicit `c.` alias restored (the
alias-less `fetch_ts` inside `EXISTS` was an always-true bug); briefing
sentiment skips the empty last-day bucket (no more fake "▼ sentiment
crash").

**Fixes (R3-W10, CLEAN + 4 nits):** `_save_briefing` name validated
`[A-Za-z0-9_-]` with `.tmp` cleaned on failure; `_list_saved_briefings`
TOCTOU closed (stat inside try); dup-ratio tooltip shows its '%' unit;
qualityx GROUP BY scale-verified at 1M rows (linear 1.3 s, no fabricated
zero days at 260k, no O(n²) EXISTS).

**Performance (final 100k benchmark):** at `1bdbced`, every warm op ≤ 1.7 s
under a contended host (load 41–75); domain_rank **×31.6** vs Round 1,
term_frequency ×2.1, detect_spikes ×2.0, export 10k rows in 1.319 s; FTS
delta maintenance search-wall 5.57 s (FTS-internal 0.466 s) —
[`docs/benchmarks/perf_final_round3.md`](docs/benchmarks/perf_final_round3.md).

---

## [0.2.0] — 2026-08-04

Delivered by the **Ralph Loop Round 2** (iterations 1–8, commits
`876dbc6` → `7d46372`). Each claim below is pinned to the iteration that
shipped it; every W-series audit finding referenced is recorded with its
resolution status in [`docs/AUDIT_FINDINGS_2026-08-03.md`](docs/AUDIT_FINDINGS_2026-08-03.md).

### Security

- **API key auth** — `AW_API_KEY` gates the HTTP control plane behind
  `Authorization: Bearer`; binding to a non-loopback interface **refuses to
  start** (`SystemExit`) without a key, enforced again at lifespan startup
  (iter 1, W5-A F-1).
- **CSRF JSON enforcement** — state-changing requests must be
  `application/json`; empty-body mutators are rejected (415/422) and the
  `Origin` header is checked against the configured host, not the spoofable
  `Host` header (iter 1, W5-A F-2).
- **SSRF gates** — all untrusted URLs (feed seeds, alert webhooks, redirect
  hops, sitemap recursion) pass the `is_public_http_url` gate: no
  loopback/private/link-local/metadata hosts, no userinfo, DNS must resolve
  to globally routable addresses (iter 1, W5-A; iter 4, W21).
- **Path confinement** — config writes (`data_dir`, `tail_seed_file`, …)
  must resolve inside the project root without `..` segments, and `data_dir`
  may not point at an existing non-directory (iter 1, W5-A).
- **Digest email STARTTLS** — `awareness digest --email` upgrades to
  STARTTLS on non-465 ports before any SMTP authentication, so credentials
  and the digest body are never sent in the clear (iter 2/3, W12-3).
- **Alert webhook validation** — webhook URLs validated against the
  public-host gate on rule create/update and re-checked at every delivery
  (iter 4, W21-3).
- **Secret masking** — `/healthz` and `/settings/schema` no longer disclose
  `db_path`, `jsonl_dir`, `redis_url`, or `state_db_url`; URL userinfo is
  redacted from logs and responses (iter 1, W5-A F-3; round 1 H-21).

### Bug fixes

- `tail stop` now actually stops: the reseed loop breaks on COMPLETED, so a
  detached tail no longer fetches forever (iter 1; round 1 C-01).
- CC-WET crawl IDs resolved against the live Common Crawl index instead of
  fabricated odd-week IDs (~90% of real crawls were missed; iter 1; C-02).
- Drain detection respects retry backoff and RUNNING orphans — no false
  COMPLETED, retries actually run (iter 1; H-01/H-02).
- FTS staleness closed on both fronts: incremental append detects
  content-hash changes (round 1 H-10) and re-fetched `fetch_ts` mismatches
  that silently dropped docs from date-windowed ranked search (iter 7, W28).
- JSONL gzip truncation repaired atomically (`.repair` temp + fsync +
  `os.replace`); truncated gzip is rebuilt into valid gzip and orphan temps
  are recovered instead of deleted (iter 1, W5-A F-4; round 1 H-11).
- Dedup correctness: band index widened 32×4 → 32×8 (1/256 selectivity,
  ~16× the silent-truncation ceiling), `near_threshold` clamped to the
  banding guarantee, and a token-sketch guard blocks boilerplate-template
  merges while genuine near-dups still fold (round 1 H-24/M-19; iter 5, W24).
- Entity trend month buckets drifted (Jun+31d = Jul 2) — now calendar-month
  arithmetic (round 1 second pass, `846a2bc`).
- Sparkline extremes pinned to the nearest lattice column under
  downsampling, NaN-guarded, upsample path uncorrupted (iter 3/4, W16-3 +
  W21-6).
- Alerts delivered to **all** configured webhooks, not just the first; rule
  update mirrors `webhook_url`; rule import is atomic (validates fully
  before any write) (iter 4, W21-3/4/5).
- `report --json` warns when `--out`/`--email` are ignored and writes
  atomically (tmp + replace); alerts history `--json` emits strict ISO-8601
  timestamps (iter 8, W32).
- SPA keyboard navigation: `0` reaches the tenth route — Settings was
  previously unreachable (iter 8, W32).

### Performance

- **Materialized corpus** — the deduped `captures` union is a real indexed
  table; `COUNT(*)` at 100k docs: 135 ms → **0.7 ms (~365×)** (iter 1, W7).
- **Incremental materialization** — pure additions INSERT only changed
  chunks; a 20k-doc refresh: 123 ms → **0.6 ms (~196×)**, full-rebuild
  fallback intact for removals/edits (iter 6, W25).
- **FTS coalescing** — dirty indexes defer rebuild inside a 30 s window,
  degrading to the table-backed prefix/substring path until batches
  coalesce into one rebuild (iter 6, W25).
- **Near-dup threshold 24 → 32** — dedup F1 0.845 → 0.961 at precision 1.0
  (iter 1, W7).
- **Signature guard** — 3-level directory-mtime short-circuit: 92 ms
  signature walk → 0.22 ms @100k (iter 6, W25).
- Warm analytics at 100k docs all ≤ ~1.2 s; domain_rank 16.4×, story_origins
  15.4×, export 3× vs the pre-materialization baseline (iter 5 probe,
  [`docs/benchmarks/perf_iter5_report.md`](docs/benchmarks/perf_iter5_report.md)).

### Features

- **Sentiment** (`/sentiment/*`) — finance lexicon (189 pos / 251 neg) with
  negation and intensity scoring: per-term sentiment over time + market-heat
  snapshot (iter 1).
- **Origin** (`/origin/*`) — breaking-news origin tracking from dedup
  groups: first publisher + lead minutes, publisher-firsts ranking (iter 1).
- **GDELT bridge** (`/gdelt/*`) — local-vs-GDELT correlation,
  coverage-gap detection with truncation surfacing, 6 h disk cache, offline
  degradation (iter 2–4, W12/W16/W21).
- **Corpus intelligence** (`/corpus/*`) — term × domain topic matrix +
  corpus-quality snapshot (iter 2).
- **Saved searches** (`/saved/*`) — SQLite-backed named-query store with
  pin/run; SPA Saved view + dashboard band, CLI group, API (iter 6/7).
- **X sessions** (`/x/*`) — session store, deterministic seeded simulation
  (no network), aggregated analysis (authors/terms/sentiment/timeline/
  engagement), **per-day sentiment trend** + **CSV export** (API attachment
  and `awareness x export`), CLI group (iter 6–8).
- **Alerts** (`/alerts/*`) — rule store, multi-webhook + Slack delivery,
  rule import/export, firing history (`/alerts/firings` + `awareness alerts
  history`), periodic runner (`AW_ALERTS_AUTOSTART`), SPA view with
  expandable firing detail (iter 1–8).
- **Analytics** (`/analytics/*`) — term frequency (day/week/month),
  z-score spikes, top terms, domains, languages, co-occurrence (iter 1).
- **Entities** (`/entities/*`) — dependency-free NER, trend, co-occurrence,
  Pearson correlation with lead-lag (iter 1).
- **Source intelligence** (`/source-intel/*`) — domain quality scoring,
  replication map, freshness report (iter 1).
- **Consumption** (`/consume/*`) — LLM dataset export (jsonl/parquet,
  streamed, atomic), weekly digest (JSON/markdown/email), GDELT context
  (iter 1–4).
- **CLI** — `search`, `browse`, `tui` (with analytics panel), `trends`,
  `quality`, `feeds`, `report`, `saved`, `x`, `alerts`, `config`, `backfill`,
  `tail`, `dedup`, `dlq`, `cloud`, `export`, `hf-push`, `shell` (iter 1–8).
- **SPA** — ten views (dashboard, captures, work, jobs, tail, analytics,
  alerts, saved, x, settings) with shortcuts `1`–`9` + `0`; X view, saved
  widgets, entity-network band, feed-health KPIs (iter 1–8).
- **E2E smoke** — 11 stages: init → ingest → query → analytics → API →
  alerts → digest → export → saved → X → report (iter 4–8).

### Testing

- Suite growth **1,209 → 1,701 tests** across the loop (per-iteration
  regressions included: W28 34/34, W32 clean).
- End-to-end smoke harness expanded from 8 to **11 stages**, wrapper asserts
  every stage; full suite green at every iteration boundary.
- New adversarial coverage: auth/SSRF/CSRF, materialization, signature
  guard, FTS coalescing, token-sketch dedup, gdeltx truncation, X
  simulate/analyze/export, alert runner, saved-search store.

---

## Changelog entry style (Ralph Loop Round 2)

Entries in this file are written the way the Round-2 loop wrote its
iteration registers:

- **One entry per release**, subsections grouped by impact (Security /
  Bug fixes / Performance / Features / Testing).
- **Every claim is pinned to a commit** (or a W-series audit ID) so it can
  be verified with `git log` / `git show` — never asserted from memory.
- **Statuses are honest**: fixed, open-with-note, or "in progress" — a
  claim that could not be pinned is marked rather than guessed.
- Feature lines name the concrete surface (endpoint prefix, CLI command,
  SPA view) so a reader can exercise the claim directly.
