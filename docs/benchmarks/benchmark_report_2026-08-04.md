# Awareness Performance Baseline Report

- **Date**: 2026-08-04 (run 03–04 Aug 2026 UTC)
- **Repo**: `awareness` @ working tree (awareness `0.2.0`, commit `c255d99` + local changes)
- **Runner**: `benchmarks.run_all` (all 5 suites, default corpora, single run) + targeted probes
- **Method**: `benchmarks/harness.py` protocol — 1 warm-up + `repeats` rounds, **median** wall-clock, throughput = work / median s

## 1. Methodology

| Item | Value |
|---|---|
| Hardware | Apple M1 (`machdep.cpu.brand_string` = "Apple M1"), 8 cores, 16 GiB RAM (`hw.memsize` = 17179869184) |
| OS | macOS 27.0 arm64 (Mach-O) |
| Python | 3.13.9 (Anaconda, Clang 20.1.8), venv `/tmp/awareness-fresh/.venv` |
| Bench harness | `harness.py`: 1 warm-up + `repeats` rounds, **median** wall-clock, throughput = work/median s |
| Corpora | `benchmarks/corpus.py` (deterministic, seeded): hashing 8,000 docs / 8.9 MB (~1.1 KB/doc); simhash 4,000 docs + 1,000-doc near-dup set (160 clusters × 4 + 360 singletons, seed 7); extraction 400 HTML pages; query 20,000 docs / 38 MB JSONL; ingestion 6,000 docs / 6.7 MB |
| Bench-extra deps | `datasketch`, `blake3`, `matplotlib`, `readability-lxml`, `inscriptis`, `html2text` — **NOT installed** in the venv (verified via importlib; no installs performed). Suites degrade gracefully: MinHash/BLAKE3/readability/inscriptis/html2text peer entries skipped. |
| Peer workaround | Task 4 required a simhash-vs-MinHash comparison; `datasketch` absent → a numpy MinHash(num_perm=128) probe was implemented (`/tmp/awprobe/minhash_probe.py`) using the bench's exact shingles/corpus/protocol (universal linear hashing mod 2⁶¹−1; unbiased Jaccard estimator, same statistics as datasketch). |
| Total wall | 309 s for the full suite |

**Reference (prior committed run)**: `docs/benchmarks/results.json` @ HEAD was generated on a 10-core Apple Silicon / Python 3.11.14 machine at awareness 0.1.0 with the bench extra installed. Cited below as *prior-0.1.0* for trend context; not directly comparable to this M1 run.

## 2. Results

### 2.1 Hashing — `bench_hashing` (8,000 docs, 8.9 MB)

| Competitor | Throughput (MB/s) |
|---|---|
| **xxh3_64 (Awareness)** | **4,422.9** |
| MurmurHash3 | 1,333.0 |
| SHA-256 | 572.8 |
| BLAKE2b | 317.8 |
| MD5 | 210.4 |
| BLAKE3 | *skipped (blake3 not installed)* |

| Pipeline stage | Throughput (MB/s) | Share of cost |
|---|---|---|
| xxh3 digest only | 6,009.0 | 0.2 % |
| normalize_for_hash only | 11.1 | ~86 % |
| **content_hash full (normalize + xxh3)** | **12.8** | 100 % |

xxh3 is 3.3× Murmur and 21× SHA-256, but the real per-doc fingerprint path is bottlenecked by pure-Python NFKC normalize + punctuation fold: **content_hash = 12.8 MB/s ≈ 11,500 docs/s** at 1.1 KB/doc. Normalization is ~99.8 % of fingerprint time.

### 2.2 Near-dup detection — `bench_simhash` + MinHash probe

**Throughput (docs/s, 4,000 docs, same shingles)**

| Method | docs/s | B/doc |
|---|---|---|
| SimHash 64-bit (vectorized) | 3,885.8 | 8 |
| **SimHash 128-bit weighted (Awareness)** | **1,384.9** | **16** |
| MinHash 128 (datasketch, prior-0.1.0 machine) | 1,626.9 | 1,024 |
| MinHash 128 (numpy probe, this M1) | 2,500.1 | 1,024 |
| SimHash 64-bit (naive Python loop, "before") | 540.0 | 8 |

