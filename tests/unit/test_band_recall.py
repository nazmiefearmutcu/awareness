"""H-24: band-candidate truncation must not silently lose recall at scale.

With the old 32x4-bit bands, bucket selectivity was 1/16, so the 1024-row
candidate cap was exhausted past ~16k docs (empirically 61%/82%/96% miss at
50k/100k/500k). With 32x8-bit bands, selectivity is 1/256 and a 20k-doc
corpus keeps every band bucket far below the cap — a Hamming-16 pair must
still be retrieved.
"""

from __future__ import annotations

import random
from pathlib import Path

from sqlalchemy import text

from awareness.storage.state import (
    NEAR_DUP_CANDIDATE_LIMIT,
    NEAR_DUP_SEG_BITS,
    NEAR_DUP_SEGMENTS,
    StateDB,
)
from awareness.util.hashing import hamming128, sig128_to_hex

# Only bands that carry signature bits are indexed (128-bit sig / 8-bit bands).
_DATA_BANDS = min(NEAR_DUP_SEGMENTS, 128 // NEAR_DUP_SEG_BITS)


def test_hamming_16_pair_retrieved_at_20k_docs(tmp_path: Path) -> None:
    state = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    state.init()

    # Query signature b, and pair partner a at Hamming 16 (flip 16 bits inside
    # bands 14 and 15 only — bands 0..13 are shared exactly with b).
    rng = random.Random(0xBADC0DE)
    b = rng.getrandbits(128)
    a = b ^ (0x00FF << (8 * 15)) ^ (0x00FF << (8 * 14))
    assert hamming128(a, b) == 16

    state.add_near_dup_index("doc-pair-A", a)

    # 20k filler docs with uniform random signatures: each (band, value) bucket
    # averages 20k / 2**8 ≈ 78 rows (8-bit width) — well under the 1024 cap.
    # The same corpus on 4-bit bands would average 20k / 16 ≈ 1250 per bucket
    # and truncate doc-pair-A out of every shared band.
    filler_docs = 20_000
    rows: list[dict] = []
    for i in range(filler_docs):
        sig = rng.getrandbits(128)
        hex_sig = sig128_to_hex(sig)
        legacy = sig & 0xFFFFFFFFFFFFFFFF
        if legacy >= (1 << 63):
            legacy -= 1 << 64
        for seg in range(_DATA_BANDS):
            rows.append(
                {
                    "doc_id": f"filler-{i:06d}",
                    "sig_hex": hex_sig,
                    "near_dup_hash": legacy,
                    "seg": seg,
                    "seg_value": (sig >> (NEAR_DUP_SEG_BITS * seg)) & ((1 << NEAR_DUP_SEG_BITS) - 1),
                }
            )
    with state.session() as s:
        # Executemany in chunks to stay inside SQLite's parameter limit.
        for start in range(0, len(rows), 50_000):
            s.execute(
                text(
                    "INSERT INTO dedup_near (doc_id, sig_hex, near_dup_hash, seg, seg_value) "
                    "VALUES (:doc_id, :sig_hex, :near_dup_hash, :seg, :seg_value)"
                ),
                rows[start : start + 50_000],
            )
        s.commit()

    candidates = dict(state.find_near_dup_candidates(b))
    assert "doc-pair-A" in candidates, (
        "Hamming-16 pair lost at 20k docs: an 8-bit band bucket must stay "
        f"under the {NEAR_DUP_CANDIDATE_LIMIT}-row candidate cap"
    )
    assert candidates["doc-pair-A"] == a

    # Sanity: the filler corpus actually exercises the cap math (doc-pair-A
    # shares 31 bands with b, each bucket holding ~20k/2**8 fillers).
    with state.session() as s:
        max_bucket = int(
            s.execute(
                text(
                    "SELECT MAX(n) FROM (SELECT seg, seg_value, COUNT(*) n FROM dedup_near "
                    "GROUP BY seg, seg_value)"
                )
            ).scalar()
            or 0
        )
    assert max_bucket < NEAR_DUP_CANDIDATE_LIMIT
    assert max_bucket > NEAR_DUP_CANDIDATE_LIMIT // 32  # corpus is not trivial
