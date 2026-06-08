# Awareness Cycle 2 — Plan 2: FPR-Calibrated Threshold + IDF Weighting (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make the near-dup threshold *principled* (derived from a target false-positive rate via the exact Hamming-distance distribution, not hand-picked) and give the 128-bit SimHash an *IDF-weighting hook* so shingle influence can be scaled by corpus rarity, not only local term frequency.

**The math:** For independent 128-bit signatures, the Hamming distance between two unrelated docs is `Binomial(128, 0.5)` (each bit independently agrees/differs with p=0.5). The false-positive rate of a Hamming≤t merge rule is therefore `FPR(t) = P(Binomial(128,0.5) ≤ t) = (Σ_{i=0..t} C(128,i)) / 2^128` (exact rational). The calibrated threshold for a target FPR is the largest `t` with `FPR(t) ≤ target`. (At t=24 the FPR is astronomically small — precision ~1.0 but recall is sacrificed; a 1e-6 target lands around t≈37.) Charikar weighting: a shingle's contribution scales by `tf_weight × idf(shingle)`; IDF down-weights boilerplate that appears in many documents.

**Tech Stack:** Python 3.13 (`math.comb` for exact binomial), NumPy, pytest. No new deps (no scipy).

**Standard test command:** `PYTHONPATH=src .venv/bin/python -m pytest -q -p no:cacheprovider`
**Baseline at plan start:** 229 passing after Cycle 2 Plan 1.