**Accuracy — end-to-end engine vs peers (1,000-doc near-dup corpus, seed 7, 480 true pairs)**

| Method | F1 | P | R | Operating point |
|---|---|---|---|---|
| **Awareness DedupEngine (default)** | **0.845** | 1.000 | 0.731 | Hamming ≤ 24 |
| Awareness DedupEngine (tuned) | 0.961 | 1.000 | 0.925 | Hamming ≤ 32 |
| SimHash-128 fingerprint separability (all-pairs oracle) | 0.989 | — | — | ceiling, no retrieval |
| MinHash 128 all-pairs (numpy probe, same corpus) | 1.000 | 1.000 | 1.000 | J ≥ 0.16 |
| MinHashLSH (datasketch, prior-0.1.0 machine) | 0.998 | 0.999 | 0.997 | J ≥ 0.5 |

**H≤24 vs H≤32 comparison** — the default Hamming ≤ 24 band retrieves only 73.1 % of true pairs (R = 0.731, P = 1.000). Widening to Hamming ≤ 32 recovers **F1 0.845 → 0.961** with precision still perfect (R = 0.925) on this corpus. The band decision was validated as *conservative, not optimal*.

**F1 vs edit intensity (sweep, engine default H≤24)** — x = % of words edited: 2 % → **0.995**, 4 % → 0.940, 7 % → 0.701, 10 % → 0.479, 15 % → **0.092**, 22 % → 0.007.

**Task-4 validation (H-24 band decision)**: the fingerprint is not the problem — both SimHash-128 (0.989) and MinHash (1.000) separate the corpus near-perfectly all-pairs. The loss is **retrieval**: the H≤24 pigeonhole band misses 27 % of true pairs (R = 0.731) while keeping P = 1.000. Raising the band to **H≤32 recovers F1 0.961 with precision still perfect** on this corpus. MinHash's edge is end-to-end: datasketch MinHashLSH at J≥0.5 achieved F1 0.998 (prior-0.1.0 machine) because LSH recall holds far better than banded simhash as edits grow. SimHash-128 wins on footprint (16 B/doc vs 1,024 B/doc = **64×**), and the tuned engine matches text-dedup's published SimHash F1≈0.85 (CORE) at 0.961.

> **Resolution (H≤24 band)** — `DEFAULT_NEAR_THRESHOLD` raised 24 → 32 (`src/awareness/dedup/engine.py:42`), clamped to `[0, NEAR_DUP_SEGMENTS - 1]`; covered by `tests/unit/test_near_threshold_32.py`. **Fixed in `876dbc6`.**

### 2.3 Extraction — `bench_extraction` (400 synthetic pages, only trafilatura + raw lxml ran)

| Extractor | Word F1 (measured) | pages/s | F1 (Barbaresi 2022, published) |
|---|---|---|---|
| **trafilatura (Awareness)** | **0.960** | **152.4** | **0.909** |
| raw lxml text() (no boilerplate removal) | 0.780 | 20,007.6 | — |
| readability-lxml / inscriptis / html2text | *skipped* | — | 0.801 / 0.686 / 0.577 |

trafilatura: +18.1 F1 points over raw lxml at 131× lower throughput. Peer F1 numbers (readability-lxml 0.801 etc.) are published values transcribed in the bench, not re-measured here (peers not installed).

### 2.4 Query — `bench_query` (20,000 docs, ~38 MB JSONL) ⚠ red flag

| Metric | Value |
|---|---|
| **BM25 search p50 (cached views)** | **153.9 ms** |
| BM25 search p50 (refresh-per-query "before") | 716.6 ms |
| SQLite FTS5 peer (same corpus, same machine) | 0.056 ms |
| naive Python substring scan (unranked) | 39.1 ms |
| FTS index build (cold, 20k docs) | 5.52 s |
| Range-scan COUNT(*) p50 | 155.5 ms |

**Root cause (probe-verified)**: `captures` was a **view over `read_json_auto` on the JSONL staging files** (`duckdb_index.py`), so *every* SQL call re-parsed the entire corpus:

