from __future__ import annotations

from awareness.dedup.engine import DEFAULT_NEAR_THRESHOLD
from awareness.storage.state import NEAR_DUP_SEG_BITS, NEAR_DUP_SEGMENTS


def test_banding_covers_default_threshold() -> None:
    pigeonhole_guarantee = NEAR_DUP_SEGMENTS - 1
    assert pigeonhole_guarantee >= DEFAULT_NEAR_THRESHOLD, (
        f"banding guarantees Hamming <={pigeonhole_guarantee} but the default "
        f"merge threshold is {DEFAULT_NEAR_THRESHOLD} — near-dups in the gap "
        f"would be missed; raise NEAR_DUP_SEGMENTS."
    )


def test_band_width_is_8_bits() -> None:
    # H-24: 32x8 banding. Band selectivity is 1/256 per bucket, so the 1024-row
    # candidate cap covers ~256k docs before silent recall loss (the old 32x4
    # layout truncated past ~16k docs).
    assert NEAR_DUP_SEGMENTS == 32
    assert NEAR_DUP_SEG_BITS == 8
