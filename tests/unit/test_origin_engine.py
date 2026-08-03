"""OriginEngine tests against a synthetic DuckDB corpus.

Corpus pattern mirrors tests/unit/test_sourceintel_engine.py: JSONL chunk
files read by DuckDbIndex's ``captures`` view. Timestamps are relative to
``datetime.now(UTC)`` so the 30-day window is live, with minute-level
offsets to pin down ``lead_minutes``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from awareness.origin.engine import OriginEngine
from awareness.storage.duckdb_index import DuckDbIndex

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
    ts: datetime,
    domain: str,
    group: str | None = None,
    text: str = "breaking news about bitcoin",
    title: str = "t",
    language: str = "en",
    source_type: str = "rss",
    url: str | None = None,
) -> None:
    day = root / "captures" / f"{ts:%Y}" / f"{ts:%m}" / f"{ts:%d}"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}",
        capture_id=f"cap-{idx}",
        parent_doc_or_dup_group=group,
        source_type=source_type,
        domain=domain,
        url=url or f"https://{domain}/{idx}",
        canonical_url=url or f"https://{domain}/{idx}",
        fetch_ts=ts.isoformat(),
        observed_ts=ts.isoformat(),
        title=title,
        text=text,
        language=language,
        content_hash=f"hash-{idx}",
    )
    (day / f"chunk-{idx}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


@pytest.fixture()
def engine(tmp_path: Path) -> Iterator[OriginEngine]:
    idx = DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )
    yield OriginEngine(idx)
    idx.close()


def _origin_cluster(root: Path) -> tuple[datetime, datetime]:
    """Docs for one 2-domain cluster: origin 90 min before its replica."""
    origin_ts = datetime.now(UTC) - timedelta(days=3, minutes=90)
    replica_ts = origin_ts + timedelta(minutes=90)
    _write_doc(
        root, 1, ts=origin_ts, domain="wire.example", group="grp-1",
        title="Bitcoin breaks out",
        text="bitcoin rally shocks markets",
        url="https://wire.example/story-1",
    )
    _write_doc(
        root, 2, ts=replica_ts, domain="echo.example", group="grp-1",
        title="Bitcoin breaks out",
        text="bitcoin rally shocks markets",
        url="https://echo.example/repost",
    )
    return origin_ts, replica_ts


def test_story_origins_finds_origin_and_replica(
    tmp_path: Path, engine: OriginEngine
) -> None:
    root = tmp_path / "jsonl"
    origin_ts, replica_ts = _origin_cluster(root)
    _write_doc(root, 3, ts=datetime.now(UTC) - timedelta(days=2), domain="solo.example", group=None)

    stories = engine.story_origins("bitcoin")
    assert len(stories) == 1
    story = stories[0]
    assert story.term == "bitcoin"
    assert story.origin_domain == "wire.example"
    assert story.origin_url == "https://wire.example/story-1"
    assert story.origin_title == "Bitcoin breaks out"
    assert story.origin_ts == origin_ts
    assert story.replica_count == 1
    assert len(story.replicas) == 1
    assert story.replicas[0].domain == "echo.example"
    assert story.replicas[0].first_ts == replica_ts
    assert story.lead_minutes == 90


def test_story_origins_skips_singletons_and_null_groups(
    tmp_path: Path, engine: OriginEngine
) -> None:
    root = tmp_path / "jsonl"
    now = datetime.now(UTC)
    _origin_cluster(root)
    # Singleton with a group id: fewer than 2 docs → not a story.
    _write_doc(root, 4, ts=now - timedelta(days=2), domain="lone.example", group="grp-solo")
    # Two docs with NULL groups: cannot be clustered.
    _write_doc(root, 5, ts=now - timedelta(days=2), domain="a.example", group=None)
    _write_doc(root, 6, ts=now - timedelta(days=1), domain="b.example", group=None)

    stories = engine.story_origins("bitcoin")
    assert len(stories) == 1
    assert stories[0].origin_domain == "wire.example"


def test_story_origins_term_matching_uses_word_boundaries(
    tmp_path: Path, engine: OriginEngine
) -> None:
    root = tmp_path / "jsonl"
    now = datetime.now(UTC)
    # "catalog"/"cats" must not match the term "cat" (word boundaries).
    _write_doc(
        root, 1, ts=now - timedelta(days=3, minutes=60),
        domain="catalog.example", group="grp-2", text="catalog of cats everywhere",
    )
    _write_doc(
        root, 2, ts=now - timedelta(days=3),
        domain="cats.example", group="grp-2", text="catalog of cats everywhere",
    )
    # An exact-word cluster must match.
    _write_doc(
        root, 3, ts=now - timedelta(days=3, minutes=45),
        domain="pet.example", group="grp-3", text="a cat sat on a mat",
    )
    _write_doc(
        root, 4, ts=now - timedelta(days=3),
        domain="pet2.example", group="grp-3", text="a cat sat on a mat",
    )

    stories = engine.story_origins("cat")
    assert len(stories) == 1
    assert stories[0].origin_domain == "pet.example"


def test_story_origins_sorts_by_replica_count_then_origin_ts(
    tmp_path: Path, engine: OriginEngine
) -> None:
    root = tmp_path / "jsonl"
    now = datetime.now(UTC)
    # Cluster with 2 replicas, older origin.
    _write_doc(root, 1, ts=now - timedelta(days=5, minutes=120), domain="old.example", group="grp-a")
    _write_doc(root, 2, ts=now - timedelta(days=5), domain="r1.example", group="grp-a")
    _write_doc(root, 3, ts=now - timedelta(days=5, minutes=-30), domain="r2.example", group="grp-a")
    # Cluster with 1 replica, newer origin.
    _write_doc(root, 4, ts=now - timedelta(days=1, minutes=60), domain="new.example", group="grp-b")
    _write_doc(root, 5, ts=now - timedelta(days=1), domain="r3.example", group="grp-b")

    stories = engine.story_origins("bitcoin")
    assert [s.origin_domain for s in stories] == ["old.example", "new.example"]
    assert [s.replica_count for s in stories] == [2, 1]


def test_publisher_firsts_ranks_origin_domains(
    tmp_path: Path, engine: OriginEngine
) -> None:
    root = tmp_path / "jsonl"
    _origin_cluster(root)

    publishers = engine.publisher_firsts("bitcoin")
    assert [p.domain for p in publishers] == ["wire.example"]
    assert publishers[0].origin_count == 1
    assert publishers[0].total_stories == 1


def test_publisher_firsts_ranks_by_origin_count(
    tmp_path: Path, engine: OriginEngine
) -> None:
    root = tmp_path / "jsonl"
    now = datetime.now(UTC)
    # wire.example originates two clusters; fresh.example originates one.
    for i, group in enumerate(("grp-a", "grp-b"), start=1):
        _write_doc(root, i, ts=now - timedelta(days=4 - i, minutes=90), domain="wire.example", group=group)
        _write_doc(root, 10 + i, ts=now - timedelta(days=4 - i), domain=f"echo{i}.example", group=group)
    _write_doc(root, 20, ts=now - timedelta(days=1, minutes=90), domain="fresh.example", group="grp-c")
    _write_doc(root, 21, ts=now - timedelta(days=1), domain="copy.example", group="grp-c")

    publishers = engine.publisher_firsts("bitcoin")
    assert [p.domain for p in publishers] == ["wire.example", "fresh.example"]
    assert publishers[0].origin_count == 2
    assert publishers[0].total_stories == 2
    assert publishers[1].origin_count == 1


def test_empty_corpus_and_unknown_term(tmp_path: Path, engine: OriginEngine) -> None:
    assert engine.story_origins("bitcoin") == []
    assert engine.publisher_firsts("bitcoin") == []

    root = tmp_path / "jsonl"
    _origin_cluster(root)
    assert engine.story_origins("dogecoin") == []
    assert engine.publisher_firsts("dogecoin") == []


def test_bad_inputs_raise_value_error(tmp_path: Path, engine: OriginEngine) -> None:
    with pytest.raises(ValueError):
        engine.story_origins("")
    with pytest.raises(ValueError):
        engine.story_origins("x" * 201)
    with pytest.raises(ValueError):
        engine.story_origins("bitcoin", window_days=0)
    with pytest.raises(ValueError):
        engine.story_origins("bitcoin", window_days=400)
    with pytest.raises(ValueError):
        engine.publisher_firsts("")
