# Awareness benchmark suite

Same-machine, head-to-head micro-benchmarks comparing Awareness's own
algorithms against the de-facto peer libraries, plus the SOTA libraries
Awareness rides on. Everything is deterministic: the corpus is generated from a
fixed seed, so cloning the repo reproduces the same numbers (modulo hardware).

```bash
uv pip install -e '.[bench]'          # datasketch, blake3, matplotlib, extraction peers
python -m benchmarks.run_all          # → docs/benchmarks/results.json
python -m benchmarks.plot             # → docs/benchmarks/*.png
# subsets / quick smoke:
python -m benchmarks.run_all --only simhash,hashing
python -m benchmarks.run_all --fast
# individual suite, printed to stdout:
python -m benchmarks.bench_simhash
```

## What each suite measures

| Suite | Awareness code under test | Peers | Headline metric |
| --- | --- | --- | --- |
| `bench_hashing` | `xxh3_64` content fingerprint | BLAKE3, MurmurHash3, BLAKE2b, SHA-256, MD5 | MB/s |
| `bench_simhash` | 128-bit weighted `simhash128` + Hamming banding | `datasketch` MinHashLSH (num_perm=128) | docs/s, F1, B/doc |
| `bench_extraction` | `trafilatura` wrapper (`html_to_text`) | readability-lxml, inscriptis, html2text, raw lxml | word-F1, pages/s |
| `bench_query` | `DuckDbIndex` BM25 FTS + range scan | SQLite FTS5, naive scan | latency ms |
| `bench_ingestion` | normalize → fingerprint → JSONL write | self before/after | docs/s |

## Methodology & honesty notes

- **Corpus** (`corpus.py`) is synthetic but realistic: sentences are assembled
  from large word banks via templates, so unrelated documents share few tokens
  (mirroring real web text); near-duplicates apply a small, bounded fraction of
  word-level edits drawn from a realistic spread.
- **Timing** (`harness.py`) warms up once, then reports the **median** of N
  rounds (min would flatter, mean is GC-skewed).
- **Dedup F1 is end-to-end**: the real `DedupEngine` (banded retrieval +
  Hamming threshold + grouping) vs `datasketch` MinHashLSH (LSH query + union),
  each at its F1-optimal threshold — the same full-pipeline metric text-dedup
  and datasketch report. A separate "fingerprint separability" number (all-pairs
  oracle) shows the 128-bit signature itself is as separable as MinHash (~0.99),
  so the remaining gap is retrieval, not the fingerprint.
- **What Awareness wins:** content-hash throughput (~2.4–16×), near-dup
  throughput (~3.3×), near-dup memory (64×), extraction quality (F1), and
  near-dup **precision** (1.00 — it never false-merges).
- **What it trades, honestly:** MinHashLSH wins near-dup **recall/F1**
  (~0.998 vs ~0.84 at Awareness's shipped default, ~0.96 tuned) and degrades far
  more gracefully under heavy edits — the well-known SimHash↔MinHash trade;
  trafilatura is slower than crude tag-stripping (it earns the top F1); SQLite
  FTS5 answers unranked lookups much faster than DuckDB FTS (DuckDB unifies FTS +
  analytics over one lake). Dedup never drops rows, so lower recall = less
  folding, not lost data.
- **Where we were lower, we improved** rather than re-framing: 64-bit→128-bit
  weighted SimHash + 16×8-bit banding + a Hamming≤24 default (end-to-end recall
  ~2%→73%, up to 93% tuned at precision 1.0), NumPy-vectorized fingerprinting
  (~5.6×), cached DuckDB views (search ~8.5×).

> The committed `results.json` and charts must come from a full `python -m
> benchmarks.run_all` (no `--only`), since a subset run overwrites `results.json`
> with only the suites it ran.

Raw results: [`../docs/benchmarks/results.json`](../docs/benchmarks/results.json).
