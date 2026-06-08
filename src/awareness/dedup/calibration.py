"""Data-driven near-duplicate threshold calibration.

The Hamming distance between two *unrelated* b-bit SimHash signatures is
distributed Binomial(b, 0.5) (each bit independently matches with p=0.5). So the
false-positive rate of a "merge if Hamming <= t" rule is the exact CDF

    FPR(t) = P(Binomial(b, 0.5) <= t) = (sum_{i=0..t} C(b, i)) / 2^b

computed here with exact integer arithmetic (no floating-point summation error).
`calibrate_threshold` returns the largest t whose FPR stays within a target —
replacing a hand-picked threshold with a principled, FPR-controlled cutoff.
"""

from __future__ import annotations

from math import comb


def fpr_at_threshold(bits: int, threshold: int) -> float:
    """Exact false-positive rate of a Hamming<=``threshold`` merge rule over
    ``bits``-bit signatures, assuming unrelated pairs are Binomial(bits, 0.5)."""
    if threshold < 0:
        return 0.0
    if threshold >= bits:
        return 1.0
    favorable = sum(comb(bits, i) for i in range(threshold + 1))
    return favorable / (1 << bits)


def calibrate_threshold(bits: int, target_fpr: float) -> int:
    """Largest Hamming threshold whose false-positive rate stays <= ``target_fpr``.

    Returns -1 if even threshold 0 exceeds the target (i.e. target < 2^-bits).
    """
    best = -1
    for t in range(bits + 1):
        if fpr_at_threshold(bits, t) <= target_fpr:
            best = t
        else:
            break
    return best
