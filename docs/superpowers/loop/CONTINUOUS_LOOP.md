# Awareness Continuous Development Loop

**Branch:** `loop/continuous-dev`  
**Started:** 2026-07-13  
**Stop condition:** User says stop / `touch .ralph/STOP` / explicit cancel.  
**Method:** subagent-driven-development + TDD + rotation (bug hunt → search → dedup → features).

## Rotation order (repeat forever)

1. **Bug hunt** — broken tests, NameErrors, silent data loss  
2. **Search system** — FTS, ranking, collapse, UX  
3. **Dedup / re-fetch prevention** — URL gate, banding, cross-source keys  
4. **New features / gaps** — product improvements  
5. *(repeat)*

## Completed (Cycle 1+)

| ID | Area | Task | Status |
|----|------|------|--------|
| C1-T1 | search/bug | API `_get_index` singleton | ✅ |
| C1-T2 | search/bug | Index `.jsonl.gz` staging | ✅ |
| C1-T2b | search/bug | Exclude `.tmp` from globs | ✅ |
| C1-T3 | dedup | Unify RSS/GDELT partition keys | ✅ |
| C1-T4 | dedup | Pre-fetch URL seen-gate | ✅ |
| C1-T5 | dedup | 32×4 SimHash banding | ✅ |
| C1-T6 | search | Collapse by parent_doc_or_dup_group | ✅ |
| C1-T7 | bug hunt | Import/schema/cc-wet/gdrive/idf/cli window | ✅ |
| C1-T8 | search | Multi-term prefix OR | ✅ |
| C1-T9 | search | FTS rebuild on content swap | ✅ |
| C1-T10 | search | Order-insensitive FTS + wire `_rerank` | ✅ |
| C1-T11 | search | Empty-result diagnostics | ✅ |
| C1-T12 | api | `/captures?unique=` collapse | ✅ |
| C1-T13 | search | Inclusive end-of-day windows | ✅ |
| C1-T14 | chore | Version 0.2.0 + architecture banding docs | ✅ |

## Completed (Cycle 2)

| ID | Area | Task | Status |
|----|------|------|--------|
| C2-T1 | dedup | Optional skip-store for tight NEAR_DUP (Hamming ≤12) | ✅ |
| C2-T2 | search | SPA mode/fields controls + real mode label | ✅ |
| C2-T3 | systems | Long-lived DuckDB search connection reuse (lock) | ✅ |
| C2-T6 | bug | Broader non-slow unit suite green | ✅ |
| C2-T7 | bug | Restore LID `detect_langs` + confidence gate | ✅ |
| C2-T8 | search | NULL-fill missing staging columns in captures view | ✅ |
| C2-T9 | config | `cc_wet_max_shards_per_crawl` so WET adapter registers | ✅ |
| C2-T10 | state | Import `timedelta` for fail_task and reaper paths | ✅ |
| C2-T11 | config | Align `user_agent` default schema vs Settings | ✅ |
| C2-T12 | test | Skip Redis lock tests when Redis unavailable | ✅ |
| C2-T13 | obs | Metrics for URL fetch skips and tight near-dup drops | ✅ |
| C2-T14 | ui | Surface empty-search diagnostics hints in Captures | ✅ |
| C2-T15 | search | Quoted queries match exact phrases | ✅ |
| C2-T16 | ui | Highlight last search terms in capture reader | ✅ |
| C2-T17 | api | Reset DuckDbIndex singleton after path-related settings | ✅ |
| C2-T18 | search | Label quoted matches as `mode=phrase` | ✅ |

## Completed (Cycle 3)

| ID | Area | Task | Status |
|----|------|------|--------|
| C3-T1 | systems | Persisted FTS restore + append-only captures_idx | ✅ |
| C3-T2 | dedup | Union-find cluster resolve for near-dups | ✅ |
| C3-T4 | search | Pagination correctness after collapse/rerank | ✅ |
| C3-T5 | scrape | Honor robots crawl-delay under concurrency (limiter race) | ✅ |
| C3-T6 | scrape | Seed discovery (robots Sitemap: + feed probe) | ✅ |

## Next backlog (Cycle 3+)

| ID | Area | Task | Status |
|----|------|------|--------|
| C3-T3 | systems | Streaming WET parse (bounded memory) | pending |

