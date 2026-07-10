# Awareness Cycle 2 — Plan 1: Pigeonhole-Correct Near-Dup Banding (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make the SimHash band index satisfy the Manku/Jain pigeonhole guarantee for the engine's default merge threshold, so near-duplicates within the threshold are *guaranteed* to be retrieved (not merely found probabilistically) — and lock the bands↔threshold relationship so it can't silently regress.

**The math:** Splitting a 128-bit signature into `B` disjoint bands, any pair within Hamming distance `≤ B-1` must share ≥1 identical band (pigeonhole: `B-1` differing bits can mark at most `B-1` of `B` bands, leaving ≥1 band difference-free). Today `B=16` (16×8-bit) → guarantee ≤15, but the default merge threshold is **24**, so 16-24-bit near-dups are retrieved only by luck. Set `B=32` (32×4-bit) → guarantee **≤31 > 24**. The 128-bit fingerprint is unchanged; only the band split changes. Cost: 32 tiny index rows/doc (was 16).

**Tech Stack:** Python 3.13, NumPy, SQLAlchemy, pytest. The banding loop is already parameterized by `NEAR_DUP_SEGMENTS`/`NEAR_DUP_SEG_BITS`, so changing the two constants re-bands everything.

**Standard test command:** `PYTHONPATH=src .venv/bin/python -m pytest -q -p no:cacheprovider`
**Baseline at plan start:** 227 passing after Cycle 1 (Plans 1-4).

**Spec:** `docs/superpowers/specs/2026-06-08-awareness-cycle2-math-systems-design.md` (D1, D2).
**Honesty note (D2):** the README near-dup F1/recall figures predate this banding change; this plan adds a note that they are pending re-measurement (Cycle-2 Plan 5), and does NOT hand-edit the numbers.

---

### Task 1: Re-band to 32×4 (pigeonhole-correct at the default threshold)

**Files:**
- Modify: `src/awareness/storage/state.py` (`NEAR_DUP_SEGMENTS`/`NEAR_DUP_SEG_BITS` ~lines 135-141 region; the `DedupNearRow` docstring)
- Modify: `README.md` (append a note to the near-dup benchmark section)
- Test: `tests/unit/test_dedup_banding.py` (create)

- [ ] **Step 1: Write the failing test.** This test constructs a pair at Hamming distance 16 where every 8-bit band differs (so the OLD 16×8 banding retrieves nothing) but a 4-bit half of each byte is identical (so 32×4 banding retrieves it). Create `tests/unit/test_dedup_banding.py`:

```python
from __future__ import annotations

from awareness.storage.state import StateDB
from awareness.util.hashing import hamming128


def test_banding_retrieves_pair_within_threshold(tmp_path) -> None:
    state = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    state.init()

    # A: every byte = 0xF0. B: flip bit 0 of each of the 16 bytes (Hamming 16).
    # → every 8-bit band differs (0xF0 vs 0xF1): a 16×8 index would MISS this pair.
    # → the high nibble (0xF) of each byte is identical: a 32×4 index RETRIEVES it.
    a = sum(0xF0 << (8 * i) for i in range(16))
    b = a ^ sum(1 << (8 * i) for i in range(16))
    assert hamming128(a, b) == 16  # within the default merge threshold (24)

    state.add_near_dup_index("docA", a)
    candidates = dict(state.find_near_dup_candidates(b))
    assert "docA" in candidates, (
        "pigeonhole banding must retrieve a Hamming-16 pair (32 bands guarantee ≤31)"
    )
    assert candidates["docA"] == a  # signature round-trips via sig_hex
```

- [ ] **Step 2: Run, confirm FAIL** (with 16×8 banding `docA` is not retrieved):
`PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_dedup_banding.py -q`

- [ ] **Step 3: Re-band.** In `src/awareness/storage/state.py`, change the two constants:
```python
NEAR_DUP_SEGMENTS = 16
NEAR_DUP_SEG_BITS = 8
```
to:
```python
# 128 bits split into 32 bands of 4 bits. The Manku/Jain pigeonhole guarantee is
# "a pair within Hamming ≤ (bands-1) shares ≥1 identical band", so 32 bands give
# an EXACT-retrieval guarantee up to Hamming ≤31 — comfortably covering the
# engine's default merge threshold (24), which 16×8 banding (guarantee ≤15) did
# not. Cost: 32 tiny index rows/doc. The 128-bit fingerprint is unchanged.
NEAR_DUP_SEGMENTS = 32
NEAR_DUP_SEG_BITS = 4
```
Also update the `DedupNearRow` class docstring where it says the bands "guarantee a shared band only up to Hamming < 16" — change that sentence to reflect the new guarantee (≤31). (`_NEAR_DUP_SEG_MASK`, the add/find loops, and `NEAR_DUP_CANDIDATE_LIMIT` are all derived from these constants and need no other change — verify by reading the surrounding code.)

