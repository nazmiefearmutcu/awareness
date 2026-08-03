from __future__ import annotations

from awareness.storage.state import StateDB
from awareness.util.hashing import hamming128


def test_banding_retrieves_pair_within_threshold(tmp_path) -> None:
    state = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    state.init()

    # A: every byte = 0xF0. B: flip bit 0 of each of bytes 0 and 1 (Hamming 16).
    # → bands 0 and 1 differ (0xF0 vs 0x0F); bands 2..31 are identical
    #   (0xF0), so a 32x8 index RETRIEVES this pair via any shared band.
    # (The old 32x4 layout test pair 0xF0/0xF1 would NOT be retrieved at 8-bit
    # band width — updated for H-24's 32x8 bands.)
    a = sum(0xF0 << (8 * i) for i in range(16))
    b = a ^ ((0xFF << (8 * 0)) | (0xFF << (8 * 1)))
    assert hamming128(a, b) == 16

    state.add_near_dup_index("docA", a)
    candidates = dict(state.find_near_dup_candidates(b))
    assert "docA" in candidates, (
        "pigeonhole banding must retrieve a Hamming-16 pair (32 bands guarantee <=31)"
    )
    assert candidates["docA"] == a


def test_banding_uses_8bit_band_width(tmp_path) -> None:
    """8-bit bands: a doc is found only via an EXACT 8-bit band match."""
    from awareness.storage.state import NEAR_DUP_SEG_BITS, NEAR_DUP_SEGMENTS

    assert NEAR_DUP_SEGMENTS == 32
    assert NEAR_DUP_SEG_BITS == 8

    state = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    state.init()
    a = sum((0xAB + i) << (8 * i) for i in range(16))
    state.add_near_dup_index("docA", a)
    # Same signature → all 32 bands match.
    assert "docA" in dict(state.find_near_dup_candidates(a))
    # Flip a bit inside band 5 only: bands 0..4,6..31 still match exactly.
    b = a ^ (1 << (8 * 5))
    assert "docA" in dict(state.find_near_dup_candidates(b))