**Spec:** `docs/superpowers/specs/2026-06-08-awareness-cycle2-math-systems-design.md` (D3).
**Note:** Plan 2 ships the calibration *math* and the IDF *hook* (both backward-compatible — defaults unchanged). Actually RAISING the default threshold requires the banding to follow (`bands-1 ≥ threshold`, enforced by Plan-1 Task 2's invariant test) AND a benchmark re-measure (Plan 5); that combined change is a later plan, not this one.

---

### Task 1: FPR calibration module (exact binomial)

**Files:**
- Create: `src/awareness/dedup/calibration.py`
- Test: `tests/unit/test_dedup_calibration.py`

- [ ] **Step 1: Write the failing test** — create `tests/unit/test_dedup_calibration.py`:

```python
from __future__ import annotations

from awareness.dedup.calibration import calibrate_threshold, fpr_at_threshold


def test_fpr_endpoints() -> None:
    # Whole space ≤ 128 bits → FPR 1.0; ≤ -1 → 0.0.
    assert fpr_at_threshold(128, 128) == 1.0
    assert fpr_at_threshold(128, -1) == 0.0
    # Just below the mean (64) the FPR is well under 0.5.
    assert 0.0 < fpr_at_threshold(128, 50) < 0.5


def test_fpr_is_monotonic() -> None:
    prev = -1.0
    for t in range(0, 65, 8):
        cur = fpr_at_threshold(128, t)
        assert cur >= prev
        prev = cur


def test_calibrate_threshold_respects_target() -> None:
    target = 1e-6
    t = calibrate_threshold(128, target)
    # The calibrated threshold's FPR is within target, and one bit higher exceeds it.
    assert fpr_at_threshold(128, t) <= target
    assert fpr_at_threshold(128, t + 1) > target
    # Sanity: a 1e-6 target on 128 bits lands well below the mean (64) and
    # comfortably above the very-conservative default of 24.
    assert 24 < t < 64


def test_tighter_target_gives_lower_threshold() -> None:
    assert calibrate_threshold(128, 1e-9) <= calibrate_threshold(128, 1e-3)
```

- [ ] **Step 2: Run, confirm FAIL** (`ModuleNotFoundError: awareness.dedup.calibration`):
`PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_dedup_calibration.py -q`

- [ ] **Step 3: Implement** — create `src/awareness/dedup/calibration.py`:

```python
"""Data-driven near-duplicate threshold calibration.

The Hamming distance between two *unrelated* b-bit SimHash signatures is
distributed Binomial(b, 0.5) (each bit independently matches with p=0.5). So the
false-positive rate of a "merge if Hamming ≤ t" rule is the exact CDF

    FPR(t) = P(Binomial(b, 0.5) ≤ t) = (Σ_{i=0..t} C(b, i)) / 2^b

computed here with exact integer arithmetic (no floating-point summation error).
`calibrate_threshold` returns the largest t whose FPR stays within a target —
replacing a hand-picked threshold with a principled, FPR-controlled cutoff.
"""

from __future__ import annotations

from math import comb


def fpr_at_threshold(bits: int, threshold: int) -> float:
    """Exact false-positive rate of a Hamming≤``threshold`` merge rule over
    ``bits``-bit signatures, assuming unrelated pairs are Binomial(bits, 0.5)."""
    if threshold < 0:
        return 0.0
    if threshold >= bits:
        return 1.0
    favorable = sum(comb(bits, i) for i in range(threshold + 1))
    return favorable / (1 << bits)


def calibrate_threshold(bits: int, target_fpr: float) -> int:
    """Largest Hamming threshold whose false-positive rate stays ≤ ``target_fpr``.

    Returns -1 if even threshold 0 exceeds the target (i.e. target < 2^-bits).
    """
    best = -1
    for t in range(bits + 1):
        if fpr_at_threshold(bits, t) <= target_fpr:
            best = t
        else:
            break
    return best
```

- [ ] **Step 4: Confirm PASS:** `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_dedup_calibration.py -q`
- [ ] **Step 5: Full-suite gate:** `PYTHONPATH=src .venv/bin/python -m pytest -q -m "not slow and not smoke"`
- [ ] **Step 6: Commit:**
```bash
git add src/awareness/dedup/calibration.py tests/unit/test_dedup_calibration.py
git commit -m "feat(dedup): exact-binomial FPR threshold calibration"
```

---

### Task 2: IDF-weighting hook in `simhash128`

**Why:** `simhash128` currently weights each shingle only by local term frequency (`1 + ln(1+count)`), so boilerplate that is common *across the corpus* still influences the signature. Add an optional `idf` callable so a shingle's weight can be scaled by corpus rarity. Backward-compatible: `idf=None` reproduces the current signature exactly.

**Files:**
- Modify: `src/awareness/util/hashing.py` (`simhash128`, ~lines 93-123)
- Test: `tests/unit/test_simhash_idf.py` (create)

- [ ] **Step 1: Read** `simhash128` (~lines 93-123) to confirm the current weighted-path structure: it builds `uniq = list(counts.keys())` and `weights = [1 + ln(1+count)]`, then multiplies `signed *= weights[:, None]`.

- [ ] **Step 2: Write the failing test** — create `tests/unit/test_simhash_idf.py`:

```python
from __future__ import annotations

from collections.abc import Callable

from awareness.util.hashing import simhash128


def test_idf_none_is_backward_compatible() -> None:
    text = "the quick brown fox jumps over the lazy dog the the the"
    assert simhash128(text) == simhash128(text, idf=None)


def test_idf_changes_the_signature() -> None:
    text = "the quick brown fox jumps over the lazy dog the the the"

    def downweight_the(shingle: str) -> float:
        return 0.01 if "the" in shingle.split() else 1.0

    assert simhash128(text, idf=downweight_the) != simhash128(text)


def test_idf_accepts_callable_type() -> None:
    idf: Callable[[str], float] = lambda s: 1.0  # noqa: E731
    # An all-ones idf must reproduce the default signature (weights unchanged).
    text = "alpha beta gamma delta epsilon"
    assert simhash128(text, idf=idf) == simhash128(text)
```

- [ ] **Step 3: Run, confirm FAIL** (`simhash128() got an unexpected keyword argument 'idf'`):
`PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_simhash_idf.py -q`

- [ ] **Step 4: Implement.** In `src/awareness/util/hashing.py`, add the import at the top (with the other stdlib imports):
```python
from collections.abc import Callable
```
Change the `simhash128` signature from:
```python
def simhash128(text: str, k: int = 3, *, weighted: bool = True) -> int:
```
to:
```python
def simhash128(
    text: str, k: int = 3, *, weighted: bool = True, idf: Callable[[str], float] | None = None
) -> int:
```
Update the docstring to mention `idf` (one line: "When ``idf`` is given, each shingle's weight is additionally scaled by ``idf(shingle)`` so corpus-common boilerplate can be down-weighted."). Then, in the weighted branch where `weights` is computed, fold in the idf factor. Replace:
```python
    if weighted:
        counts = Counter(grams)
        uniq = list(counts.keys())
        weights = np.fromiter(
            (1.0 + np.log1p(counts[g]) for g in uniq), dtype=np.float64, count=len(uniq)
        )
    else:
        uniq = grams
        weights = None
```
with:
```python
    if weighted:
        counts = Counter(grams)
        uniq = list(counts.keys())
        weights = np.fromiter(
            (
                (1.0 + np.log1p(counts[g])) * (idf(g) if idf is not None else 1.0)
                for g in uniq
            ),
            dtype=np.float64,
            count=len(uniq),
        )
    else:
        uniq = grams
        weights = None
```
(When `weighted=False` the `idf` argument is ignored — that's acceptable; the IDF hook is a refinement of the frequency-weighted path. Note this in the docstring.)

- [ ] **Step 5: Confirm PASS:** `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_simhash_idf.py -q`
- [ ] **Step 6: Full-suite gate:** `PYTHONPATH=src .venv/bin/python -m pytest -q -m "not slow and not smoke"` (existing `simhash128` callers pass `idf` nothing → unchanged behavior; `tests/unit/test_hashing.py` must still pass).
- [ ] **Step 7: Commit:**
```bash
git add src/awareness/util/hashing.py tests/unit/test_simhash_idf.py
git commit -m "feat(hashing): optional IDF weighting hook in simhash128 (backward-compatible)"
```

---

## Plan-level self-review checklist

- [ ] Full suite green after both tasks.
- [ ] `fpr_at_threshold(128, 24)` is tiny; `calibrate_threshold(128, 1e-6)` is in (24, 64).
- [ ] `simhash128(text)` unchanged when `idf=None` (backward compat).
- [ ] `ruff check` introduces no NEW errors in touched files.

## Follow-ups

- Wiring a real corpus-IDF table (shingle→document-frequency store) to feed the `idf` hook — its own plan (a systems change; needs a persisted DF count).
- Raising `DEFAULT_NEAR_THRESHOLD` toward the calibrated value, with banding raised to keep the pigeonhole invariant AND a benchmark re-measure (Cycle-2 Plan 5).