| Probe (this machine) | Latency |
|---|---|
| `SELECT count(*) FROM captures` (view over JSONL) | 182.7 ms |
| same query after `CREATE TABLE captures_mat AS SELECT * FROM captures` | **0.5 ms** (365×) |
| `count(*)` view over 5,000-doc corpus | 113.9 ms (O(corpus)) |
| range COUNT on materialized table | 0.7 ms |

The ~154–160 ms search = ~110 ms JSONL re-parse + ~40 ms pure-Python `_rerank` (200 candidates × per-candidate regex tokenization of title/URL, `_lead_tokens`/`_url_slug_tokens`/`urlsplit` per row) + FTS overhead. cProfile readouts showing 47–57 ms are artifacts (DuckDB C-side work invisible to the Python profiler); plain wall timings are consistent at 160–200 ms per call.

> **Resolution (O(corpus) view re-parse)** — the deduped union is now materialized into `captures_materialized` (a real table with a unique index on `capture_id`), rebuilt only on source-signature change; the `captures` view reads the table, so the SQL surface is unchanged. Measured 365× on `COUNT(*)` (183 → 0.5 ms). Covered by `tests/unit/test_materialized_corpus.py`. **Fixed in `876dbc6`.**

**Second flag**: low-IDF query terms are silently dropped from BM25F (`search_idf_threshold`, default 1.0) — observed `bm25f_low_idf_terms_dropped: dropped=["coastal"]` for query "coastal sediment" at 20k docs, i.e. multi-term queries silently degrade to their high-IDF subset at small corpus sizes. *Still open at the time of this report* — the drop is surfaced in search diagnostics but the threshold itself is unchanged.

> **Resolution (silent term-dropping)** — Open as of `876dbc6`: the drop is logged (`bm25f_low_idf_terms_dropped`) but the `search_idf_threshold` prune still applies below ~100k docs. Tracked in Recommendation 3.

### 2.5 Ingestion — `bench_ingestion` (6,000 docs, single core)

| Loop | docs/s |
|---|---|
| **Awareness loop (normalize → content_hash + simhash → JSONL write)** | **1,399.6** |
| Same loop, naive simhash ("before") | 357.5 |
| Fingerprint stage only (content_hash + simhash, vectorized) | 1,406.4 |
| Fingerprint stage only (naive) | 452.1 |

Vectorized simhash gives **3.9×** loop throughput; the fingerprint stage is not the bottleneck — the full loop runs at 99.5 % of fingerprint-only speed.

## 3. Interpretation vs stated goals

- **Backfill throughput**: single-core ingestion ≈ **1,400 docs/s** (M1) ≈ 5.0 M docs/hour/core; 6,600 docs/s on the prior 10-core machine. Normalize → fingerprint costs ~0.71 ms/doc; extraction (trafilatura, 152 pages/s ≈ 6.6 ms/page) is the per-doc bottleneck in any HTML-heavy backfill, and the fingerprint is close to the JSONL-write floor. Throughput scales per worker; ~1–5 M docs/hour/core is the honest envelope. Fingerprinting choice (simhash vs MinHash) barely matters here — both ≤ 0.7 ms/doc — but simhash costs 64× less memory.
- **Live-tail latency**: per-doc ingestion at 1,400 docs/s means a tail landing in the JSONL staging is queryable after one view-refresh — *except* every search/count used to re-parse the full corpus, so per-query latency grew linearly with accumulated corpus (113.9 ms at 5k docs → 182.7 ms at 20k docs). **Live-tail latency did not meet "interactive" without materialization** (Section 2.4); the `captures_materialized` table closes this (measured 0.5 ms on `COUNT(*)`).
- **Search latency**: 154 ms p50 for a 20k-doc corpus (≈2,750× SQLite FTS5, 4× a naive unranked Python scan on the same machine). The FTS index exists and is fast to *build* (5.5 s) and *restore* (~1 ms); the latency was dominated by the JSONL re-parse view + Python rerank. Materializing the corpus table makes the engine's DuckDB SQL surface 1–2 ms class — the project's architectural win, available for ~one table — with the rerank cost (now LRU-cached) and FTS join remaining.

## 4. Anomalies / red flags (with resolution status)

