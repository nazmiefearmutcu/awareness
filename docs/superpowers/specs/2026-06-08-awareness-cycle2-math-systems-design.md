# Awareness — Cycle 2 Design: Mathematical & Systems Upgrades

**Status:** Approved scope (follows Cycle 1) · 2026-06-08
**Scope:** Roadmap Phases 4-5. Principled upgrades to dedup math, ranking, hashing, language
detection, benchmarks (math) and concurrency, memory, storage layout, observability (systems).
These are not bug fixes — they raise precision/recall and operability and make the benchmark
claims defensible. Builds on the Cycle-1 reliability/scraping/search foundation (Plans 1-4).
**Source:** the "out of scope → Cycle 2" list in
`2026-06-08-awareness-make-it-work-design.md`; audit `docs/superpowers/audit/2026-06-08-awareness-audit.json`.

## Plans (each its own spec→plan→implement)

- **Plan 1 — Dedup math:** pigeonhole-correct banding (recall-exact retrieval at the merge
  threshold); union-find canonical cluster resolution (transitive near-dup folding); named
  shingle-`k`. *(This plan — pure logic, testable without re-running benchmarks.)*
- **Plan 2 — Threshold calibration & IDF weighting:** FPR-controlled Hamming threshold from a
  measured unrelated-pair distribution; corpus-IDF shingle weighting (Charikar). *(Needs an
  empirical calibration harness; partly benchmark-driven.)*
- **Plan 3 — Ranking:** BM25F field-boost (title weight) + length-aware + recency prior.
- **Plan 4 — Language detection & quality:** confidence-aware LID (fastText/CLD3), length
  gating, trust FineWeb metadata; Gopher/C4-style content-quality filter for WET.
- **Plan 5 — Benchmarks honesty:** bootstrap CIs + real-text holdout; metrics reservoir
  sampling + true p50/p95/p99. *(Re-measures the numbers any banding/threshold change shifts.)*
- **Plan 6 — Systems:** persisted/incremental FTS; streaming WET parse (bounded memory);
  pooled httpx client + global fetch/extract concurrency; Iceberg re-partition
  `month(fetch_ts)+source_type` + compaction; crash-safe flush + idempotent appends;
  `/metrics` export + per-fetch tracing.

## Key decisions

- **D1 — Banding follows the threshold.** The Manku/Jain pigeonhole guarantee is "a pair within
  Hamming ≤ (bands − 1) shares ≥1 identical band." With 16 bands the guarantee is ≤15, but the
  default merge threshold is 24 — so 16-24-bit near-dups are retrieved only probabilistically.
  Set bands ≥ threshold+1. Default to **32 bands × 4 bits** (guarantee ≤31 > 24). Cost: 32
  index rows/doc (was 16). The fingerprint stays 128-bit; only the band split changes.
- **D2 — Honesty about benchmarks.** Any banding/threshold change shifts the measured F1/recall
  in the README and `benchmarks/`. Plan 1 changes the *math* and proves the *guarantee* with a
  unit test; it does NOT hand-edit the README's numbers. Re-measuring is Plan 5's job — until
  then the README carries a note that the banding changed and numbers are pending re-measurement.
- **D3 — Calibration is empirical, not guessed.** The threshold and IDF tables come from
  measured distributions (Plan 2), not hand-tuning — that is the whole point of the "data-driven"
  upgrade the user asked for.

## Out of scope (Cycle 3)

SPA Settings editable; fetch_ts/published_ts disentangle; URL canonicalization tightening;
JSONL+Iceberg double-count reconcile.
