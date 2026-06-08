from __future__ import annotations

from awareness.dedup.engine import DEFAULT_NEAR_THRESHOLD
from awareness.storage.state import NEAR_DUP_SEGMENTS


def test_banding_covers_default_threshold() -> None:
    pigeonhole_guarantee = NEAR_DUP_SEGMENTS - 1
    assert pigeonhole_guarantee >= DEFAULT_NEAR_THRESHOLD, (
        f"banding guarantees Hamming <={pigeonhole_guarantee} but the default "
        f"merge threshold is {DEFAULT_NEAR_THRESHOLD} — near-dups in the gap "
        f"would be missed; raise NEAR_DUP_SEGMENTS."
    )
