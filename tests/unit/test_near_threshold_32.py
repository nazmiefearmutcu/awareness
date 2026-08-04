"""DEFAULT_NEAR_THRESHOLD = 32: clamp bounds + Hamming-32 merge behavior.

The W7 benchmark measured the H<=32 default at F1 0.961 / P 1.0 with the 32x8
band layout. These tests pin:
1. the default value and the [0, NEAR_CLAMP_MAX] clamp (which must allow 32),
2. that the 16 real 8-bit data bands still RETRIEVE a Hamming-32 pair
   (distance-32 pairs differ in at most 4 bands, leaving shared bands),
3. that the engine merges a Hamming-32 pair at the default threshold,
4. that tight_store_threshold is unaffected by the raised default.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from awareness.dedup.engine import (
    DEFAULT_NEAR_THRESHOLD,
    NEAR_CLAMP_MAX,
    DedupDecision,
    DedupEngine,
)
from awareness.schemas.doc import DocCapture, RobotsDecision, SourceKind, SourceRef
from awareness.storage.state import StateDB
from awareness.util.hashing import content_hash, doc_id_for, hamming128


def _make_cap(url: str, text: str, *, observed_str: str = "2024-01-01T00:00:00+00:00") -> DocCapture:
    ch = content_hash(text)
    did = doc_id_for(url, ch)
    return DocCapture(
        doc_id=did,
        capture_id=f"cap-{did[:8]}-{observed_str}",
        source=SourceRef(source_type=SourceKind.LOCAL_FIXTURE, source_name="fixture", source_locator="local"),
        discovery_channel="test",
        ingest_version="0.0",
        url=url,
        canonical_url=url,
        domain="x.test",
        fetch_ts=datetime(2024, 1, 1, tzinfo=UTC),
        observed_ts=datetime.fromisoformat(observed_str),
        text=text,
        content_hash=ch,
        near_dup_hash=0,
        robots_decision=RobotsDecision.NOT_APPLICABLE,
    )


def _state(tmp_path: Path) -> StateDB:
    db = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    db.init()
    return db


def test_default_threshold_is_32(tmp_path: Path) -> None:
    assert DEFAULT_NEAR_THRESHOLD == 32
    eng = DedupEngine(_state(tmp_path))
    assert eng.near_threshold == 32
    # tight-store cutoff still collapses to TIGHT_NEAR_STORE_THRESHOLD (12).
    assert eng.tight_store_threshold == 12


def test_clamp_allows_default_and_bounds_extremes(tmp_path: Path) -> None:
    db = _state(tmp_path)
    assert DedupEngine(db, near_threshold=DEFAULT_NEAR_THRESHOLD).near_threshold == 32
    assert DedupEngine(db, near_threshold=-7).near_threshold == 0
    assert DedupEngine(db, near_threshold=500).near_threshold == NEAR_CLAMP_MAX
    assert DedupEngine(db, near_threshold=NEAR_CLAMP_MAX).near_threshold == NEAR_CLAMP_MAX
    assert NEAR_CLAMP_MAX >= DEFAULT_NEAR_THRESHOLD


def test_banding_retrieves_hamming_32_pair(tmp_path: Path) -> None:
    """A distance-32 pair differs in at most 4 of the 16 data bands, so the
    remaining bands retrieve it (the candidate limit, not the band width,
    is the binding constraint at the default)."""
    state = _state(tmp_path)
    sig_a = (1 << 128) - 1  # all bits set
    sig_b = sig_a ^ ((1 << 32) - 1)  # flip the low 32 bits -> Hamming 32
    assert hamming128(sig_a, sig_b) == 32
    state.add_near_dup_index("docA", sig_a)
    candidates = dict(state.find_near_dup_candidates(sig_b))
    assert "docA" in candidates
    assert candidates["docA"] == sig_a


def test_hamming_32_pair_merged_at_default(tmp_path: Path, monkeypatch) -> None:
    """evaluate() must merge a Hamming-32 near-dup with the default threshold."""
    sig_a = (1 << 128) - 1
    sig_b = sig_a ^ ((1 << 32) - 1)
    assert hamming128(sig_a, sig_b) == 32

    # W19 content-diversity guard: a short-doc merge requires exact token-set
    # agreement, so the two docs must share their token set (same words,
    # reordered) while the monkeypatched simhash still yields Hamming 32.
    calls = {"n": 0}

    def fake_simhash(text: str) -> int:
        calls["n"] += 1
        return sig_a if calls["n"] == 1 else sig_b

    monkeypatch.setattr("awareness.dedup.engine.simhash128", fake_simhash)
    db = _state(tmp_path)
    eng = DedupEngine(db)
    a = _make_cap("https://a.test/1", "alpha body")
    out_a = eng.evaluate(a)
    assert out_a.decision == DedupDecision.NEW

    b = _make_cap("https://b.test/2", "body alpha", observed_str="2024-01-02T00:00:00+00:00")
    out_b = eng.evaluate(b)
    assert out_b.decision == DedupDecision.NEAR_DUP
    assert out_b.hamming == 32
    assert b.parent_doc_or_dup_group == out_a.dup_group


def test_hamming_33_pair_not_merged_at_default(tmp_path: Path, monkeypatch) -> None:
    """The default must still reject pairs past the threshold."""
    sig_a = (1 << 128) - 1
    sig_b = sig_a ^ ((1 << 33) - 1)
    assert hamming128(sig_a, sig_b) == 33
    assert hamming128(sig_a, sig_b) > DEFAULT_NEAR_THRESHOLD

    monkeypatch.setattr(
        "awareness.dedup.engine.simhash128",
        lambda text: sig_a if "alpha" in text else sig_b,
    )
    db = _state(tmp_path)
    eng = DedupEngine(db)
    a = _make_cap("https://a.test/1", "alpha body")
    eng.evaluate(a)
    b = _make_cap("https://b.test/2", "beta body", observed_str="2024-01-02T00:00:00+00:00")
    out_b = eng.evaluate(b)
    assert out_b.decision == DedupDecision.NEW, "Hamming 33 > 32 must not merge"
