# Awareness benchmarks

Same-machine, head-to-head micro-benchmarks comparing Awareness's own algorithms
against the de-facto peer libraries in each space (datasketch MinHashLSH, BLAKE3,
trafilatura, DuckDB FTS, SQLite FTS5), plus large-corpus performance and a
Postgres-parity audit. Everything is deterministic: the corpus is generated from a
fixed seed, so accuracy reproduces exactly and throughput drifts only with hardware.

## Reports

| Report | Date | Contents |
|---|---|---|
| [benchmark_report_2026-08-04.md](benchmark_report_2026-08-04.md) | 2026-08-04 | Baseline run on Apple M1 8-core: hashing (xxh3 4,422.9 MB/s), near-dup F1 (default 0.845 vs tuned 0.961 at H≤32), extraction (trafilatura F1 0.960), query (BM25 p50 153.9 ms → 0.5 ms materialized), ingestion (1,399.6 docs/s), recommendations + resolution status per finding |
| [perf_100k_2026-08-04.md](perf_100k_2026-08-04.md) | 2026-08-04 | 100k-doc probe (health 2.317 s cold, search warm 0.558 s, all analytics <2 s, export 1.496 s), Postgres parity audit of `state.py` (all 15 dialect sites verified), divergence notes with fix status, bench-suite summary (search optimization 4.6×) |
| [perf_iter6_report.md](perf_iter6_report.md) | 2026-08-04 | W25 top-3 perf fixes (iteration 6): incremental materialize (20k refresh 123 ms → 0.6 ms, ~196×; full-rebuild fallback intact), 3-level dir-mtime signature guard (92 ms walk → 0.22 ms @100k), FTS coalescing (in-window search 13 ms / 0 rebuilds; one coalesced rebuild 195 ms, then warm 45 ms). Verbatim copy of the W25 team report (`/tmp/w25_report.md`), measured against `a711ba6` + savedsearch WIP |

Machine-independent results and per-suite entries live in
[results.json](results.json) (written by `benchmarks.run_all`); charts live in this
directory (`summary.png`, `hashing.png`, `dedup.png`, `extraction.png`, `speedups.png`).

## Re-running

The suites need the optional `[bench]` extra (datasketch, blake3, matplotlib,
readability-lxml, inscriptis, html2text). Without it the suites still run, degrading
gracefully (peer entries are skipped and the accuracy suite drops the MinHashLSH
comparison).

```bash
uv pip install -e '.[bench]'                # benchmark peers + plotting

uv run --extra bench python -m benchmarks.run_all          # full run (writes results.json)
uv run --extra bench python -m benchmarks.run_all --fast   # smaller corpora, quick smoke
uv run --extra bench python -m benchmarks.run_all --only simhash,hashing
uv run --extra bench python -m benchmarks.plot             # renders the charts
```

- Module entry point: `benchmarks.run_all` (see `benchmarks/run_all.py`); flags are
  `--fast` and `--only <a,b,c>` (hashing, simhash, extraction, query, ingestion).
- `--fast` shrinks the query corpus to 5,000 docs; a subset `--only` run overwrites
  `results.json` with just the suites it ran — the committed `results.json` must come
  from a full `python -m benchmarks.run_all`.
- Methodology: `benchmarks/harness.py` — one warm-up, then the **median** of N rounds
  (min would flatter, mean is GC-skewed); throughput = work / median s.

See [benchmarks/README.md](../../benchmarks/README.md) for what each suite measures and
the honesty notes.
