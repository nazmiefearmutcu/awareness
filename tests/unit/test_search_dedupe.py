"""Search result collapse + worker EXACT_DUP storage skip.

Regression for data-quality hard gates:
  * top-K search must not show the same parent_doc_or_dup_group twice
  * top-K search must not show the same content_hash twice (when parent unset)
  * EXACT_DUP captures must not be re-persisted to JSONL
  * tight NEAR_DUP (Hamming ≤12) must not be re-persisted; loose NEAR_DUP still is
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from awareness.dedup.engine import DedupDecision, DedupOutcome
from awareness.obs.metrics import get_metrics
from awareness.planner.planner import Planner
from awareness.schemas.doc import DocCapture, RobotsDecision, SourceKind, SourceRef
from awareness.storage.duckdb_index import (
    DuckDbIndex,
    _collapse_key,
    _collapse_search_rows,
)
from awareness.storage.state import StateDB
from awareness.util.hashing import content_hash, doc_id_for, simhash64
from awareness.workers.engine import WorkerEngine

_FULL_KEYS = (
    "doc_id", "capture_id", "parent_doc_or_dup_group", "source_type",
    "source_name", "source_locator", "source_shard",
    "source_offset_or_record_id", "discovery_channel", "job_id", "batch_id",
    "ingest_version", "url", "canonical_url", "domain", "fetch_ts",
    "observed_ts", "published_ts", "last_modified", "content_type",
    "http_status", "etag", "title", "text", "language", "content_hash",
    "near_dup_hash", "robots_decision", "terms_note_if_relevant",
)


def _write_doc(
    root: Path,
    idx: int,
    *,
    title: str,
    text: str,
    domain: str = "example.com",
    content_hash_val: str | None = None,
    url: str | None = None,
    parent_doc_or_dup_group: str | None = None,
) -> None:
    day = root / "captures" / "2026" / "06" / "01"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}",
        capture_id=f"cap-{idx}",
        parent_doc_or_dup_group=parent_doc_or_dup_group,
        source_type="rss",
        domain=domain,
        url=url or f"https://{domain}/{idx}",
        fetch_ts="2026-06-01T12:00:00+00:00",
        title=title,
        text=text,
        content_hash=content_hash_val,
    )
    (day / f"chunk-{idx}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def test_collapse_key_prefers_parent_then_content_hash() -> None:
    # parent_doc_or_dup_group wins even when content_hash differs
    assert _collapse_key({
        "parent_doc_or_dup_group": "doc-canonical",
        "content_hash": "abc",
        "title": "T",
        "domain": "d.com",
    }) == "p:doc-canonical"
    assert _collapse_key({
        "parent_doc_or_dup_group": "  grp  ",
        "content_hash": "xyz",
    }) == "p:grp"
    # empty / missing parent falls through to content_hash
    assert _collapse_key({
        "parent_doc_or_dup_group": "",
        "content_hash": "abc",
        "title": "T",
        "domain": "d.com",
    }) == "h:abc"
    assert _collapse_key({
        "parent_doc_or_dup_group": None,
        "content_hash": "abc",
        "title": "T",
        "domain": "d.com",
    }) == "h:abc"
    assert _collapse_key({"content_hash": "abc", "title": "T", "domain": "d.com"}) == "h:abc"
    # no parent + no hash → title|domain
    assert _collapse_key({"content_hash": "", "title": " Hello ", "domain": "D.COM"}) == "t:hello|d.com"
    assert _collapse_key({"content_hash": None, "title": "X", "domain": "y"}) == "t:x|y"


def test_collapse_search_rows_keeps_highest_score() -> None:
    rows = [
        {"content_hash": "h1", "url": "https://a/1", "score": 1.0, "title": "A"},
        {"content_hash": "h1", "url": "https://b/2", "score": 3.5, "title": "A"},
        {"content_hash": "h2", "url": "https://c/3", "score": 2.0, "title": "B"},
        {"content_hash": "h1", "url": "https://d/4", "score": 3.5, "title": "A"},  # tie → keep first winner
    ]
    out = _collapse_search_rows(rows)
    assert len(out) == 2
    by_hash = {r["content_hash"]: r for r in out}
    assert by_hash["h1"]["url"] == "https://b/2"
    assert by_hash["h1"]["score"] == 3.5
    assert by_hash["h2"]["url"] == "https://c/3"


def test_collapse_search_rows_by_parent_doc_or_dup_group() -> None:
    """Same parent group collapses even when content_hash differs (near-dups)."""
    rows = [
        {
            "parent_doc_or_dup_group": "doc-root",
            "content_hash": "h-a",
            "url": "https://a/1",
            "score": 1.0,
            "title": "A",
        },
        {
            "parent_doc_or_dup_group": "doc-root",
            "content_hash": "h-b",  # different hash, same near-dup group
            "url": "https://b/2",
            "score": 4.0,
            "title": "A (edit)",
        },
        {
            "parent_doc_or_dup_group": "doc-other",
            "content_hash": "h-c",
            "url": "https://c/3",
            "score": 2.0,
            "title": "B",
        },
        {
            "parent_doc_or_dup_group": "doc-root",
            "content_hash": "h-d",
            "url": "https://d/4",
            "score": 4.0,  # tie with h-b → keep first winner (b)
            "title": "A (rev)",
        },
    ]
    out = _collapse_search_rows(rows)
    assert len(out) == 2
    by_parent = {r["parent_doc_or_dup_group"]: r for r in out}
    assert by_parent["doc-root"]["url"] == "https://b/2"
    assert by_parent["doc-root"]["score"] == 4.0
    assert by_parent["doc-other"]["url"] == "https://c/3"


def test_collapse_search_rows_parent_takes_precedence_over_hash() -> None:
    """Different content_hash but shared parent → one hit; hash alone is ignored."""
    rows = [
        {"parent_doc_or_dup_group": "g1", "content_hash": "h1", "url": "https://a/1", "score": 2.0},
        {"parent_doc_or_dup_group": "g1", "content_hash": "h2", "url": "https://b/2", "score": 1.0},
        # no parent: still collapses exact hash dups
        {"parent_doc_or_dup_group": None, "content_hash": "h3", "url": "https://c/3", "score": 1.0},
        {"parent_doc_or_dup_group": "", "content_hash": "h3", "url": "https://d/4", "score": 3.0},
    ]
    out = _collapse_search_rows(rows)
    assert len(out) == 2
    assert out[0]["url"] == "https://a/1"  # higher score within g1
    assert out[1]["url"] == "https://d/4"  # higher score within h3


def test_search_collapses_same_content_hash_different_urls(tmp_path: Path) -> None:
    """Two JSONL docs, same content_hash, different URLs → one search hit."""
    jsonl_dir = tmp_path / "jsonl"
    db_path = tmp_path / "duckdb" / "metadata.duckdb"
    body = (
        "Exact duplicate content about climate markets and carbon credits "
        "appearing on two syndication endpoints should collapse in search."
    )
    shared_hash = "deadbeefcafebabe"
    _write_doc(
        jsonl_dir, 1,
        title="Climate markets update",
        text=body,
        domain="news.example",
        content_hash_val=shared_hash,
        url="https://news.example/climate",
    )
    _write_doc(
        jsonl_dir, 2,
        title="Climate markets update",
        text=body,
        domain="mirror.example",
        content_hash_val=shared_hash,
        url="https://mirror.example/climate",
    )
    # A third unrelated doc so total is meaningful.
    _write_doc(
        jsonl_dir, 3,
        title="Sports scoreboard",
        text="Football match results unrelated to climate markets entirely.",
        domain="sports.example",
        content_hash_val="1111222233334444",
        url="https://sports.example/scores",
    )

    idx = DuckDbIndex(db_path, jsonl_dir, None)
    try:
        for mode in ("prefix", "substring", "auto", "fts"):
            res = idx.search("climate markets", mode=mode)
            hashes = [r.get("content_hash") for r in res["rows"]]
            assert hashes.count(shared_hash) == 1, (
                f"mode={mode}: expected 1 hit for shared hash, got {hashes}"
            )
            # Unique total among candidates: climate dup pair → 1, not 2.
            climate_rows = [r for r in res["rows"] if r.get("content_hash") == shared_hash]
            assert len(climate_rows) == 1
            assert res["total"] >= 1
            # Full match set for this corpus is small; total should not count both dups.
            assert res["total"] <= 2  # climate unique + maybe sports if it matched
    finally:
        idx.close()


def test_search_collapses_same_parent_doc_or_dup_group(tmp_path: Path) -> None:
    """Near-dups (different content_hash, shared parent) → one search hit."""
    jsonl_dir = tmp_path / "jsonl"
    db_path = tmp_path / "duckdb" / "metadata.duckdb"
    body_a = (
        "Near-duplicate content about climate markets and carbon credits "
        "appearing under slightly edited syndication should collapse in search."
    )
    body_b = (
        "Near-duplicate content about climate markets and carbon credits "
        "appearing under lightly rewritten syndication should collapse in search."
    )
    shared_parent = "doc-climate-canonical"
    _write_doc(
        jsonl_dir, 1,
        title="Climate markets update",
        text=body_a,
        domain="news.example",
        content_hash_val="aaaa1111bbbb2222",
        url="https://news.example/climate",
        parent_doc_or_dup_group=shared_parent,
    )
    _write_doc(
        jsonl_dir, 2,
        title="Climate markets update (wire)",
        text=body_b,
        domain="wire.example",
        content_hash_val="cccc3333dddd4444",
        url="https://wire.example/climate",
        parent_doc_or_dup_group=shared_parent,
    )
    _write_doc(
        jsonl_dir, 3,
        title="Sports scoreboard",
        text="Football match results unrelated to climate markets entirely.",
        domain="sports.example",
        content_hash_val="1111222233334444",
        url="https://sports.example/scores",
        parent_doc_or_dup_group="doc-sports",
    )

    idx = DuckDbIndex(db_path, jsonl_dir, None)
    try:
        for mode in ("prefix", "substring", "auto", "fts"):
            res = idx.search("climate markets", mode=mode)
            parents = [r.get("parent_doc_or_dup_group") for r in res["rows"]]
            assert parents.count(shared_parent) == 1, (
                f"mode={mode}: expected 1 hit for parent group, got {parents}"
            )
            climate_rows = [
                r for r in res["rows"]
                if r.get("parent_doc_or_dup_group") == shared_parent
            ]
            assert len(climate_rows) == 1
            # Must not surface both near-dup URLs.
            urls = {r["url"] for r in res["rows"]}
            assert not (
                "https://news.example/climate" in urls
                and "https://wire.example/climate" in urls
            )
            # total should count the parent group once, not both near-dups
            assert res["total"] <= 2
    finally:
        idx.close()


def test_search_collapse_null_hash_falls_back_to_title_domain(tmp_path: Path) -> None:
    jsonl_dir = tmp_path / "jsonl"
    db_path = tmp_path / "duckdb" / "metadata.duckdb"
    body = "Syndicated finance briefing without a content hash field populated."
    _write_doc(jsonl_dir, 1, title="Finance Briefing", text=body, domain="wire.example", content_hash_val=None)
    _write_doc(jsonl_dir, 2, title="Finance Briefing", text=body, domain="wire.example", content_hash_val="")
    # Different domain → different collapse key when hash is empty.
    _write_doc(jsonl_dir, 3, title="Finance Briefing", text=body, domain="other.example", content_hash_val=None)

    idx = DuckDbIndex(db_path, jsonl_dir, None)
    try:
        res = idx.search("finance briefing", mode="prefix")
        # Same title+domain collapsed; other domain kept → 2 unique.
        assert res["total"] == 2
        assert len(res["rows"]) == 2
        domains = sorted(r["domain"] for r in res["rows"])
        assert domains == ["other.example", "wire.example"]
    finally:
        idx.close()


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


@pytest.mark.asyncio
async def test_worker_exact_dup_skips_batch_buffer(tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """EXACT_DUP must not be appended to the batch buffer (no second JSONL row)."""
    state = StateDB(f"sqlite:///{tmp_project / 'state.db'}")
    state.init()
    planner = Planner(state)
    engine = WorkerEngine(state, planner, concurrency=1, silent_progress=True)

    body = " ".join(["Exact duplicate body for storage skip test."] * 20)
    cap_new = _make_cap("https://a.test/x", body)
    cap_dup = _make_cap("https://b.test/y", body, observed_str="2024-01-02T00:00:00+00:00")
    cap_nearish = _make_cap(
        "https://c.test/z",
        " ".join(["Completely different unique document content for keep path."] * 20),
        observed_str="2024-01-03T00:00:00+00:00",
    )

    # Drive the per-capture branch by mocking adapter + task plumbing.
    async def fake_run_partition(partition, context):
        yield cap_new
        yield cap_dup
        yield cap_nearish

    adapter = MagicMock()
    adapter.run_partition = fake_run_partition
    engine._registry = MagicMock()
    engine._registry.get.return_value = adapter
    engine._topic_filter_for = lambda _job_id: None  # type: ignore[method-assign]
    engine._is_tty = False

    # Bypass flush side effects; we only care about buffer membership.
    async def noop_flush(force: bool = False) -> None:
        return None

    engine._flush = noop_flush  # type: ignore[method-assign]

    from awareness.schemas.jobs import JobKind, JobState, JobStatus, TaskState

    state.create_job(
        JobState(
            job_id="j-test",
            kind=JobKind.BACKFILL,
            status=JobStatus.RUNNING,
            request={"sources": ["local_fixture"]},
        )
    )
    task = TaskState(
        task_id="t-test",
        job_id="j-test",
        source_type=SourceKind.LOCAL_FIXTURE,
        partition_key="pk",
        payload={},
    )
    state.add_tasks([task])

    await engine._run_task(task)

    buffered_urls = [c.url for c in engine._batch_buffer]
    assert "https://a.test/x" in buffered_urls
    assert "https://b.test/y" not in buffered_urls, "EXACT_DUP must not be buffered"
    assert "https://c.test/z" in buffered_urls

    job = state.get_job("j-test")
    assert job is not None
    # NEW + unique third doc emitted; EXACT_DUP counted as dropped not emitted.
    assert job.docs_emitted == 2
    assert job.docs_dedup_dropped >= 1


@pytest.mark.asyncio
async def test_worker_tight_near_dup_skips_batch_buffer(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NEAR_DUP with Hamming ≤ TIGHT_NEAR_STORE_THRESHOLD must not be buffered."""
    state = StateDB(f"sqlite:///{tmp_project / 'state.db'}")
    state.init()
    planner = Planner(state)
    engine = WorkerEngine(state, planner, concurrency=1, silent_progress=True)

    cap_new = _make_cap(
        "https://a.test/base",
        " ".join(["Canonical article body for near-dup storage policy."] * 20),
    )
    cap_tight = _make_cap(
        "https://b.test/tight",
        " ".join(["Tight near-duplicate body that should skip store."] * 20),
        observed_str="2024-01-02T00:00:00+00:00",
    )
    cap_loose = _make_cap(
        "https://c.test/loose",
        " ".join(["Loose near-duplicate body that should still store."] * 20),
        observed_str="2024-01-03T00:00:00+00:00",
    )
    cap_unique = _make_cap(
        "https://d.test/unique",
        " ".join(["Completely unrelated unique document content keep path."] * 20),
        observed_str="2024-01-04T00:00:00+00:00",
    )

    # Controlled decisions: real simhash distances are flaky for unit policy tests.
    outcomes = {
        cap_new.doc_id: DedupOutcome(
            decision=DedupDecision.NEW, dup_group=cap_new.doc_id, reason="new_content"
        ),
        cap_tight.doc_id: DedupOutcome(
            decision=DedupDecision.NEAR_DUP,
            dup_group=cap_new.doc_id,
            reason="simhash128_hamming=5",
            hamming=5,
        ),
        cap_loose.doc_id: DedupOutcome(
            decision=DedupDecision.NEAR_DUP,
            dup_group=cap_new.doc_id,
            reason="simhash128_hamming=18",
            hamming=18,
        ),
        cap_unique.doc_id: DedupOutcome(
            decision=DedupDecision.NEW, dup_group=cap_unique.doc_id, reason="new_content"
        ),
    }

    def fake_evaluate(cap: DocCapture) -> DedupOutcome:
        out = outcomes[cap.doc_id]
        cap.parent_doc_or_dup_group = out.dup_group
        return out

    engine._dedup.evaluate = fake_evaluate  # type: ignore[method-assign]

    async def fake_run_partition(partition, context):
        yield cap_new
        yield cap_tight
        yield cap_loose
        yield cap_unique

    adapter = MagicMock()
    adapter.run_partition = fake_run_partition
    engine._registry = MagicMock()
    engine._registry.get.return_value = adapter
    engine._topic_filter_for = lambda _job_id: None  # type: ignore[method-assign]
    engine._is_tty = False

    async def noop_flush(force: bool = False) -> None:
        return None

    engine._flush = noop_flush  # type: ignore[method-assign]

    from awareness.schemas.jobs import JobKind, JobState, JobStatus, TaskState

    state.create_job(
        JobState(
            job_id="j-near",
            kind=JobKind.BACKFILL,
            status=JobStatus.RUNNING,
            request={"sources": ["local_fixture"]},
        )
    )
    task = TaskState(
        task_id="t-near",
        job_id="j-near",
        source_type=SourceKind.LOCAL_FIXTURE,
        partition_key="pk",
        payload={},
    )
    state.add_tasks([task])

    before_tight = get_metrics().counter_sum("dedup.tight_near_skipped")
    await engine._run_task(task)

    buffered_urls = [c.url for c in engine._batch_buffer]
    assert "https://a.test/base" in buffered_urls
    assert "https://b.test/tight" not in buffered_urls, "tight NEAR_DUP must not be buffered"
    assert get_metrics().counter_sum("dedup.tight_near_skipped") == before_tight + 1
    assert "https://c.test/loose" in buffered_urls, "loose NEAR_DUP must still be buffered"
    assert "https://d.test/unique" in buffered_urls

    # parent linkage still applied for the tight skip path
    assert cap_tight.parent_doc_or_dup_group == cap_new.doc_id
    assert cap_loose.parent_doc_or_dup_group == cap_new.doc_id

    job = state.get_job("j-near")
    assert job is not None
    # NEW + loose NEAR_DUP + unique NEW stored; tight NEAR_DUP dropped only.
    assert job.docs_emitted == 3
    assert job.docs_dedup_dropped >= 2  # tight (skip) + loose (store+count)
