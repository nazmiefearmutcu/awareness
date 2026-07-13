"""Union-find parent resolution for near-dup clusters."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from awareness.dedup.engine import DedupDecision, DedupEngine
from awareness.schemas.doc import DocCapture, RobotsDecision, SourceKind, SourceRef
from awareness.storage.state import StateDB
from awareness.util.hashing import content_hash, doc_id_for, simhash64


def test_uf_find_unknown_returns_self(tmp_path: Path) -> None:
    db = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    db.init()
    assert db.uf_find("missing-doc") == "missing-doc"


def test_uf_union_transitive(tmp_path: Path) -> None:
    """A-B and C-B share one root (A and C find equal)."""
    db = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    db.init()
    root_ab = db.uf_union("A", "B")
    root_cb = db.uf_union("C", "B")
    assert db.uf_find("A") == db.uf_find("C")
    assert db.uf_find("A") == root_ab
    assert db.uf_find("C") == root_cb
    assert db.uf_find("B") == db.uf_find("A")


def _make_cap(url: str, text: str, *, observed_str: str = "2024-01-01T00:00:00+00:00") -> DocCapture:
    ch = content_hash(text)
    cu = url
    sim = simhash64(text)
    did = doc_id_for(cu, ch)
    return DocCapture(
        doc_id=did,
        capture_id=f"cap-{did[:8]}-{observed_str}",
        source=SourceRef(
            source_type=SourceKind.LOCAL_FIXTURE, source_name="fixture", source_locator="local"
        ),
        discovery_channel="test",
        ingest_version="0.0",
        url=url,
        canonical_url=cu,
        domain="x.test",
        fetch_ts=datetime(2024, 1, 1, tzinfo=UTC),
        observed_ts=datetime.fromisoformat(observed_str),
        text=text,
        content_hash=ch,
        near_dup_hash=sim,
        robots_decision=RobotsDecision.NOT_APPLICABLE,
    )


def test_engine_two_near_dups_share_parent(tmp_path: Path) -> None:
    """Two near-dups of the same original resolve to one parent group."""
    db = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    db.init()
    eng = DedupEngine(db, near_threshold=24)

    base = " ".join(["the quick brown fox jumps over the lazy dog"] * 50)
    near_b = base + " extra trailing words to nudge the simhash a bit"
    near_c = base + " more different trailing phrase for second near dup"

    original = _make_cap("https://orig.test/doc", base, observed_str="2024-01-01T00:00:00+00:00")
    out0 = eng.evaluate(original)
    assert out0.decision == DedupDecision.NEW
    assert original.parent_doc_or_dup_group == original.doc_id

    b = _make_cap("https://other.test/b", near_b, observed_str="2024-01-02T00:00:00+00:00")
    out_b = eng.evaluate(b)
    assert out_b.decision == DedupDecision.NEAR_DUP
    assert b.parent_doc_or_dup_group == original.doc_id

    c = _make_cap("https://other.test/c", near_c, observed_str="2024-01-03T00:00:00+00:00")
    out_c = eng.evaluate(c)
    assert out_c.decision == DedupDecision.NEAR_DUP

    # Transitive: both near-dups share the same parent group (root).
    assert b.parent_doc_or_dup_group == c.parent_doc_or_dup_group
    assert b.parent_doc_or_dup_group == original.doc_id
    assert db.uf_find(b.doc_id) == db.uf_find(c.doc_id) == original.doc_id
