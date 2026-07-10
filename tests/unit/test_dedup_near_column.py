from __future__ import annotations

from sqlalchemy import BigInteger

from awareness.storage.state import DedupNearRow


def test_near_dup_hash_is_bigint() -> None:
    col = DedupNearRow.__table__.c.near_dup_hash
    assert isinstance(col.type, BigInteger), (
        "near_dup_hash stores a 64-bit simhash; a 32-bit Integer overflows on Postgres"
    )