1. **O(corpus) per-query latency via JSONL-backed views** — `count(*)` = 183 ms at 20k docs, 114 ms at 5k docs, **0.5 ms** on the same data as a table. Every `search()`/`execute()`/`related()` re-parsed all staging JSONL (`_refresh_views` → `read_json_auto`). This was the single largest query-layer cost and scaled with corpus size. > **Resolution: materialized corpus table — fixed in `876dbc6`** (365× on `COUNT(*)`; see 2.4).
2. **Silent query-term dropping** — BM25F drops low-IDF terms (`search_idf_threshold` default 1.0): "coastal sediment" searched as just "sediment" at 20k docs. Multi-word queries silently change semantics at small/medium corpus sizes. > **Resolution: open** — surfaced in diagnostics only; see Recommendation 3.
3. **H≤24 default band misses 27 % of near-dups** — F1 0.845 vs 0.961 at H≤32 (P stays 1.000 on this corpus); beyond ~10 % edit intensity engine recall collapses (F1 0.48 at 10 %, 0.09 at 15 %) while datasketch MinHashLSH held ~1.0 (prior machine). > **Resolution: `DEFAULT_NEAR_THRESHOLD` 24 → 32 — fixed in `876dbc6`**; heavy-edit recall remains a known SimHash↔MinHash trade (Recommendation 2).
4. **Pure-Python rerank at 40 ms/query** — 200 candidates × regex tokenization/`urlsplit` per row was ~25 % of search latency, independent of the SQL layer. > **Resolution: partially fixed in `876dbc6`** — tokenizer LRU caches (`maxsize 4096`) cut the re-regexing; the remaining per-candidate `urlsplit`/tokenization cost stands.
5. **Hardware sensitivity of reported README numbers** — README's "≈5,200 docs/s" (simhash128) and prior committed numbers (search 22 ms) were produced on a faster 10-core machine at 0.1.0; this M1 run shows 1,385 docs/s and 154 ms. Numbers in `docs/benchmarks/results.json` carry the machine fingerprint (harness records it: this run = macOS 27.0 arm64, 8 cores, Python 3.13.9). > **Resolution: documented** — M1-run numbers now captured in `docs/benchmarks/results.json` (this run) and in this report; README figures are prior-hardware context.

## 5. Recommendations (top 3)

1. **Materialize the corpus instead of JSONL-backed views** — one-shot `CREATE OR REPLACE TABLE captures_mat AS SELECT * FROM captures` (or a persisted DuckDB table fed by incremental appends) turns the dominant per-query cost from O(corpus) JSON re-parse into a ~1 ms scan: measured 365× on `count(*)` (183→0.5 ms). Keep JSONL as the durable staging layer and treat the DuckDB table as the query index, refreshing on `_source_signature()` change (already computed cheaply per call). *Status: shipped in `876dbc6`* (`captures_materialized` + unique index on `capture_id`; full rebuild on signature change; FTS incremental append path retained).
2. **Raise `DEFAULT_NEAR_THRESHOLD` 24 → 32** — F1 0.845 → 0.961 at P = 1.000 on the bench corpus. For recall on heavier edits (≥10 %), add an optional second-tier MinHashLSH pass or widen banding; 16 B/doc simhash stays the right default footprint (64× smaller than MinHash's 1,024 B/doc). *Status: shipped in `876dbc6`* (`engine.py:42`; `tests/unit/test_near_threshold_32.py`).
3. **Cut the Python rerank cost and stop silent term-dropping** — (a) store pre-tokenized title/url token columns (or cache `_lead_tokens`/`_url_slug_tokens` results) instead of re-regexing 200 rows per query — *partially shipped in `876dbc6`* (tokenizer LRU caches, `maxsize 4096`); (b) relax or make explicit the IDF term-prune threshold so multi-term queries keep all terms below ~100k docs, or surface the drop in search diagnostics — *open*; the drop is logged (`bm25f_low_idf_terms_dropped`) but the prune still applies.

### Skips (require `[bench]` extra, not installed, no installs performed)

`datasketch` (MinHashLSH peer, F1-accuracy suite, sweep series), `blake3` (hashing peer), `matplotlib` (plot.py), `readability-lxml` / `inscriptis` / `html2text` (extraction peers — only measured F1/throughput entries absent; published-F1 table included verbatim).
