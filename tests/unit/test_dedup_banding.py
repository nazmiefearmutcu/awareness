from __future__ import annotations

from awareness.storage.state import StateDB
from awareness.util.hashing import hamming128


def test_banding_retrieves_pair_within_threshold(tmp_path) -> None:
    state = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    state.init()

    # A: every byte = 0xF0. B: flip bit 0 of each of the 16 bytes (Hamming 16).
    # → every 8-bit band differs (0xF0 vs 0xF1): a 16x8 index would MISS this pair.
    # → the high nibble (0xF) of each byte is identical: a 32x4 index RETRIEVES it.
    a = sum(0xF0 << (8 * i) for i in range(16))
    b = a ^ sum(1 << (8 * i) for i in range(16))
    assert hamming128(a, b) == 16

    state.add_near_dup_index("docA", a)
    candidates = dict(state.find_near_dup_candidates(b))
    assert "docA" in candidates, (
        "pigeonhole banding must retrieve a Hamming-16 pair (32 bands guarantee <=31)"
    )
    assert candidates["docA"] == a
