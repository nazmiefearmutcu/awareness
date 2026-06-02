"""Run every benchmark suite and write ``docs/benchmarks/results.json``.

Usage:
    python -m benchmarks.run_all                # full run
    python -m benchmarks.run_all --fast         # smaller corpora, quick smoke
    python -m benchmarks.run_all --only simhash,hashing
"""

from __future__ import annotations

import argparse
import sys
import time

from . import bench_extraction, bench_hashing, bench_ingestion, bench_query, bench_simhash
from .harness import Suite, Sweep, write_results

SUITES = {
    "hashing": bench_hashing.run,
    "simhash": bench_simhash.run,
    "extraction": bench_extraction.run,
    "query": bench_query.run,
    "ingestion": bench_ingestion.run,
}

# Suites that also emit a line-chart sweep.
SWEEPS = {
    "simhash": bench_simhash.accuracy_sweep,
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Awareness benchmark runner")
    ap.add_argument("--only", default="", help="comma-separated subset of: " + ", ".join(SUITES))
    ap.add_argument("--fast", action="store_true", help="smaller corpora for a quick smoke run")
    args = ap.parse_args(argv)

    selected = [s.strip() for s in args.only.split(",") if s.strip()] or list(SUITES)
    all_suites: list[Suite] = []
    all_sweeps: list[Sweep] = []
    t_start = time.perf_counter()
    for name in selected:
        if name not in SUITES:
            print(f"[skip] unknown suite '{name}'", file=sys.stderr)
            continue
        print(f"\n▶ running suite: {name}")
        t0 = time.perf_counter()
        kwargs = {}
        if name == "query" and args.fast:
            kwargs = {"n_docs": 5_000}
        suites = SUITES[name](**kwargs) if kwargs else SUITES[name]()
        for s in suites:
            all_suites.append(s)
            rev = s.higher_is_better
            best = (max if rev else min)(s.entries, key=lambda e: e.value) if s.entries else None
            print(f"  · {s.title}: {len(s.entries)} entries"
                  + (f", leader = {best.name} ({best.value:.3f} {best.unit})" if best else ""))
        if name in SWEEPS:
            sweep = SWEEPS[name]()
            if sweep is not None:
                all_sweeps.append(sweep)
                print(f"  · {sweep.title}: {len(sweep.series)} series over {len(sweep.x_values)} points")
        print(f"  ✓ {name} done in {time.perf_counter() - t0:.1f}s")

    path = write_results(all_suites, sweeps=all_sweeps,
                         extra_meta={"total_seconds": round(time.perf_counter() - t_start, 1),
                                     "suites_run": selected})
    print(f"\n✅ wrote {len(all_suites)} suites + {len(all_sweeps)} sweeps → {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
