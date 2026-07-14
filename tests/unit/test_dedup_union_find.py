"""Union-find parent resolution for near-dup clusters."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from awareness.dedup.engine import DedupDecision, DedupEngine
from awareness.schemas.doc import DocCapture, RobotsDecision, SourceKind, SourceRef
from awareness.storage.duckdb_index import DuckDbIndex
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


def test_related_near_dup_children_share_union_find_parent(tmp_path: Path) -> None:
    """Two NEAR_DUP children of one original both related() to each other.

    Regression: ``find_related_captures`` matches on exact
    ``parent_doc_or_dup_group`` equality. After union-find, every member stores
    the cluster root, so sibling lookup must work without walking the UF tree.
    Search collapse also keys on that same parent field.
    """
    db = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    db.init()
    eng = DedupEngine(db, near_threshold=24)

    base = " ".join(["the quick brown fox jumps over the lazy dog"] * 50)
    near_b = base + " extra trailing words to nudge the simhash a bit"
    near_c = base + " more different trailing phrase for second near dup"

    original = _make_cap("https://orig.test/doc", base, observed_str="2024-01-01T00:00:00+00:00")
    b = _make_cap("https://other.test/b", near_b, observed_str="2024-01-02T00:00:00+00:00")
    c = _make_cap("https://other.test/c", near_c, observed_str="2024-01-03T00:00:00+00:00")

    assert eng.evaluate(original).decision == DedupDecision.NEW
    assert eng.evaluate(b).decision == DedupDecision.NEAR_DUP
    assert eng.evaluate(c).decision == DedupDecision.NEAR_DUP
    root = original.doc_id
    assert b.parent_doc_or_dup_group == c.parent_doc_or_dup_group == root

    jsonl = tmp_path / "jsonl"
    day = jsonl / "captures" / "2024" / "01" / "01"
    day.mkdir(parents=True)
    full_keys = (
        "doc_id", "capture_id", "parent_doc_or_dup_group", "source_type",
        "source_name", "source_locator", "source_shard",
        "source_offset_or_record_id", "discovery_channel", "job_id", "batch_id",
        "ingest_version", "url", "canonical_url", "domain", "fetch_ts",
        "observed_ts", "published_ts", "last_modified", "content_type",
        "http_status", "etag", "title", "text", "language", "content_hash",
        "near_dup_hash", "robots_decision", "terms_note_if_relevant",
    )
    for i, cap in enumerate((original, b, c)):
        rec: dict[str, object] = {k: None for k in full_keys}
        rec.update(
            doc_id=cap.doc_id,
            capture_id=cap.capture_id,
            parent_doc_or_dup_group=cap.parent_doc_or_dup_group,
            source_type="local_fixture",
            domain=cap.domain,
            url=cap.url,
            fetch_ts=cap.fetch_ts.isoformat(),
            title=f"near-dup title {i}",
            text=cap.text,
            content_hash=cap.content_hash,
        )
        (day / f"chunk-{i}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")

    idx = DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=jsonl,
        iceberg_warehouse=None,
    )
    try:
        ids_from_b = {r["capture_id"] for r in idx.related(b.capture_id)}
        ids_from_c = {r["capture_id"] for r in idx.related(c.capture_id)}
        # Each NEAR_DUP child sees the other sibling + the original.
        assert c.capture_id in ids_from_b
        assert b.capture_id in ids_from_c
        assert original.capture_id in ids_from_b
        assert original.capture_id in ids_from_c
        assert b.capture_id not in ids_from_b  # self excluded
        assert c.capture_id not in ids_from_c

        # Search collapse: all three share union-find root → one unique hit.
        res = idx.search("quick brown fox", mode="substring")
        assert res["total"] == 1
        assert len(res["rows"]) == 1
        assert res["rows"][0]["parent_doc_or_dup_group"] == root
    finally:
        idx.close()