## Completed (Cycle 4 continuous)

| ID | Area | Task | Status |
|----|------|------|--------|
| C4+ | search | URL/path term coverage boost in BM25 re-rank | ✅ |
| C4+ | dedup | Unwrap Google AMP Cache hosts for fetch-gate identity | ✅ |
| C4+ | search | Ordered title-phrase boost in BM25 re-rank | ✅ |
| C4+ | dedup | Unwrap Bing/Google AMP viewers + SERP/CMS identity noise | ✅ |
| C4+ | search | Ordered URL-slug phrase boost in BM25 re-rank | ✅ |
| C4+ | dedup | Unwrap Wayback Machine + Google Translate for fetch-gate identity | ✅ |
| C4+ | search | Exact-title token equality boost in BM25 re-rank | ✅ |
| C4+ | dedup | Unwrap Facebook l.php + Google /url click redirects for fetch-gate identity | ✅ |
| C4+ | search | Lead-text (lede) ordered phrase boost in BM25 re-rank | ✅ |
| C4+ | dedup | Unwrap Outlook Safe Links + DuckDuckGo /l/ for fetch-gate identity | ✅ |
| C4+ | search | Lead-text bag-of-words term coverage boost in BM25 re-rank | ✅ |
| C4+ | dedup | Unwrap Instagram l.instagram.com + LinkedIn safety/redir for fetch-gate identity | ✅ |
| C4+ | search | Exact URL-slug token equality boost in BM25 re-rank | ✅ |
| C4+ | dedup | Unwrap Reddit out.reddit.com + YouTube /redirect for fetch-gate identity | ✅ |
| C4+ | search | Domain-label navigational boost in BM25 re-rank | ✅ |
| C4+ | dedup | Unwrap Slack slack-redir.net + WhatsApp l.wl.co for fetch-gate identity | ✅ |
| C4+ | search | Title-prefix token navigational boost in BM25 re-rank | ✅ |
| C4+ | dedup | Unwrap Telegram t.me/share|/iv + href.li for fetch-gate identity | ✅ |
| C4+ | search | URL-slug prefix token navigational boost in BM25 re-rank | ✅ |
| C4+ | dedup | Unwrap Tumblr t.umblr.com/redirect + Pocket getpocket.com/redirect for fetch-gate identity | ✅ |
| C4+ | search | Lead-text prefix token navigational boost in BM25 re-rank | ✅ |
| C4+ | dedup | Unwrap Pinterest pin-create/offsite + Flipboard share/bookmarklet for fetch-gate identity | ✅ |
| C4+ | search | Lead-text exact token equality boost in BM25 re-rank | ✅ |
| C4+ | dedup | Unwrap Buffer compose/add + Medium external-link for fetch-gate identity | ✅ |
| C4+ | obs/scrape | HTTP fetch latency hist + status counters; split connect/read timeouts | ✅ |
| C4+ | cli | `metrics --format table|json|prometheus` (TTY-aware default) | ✅ |
| C4+ | storage | fsync gzip JSONL chunks + parent dir on commit | ✅ |
| C4+ | spa | Dashboard HTTP fetch p95 + attempts KPIs | ✅ |
| C4+ | scrape | Charset-aware HTTP body decode (Content-Type / meta / detector) | ✅ |
| C4+ | cli/storage | `dlq list` / `dlq count` + StateDB.list_dlq | ✅ |
| C4+ | storage/obs | Iceberg append latency hist + row/batch counters | ✅ |
| C4+ | spa | Dashboard robots cache hit + Iceberg rows KPIs | ✅ |

## Progress snapshot

- **Branch:** `loop/continuous-dev` (~178 commits ahead of `main`)
- **Unit suite:** green (non-slow; datasets skip as configured)
- **Latest:** Diverse wave — charset decode (`218d61d`), DLQ list CLI (`9c80614`), Iceberg metrics (`d83ada0`), SPA robots/Iceberg KPIs (`7f86c1e`)

## Rules

- Fresh subagent per task (implementer → review).  
- Commit each completed task on `loop/continuous-dev`.  
- Do not push unless user asks.  
- Do not stop between tasks unless blocked or user stops.
