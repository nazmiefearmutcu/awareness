# Awareness Cycle 2 — Plan 5a: Metrics Reservoir Sampling + Percentiles (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Fix the histogram's first-N sampling bias and report true p50/p95/p99 — so latency/size metrics describe the whole stream, not just its first 256 observations.

**The math:** `_Histogram.observe` keeps only the first `max_samples` (256) values (`if len(samples) < max_samples: append`), so after 256 observations every later value is dropped — any percentile computed from it reflects only the warm-up, not the distribution. Replace with **Vitter's Algorithm R reservoir sampling**: the i-th observation (1-indexed) is admitted to a size-`k` reservoir with probability `k/i`, giving a uniform random sample of the full stream at all times; percentiles from that reservoir are unbiased.

**Tech Stack:** Python 3.13 stdlib (`random`), pytest. No new deps.

**Standard test command:** `PYTHONPATH=src .venv/bin/python -m pytest -q -p no:cacheprovider`
**Baseline at plan start:** 236 passing after Cycle 2 Plan 2.

**Spec:** `docs/superpowers/specs/2026-06-08-awareness-cycle2-math-systems-design.md` (Plan 5, metrics part).

---

### Task 1: Reservoir-sample the histogram and add p50/p95/p99

**Files:**
- Modify: `src/awareness/obs/metrics.py` (`_Histogram.observe` and `as_dict`; add a `_percentile` helper)
- Test: `tests/unit/test_metrics_percentiles.py` (create)

- [ ] **Step 1: Write the failing test** — create `tests/unit/test_metrics_percentiles.py`:

```python
from __future__ import annotations

import random

from awareness.obs.metrics import _Histogram


def test_percentiles_on_full_sample() -> None:
    h = _Histogram(max_samples=1000)  # large enough to retain all
    for v in range(1, 101):  # 1..100
        h.observe(float(v))
    d = h.as_dict()
    assert d["count"] == 100
    assert d["min"] == 1.0
    assert d["max"] == 100.0
    assert 49.0 <= d["p50"] <= 52.0
    assert 94.0 <= d["p95"] <= 96.0
    assert 98.0 <= d["p99"] <= 100.0


def test_reservoir_is_unbiased_after_capacity() -> None:
    # 256 zeros then 10_000 hundreds. First-N sampling would keep only the
    # 256 zeros → p50 == 0 (biased). Reservoir sampling reflects the stream,
    # whose overwhelming majority is 100 → p50 == 100.
    random.seed(12345)
    h = _Histogram(max_samples=256)
    for _ in range(256):
        h.observe(0.0)
    for _ in range(10_000):
        h.observe(100.0)
    d = h.as_dict()
    assert d["count"] == 10_256
    assert d["p50"] == 100.0, "reservoir sampling must not be biased toward the first 256 values"


def test_empty_histogram_percentiles_are_zero() -> None:
    h = _Histogram()
    d = h.as_dict()
    assert d["p50"] == 0.0 and d["p95"] == 0.0 and d["p99"] == 0.0
```

- [ ] **Step 2: Run, confirm FAIL** (`KeyError: 'p50'` — no percentiles; and the bias test would fail under first-N sampling):
`PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_metrics_percentiles.py -q`

- [ ] **Step 3: Implement** in `src/awareness/obs/metrics.py`:

(a) Add `import random` near the top (with `threading`, `time`).

(b) Add a module-level percentile helper (after the imports, before `_Histogram`):
```python
def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Nearest-rank percentile of an already-sorted list (0.0 if empty)."""
    if not sorted_vals:
        return 0.0
    n = len(sorted_vals)
    idx = min(n - 1, max(0, int(round((pct / 100.0) * (n - 1)))))
    return sorted_vals[idx]
```

(c) Replace `_Histogram.observe` with Algorithm-R reservoir sampling:
```python
    def observe(self, v: float) -> None:
        self.count += 1
        self.sum += v
        self.min = min(self.min, v)
        self.max = max(self.max, v)
        if len(self.samples) < self.max_samples:
            self.samples.append(v)
        else:
            # Vitter Algorithm R: the count-th item replaces a uniformly chosen
            # reservoir slot with probability max_samples/count, keeping the
            # sample uniform over the whole stream.
            j = random.randint(0, self.count - 1)  # noqa: S311 (non-crypto sampling)
            if j < self.max_samples:
                self.samples[j] = v
```

(d) Add percentiles to `as_dict`:
```python
    def as_dict(self) -> dict[str, Any]:
        avg = self.sum / self.count if self.count else 0.0
        ordered = sorted(self.samples)
        return {
            "count": self.count,
            "sum": round(self.sum, 4),
            "min": round(self.min if self.count else 0.0, 4),
            "max": round(self.max, 4),
            "avg": round(avg, 4),
            "p50": round(_percentile(ordered, 50), 4),
            "p95": round(_percentile(ordered, 95), 4),
            "p99": round(_percentile(ordered, 99), 4),
        }
```

- [ ] **Step 4: Confirm PASS:** `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_metrics_percentiles.py -q`
- [ ] **Step 5: Full-suite gate:** `PYTHONPATH=src .venv/bin/python -m pytest -q -m "not slow and not smoke"`. The `snapshot()` histograms now carry p50/p95/p99 — if a test asserts the exact key set of a histogram dict, READ it and add the new keys, noting under Deviations.
- [ ] **Step 6: Commit:**
```bash
git add src/awareness/obs/metrics.py tests/unit/test_metrics_percentiles.py
git commit -m "feat(metrics): reservoir-sample histograms and report p50/p95/p99"
```

---

## Plan-level self-review checklist

- [ ] Full suite green.
- [ ] The bias test (`test_reservoir_is_unbiased_after_capacity`) passes — proving reservoir, not first-N.
- [ ] `ruff check src/awareness/obs/metrics.py` introduces no NEW errors (the `# noqa: S311` covers the non-crypto sampling).
