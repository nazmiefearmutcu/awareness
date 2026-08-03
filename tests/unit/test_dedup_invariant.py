from __future__ import annotations

from awareness.dedup.engine import DEFAULT_NEAR_THRESHOLD, NEAR_CLAMP_MAX
from awareness.storage.state import NEAR_DUP_SEG_BITS, NEAR_DUP_SEGMENTS


def test_banding_covers_exact_and_probabilistic_default_range() -> None:
    # 32x8 banding: only 128 // 8 = 16 bands carry signature bits, so the
    # exact pigeonhole guarantee is Hamming <= 15. Distances 16..31 are
    # retrieved probabilistically (per-band miss ~ (15/16)^d), and the W7
    # benchmark measured the H<=32 default at F1 0.961 / P 1.0 — band sharing
    # at 8-bit width still surfaces distance-32 pairs, with the per-band
    # candidate limit binding before the banding width. The default therefore
    # sits one bit past the probabilistic range; the clamp bounds the damage
    # of a misconfigured caller threshold.
    assert DEFAULT_NEAR_THRESHOLD == 32
    exact_pigeonhole_guarantee = 128 // NEAR_DUP_SEG_BITS - 1
    assert exact_pigeonhole_guarantee < DEFAULT_NEAR_THRESHOLD  # by design (W7)
    assert NEAR_CLAMP_MAX >= DEFAULT_NEAR_THRESHOLD
    assert 0 <= DEFAULT_NEAR_THRESHOLD <= NEAR_CLAMP_MAX


def test_band_width_is_8_bits() -> None:
    # H-24: 32x8 banding. Band selectivity is 1/256 per bucket, so the 1024-row
    # candidate cap covers ~256k docs before silent recall loss (the old 32x4
    # layout truncated past ~16k docs).
    assert NEAR_DUP_SEGMENTS == 32
    assert NEAR_DUP_SEG_BITS == 8