- [ ] **Step 4: README honesty note.** In `README.md`, find the near-dup benchmark section and insert a blockquote note immediately BEFORE the `### Content fingerprinting` heading:
```markdown
> **Note (2026-06):** the near-duplicate band index was upgraded from 16×8-bit to
> **32×4-bit** so the Manku/Jain pigeonhole guarantee (≤ bands−1) covers the default
> Hamming≤24 merge threshold (exact retrieval, not probabilistic). The F1/recall
> figures above predate this change and are being re-measured.

```
(Use `grep -n "### Content fingerprinting" README.md` to locate the anchor; insert the note just above it.)

- [ ] **Step 5: Confirm PASS:** `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_dedup_banding.py -q`
- [ ] **Step 6: Full-suite gate:** `PYTHONPATH=src .venv/bin/python -m pytest -q -m "not slow and not smoke"`. The existing `tests/unit/test_dedup.py` exercises real near-dup decisions; it must still pass (32 bands only ADD retrieval — they never remove a true candidate). If a test asserted an exact `near_dup_index_rows` count tied to 16 bands, READ it and update the expected count to the 32-band value, noting under Deviations.
- [ ] **Step 7: Commit:**
```bash
git add src/awareness/storage/state.py README.md tests/unit/test_dedup_banding.py
git commit -m "feat(dedup): 32x4 pigeonhole-correct banding (exact retrieval at the merge threshold)"
```

---

### Task 2: Lock the bands↔threshold invariant so it can't silently regress

**Why:** The pigeonhole guarantee (`bands-1`) must stay ≥ the engine's default merge threshold; otherwise a future tweak silently reopens the recall gap. Make the default threshold a named constant and add a test asserting the invariant.

**Files:**
- Modify: `src/awareness/dedup/engine.py` (extract `DEFAULT_NEAR_THRESHOLD`; use it as the ctor default)
- Test: `tests/unit/test_dedup_invariant.py` (create)

- [ ] **Step 1: Write the failing test** — create `tests/unit/test_dedup_invariant.py`:

```python
from __future__ import annotations

from awareness.dedup.engine import DEFAULT_NEAR_THRESHOLD
from awareness.storage.state import NEAR_DUP_SEGMENTS


def test_banding_covers_default_threshold() -> None:
    # Pigeonhole guarantee is (bands - 1). It must cover the merge threshold so
    # every pair within the threshold is guaranteed to be retrieved.
    pigeonhole_guarantee = NEAR_DUP_SEGMENTS - 1
    assert pigeonhole_guarantee >= DEFAULT_NEAR_THRESHOLD, (
        f"banding guarantees Hamming ≤{pigeonhole_guarantee} but the default "
        f"merge threshold is {DEFAULT_NEAR_THRESHOLD} — near-dups in the gap "
        f"would be missed; raise NEAR_DUP_SEGMENTS."
    )
```

- [ ] **Step 2: Run, confirm FAIL** (`ImportError: cannot import name 'DEFAULT_NEAR_THRESHOLD'`):
`PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_dedup_invariant.py -q`

- [ ] **Step 3: Implement.** In `src/awareness/dedup/engine.py`, add a module-level constant near the top (after the imports / before `class DedupDecision`):
```python
# Default near-duplicate merge threshold in Hamming bits over the 128-bit
# signature. Must stay ≤ (NEAR_DUP_SEGMENTS - 1) so the band index's pigeonhole
# guarantee covers it — see tests/unit/test_dedup_invariant.py.
DEFAULT_NEAR_THRESHOLD = 24
```
and change the `DedupEngine.__init__` signature from `near_threshold: int = 24` to `near_threshold: int = DEFAULT_NEAR_THRESHOLD`.

- [ ] **Step 4: Confirm PASS:** `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_dedup_invariant.py -q`
- [ ] **Step 5: Full-suite gate.**
- [ ] **Step 6: Commit:**
```bash
git add src/awareness/dedup/engine.py tests/unit/test_dedup_invariant.py
git commit -m "feat(dedup): name DEFAULT_NEAR_THRESHOLD and assert the pigeonhole invariant"
```

---

## Plan-level self-review checklist

- [ ] Full suite green after both tasks.
- [ ] The Hamming-16 pair is retrieved (Task 1 test) and the invariant holds (Task 2 test).
- [ ] README carries the banding-change / re-measure note.
- [ ] `ruff check` introduces no NEW errors in touched files.

## Follow-ups (Cycle 2 later plans)

- Plan 2: FPR-calibrated threshold + corpus-IDF shingle weighting.
- Plan 5: re-measure the near-dup benchmark (the F1/recall figures the README note flags).
- Union-find canonical cluster resolution (transitive folding) — its own plan (needs a doc_id→canonical store).
