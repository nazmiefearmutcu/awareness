"""H-23: EXACT_DUP/REVISION parent must be the union-find root.

X (NEW) is the cluster root. Y is a NEAR_DUP of X, folded under X via
uf_union. Z is an EXACT_DUP of Y (same bytes, different URL): its
``parent_doc_or_dup_group`` must be the union-find ROOT (X), not the raw
first-seen doc_id of its content (Y) — otherwise search collapse and
``related()`` would disagree with the near-dup cluster membership.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from awareness.dedup.engine import DedupDecision, DedupEngine
from awareness.schemas.doc import DocCapture, RobotsDecision, SourceKind, SourceRef
from awareness.storage.state import StateDB
from awareness.util.hashing import content_hash, doc_id_for, simhash64


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


def test_exact_dup_parent_is_uf_root(tmp_path: Path) -> None:
    db = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    db.init()
    eng = DedupEngine(db, near_threshold=24)

    # W19 content-diversity guard: short docs (<=200 unique tokens) only
    # merge on exact token-set agreement, so the near-dup base must be
    # vocabulary-rich (>200 unique tokens) for the long-doc path.
    base = " ".join(f"climate report token number {i} from the regional desk" for i in range(230))
    near = base + " extra trailing words to nudge the simhash a bit"

    x = _make_cap("https://orig.test/doc", base, observed_str="2024-01-01T00:00:00+00:00")
    out_x = eng.evaluate(x)
    assert out_x.decision == DedupDecision.NEW
    assert x.parent_doc_or_dup_group == x.doc_id

    y = _make_cap("https://other.test/y", near, observed_str="2024-01-02T00:00:00+00:00")
    out_y = eng.evaluate(y)
    assert out_y.decision == DedupDecision.NEAR_DUP
    assert y.parent_doc_or_dup_group == x.doc_id

    # Z: exact same content as Y under a different URL.
    z = _make_cap("https://mirror.test/z", near, observed_str="2024-01-03T00:00:00+00:00")
    out_z = eng.evaluate(z)
    assert out_z.decision == DedupDecision.EXACT_DUP

    # H-23: Z folds to the union-find root (X), not Y's raw doc_id.
    assert z.parent_doc_or_dup_group == x.doc_id
    assert z.parent_doc_or_dup_group != y.doc_id
    assert db.uf_find(y.doc_id) == x.doc_id


def test_revision_parent_is_uf_root(tmp_path: Path) -> None:
    """REVISION (same URL+content re-captured) also folds to the UF root."""
    db = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    db.init()
    eng = DedupEngine(db, near_threshold=24)

    # W19 content-diversity guard: short docs (<=200 unique tokens) only
    # merge on exact token-set agreement, so the near-dup base must be
    # vocabulary-rich (>200 unique tokens) for the long-doc path.
    base = " ".join(f"climate report token number {i} from the regional desk" for i in range(230))
    near = base + " extra trailing words to nudge the simhash a bit"

    x = _make_cap("https://orig.test/doc", base, observed_str="2024-01-01T00:00:00+00:00")
    eng.evaluate(x)
    y = _make_cap("https://other.test/y", near, observed_str="2024-01-02T00:00:00+00:00")
    eng.evaluate(y)

    # Re-fetch X's URL with X's own content again → REVISION of X's content.
    x2 = _make_cap("https://orig.test/doc", base, observed_str="2024-02-01T00:00:00+00:00")
    out = eng.evaluate(x2)
    assert out.decision == DedupDecision.REVISION
    assert x2.parent_doc_or_dup_group == x.doc_id
