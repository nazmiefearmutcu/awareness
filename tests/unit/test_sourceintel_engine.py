"""SourceIntelEngine tests against a synthetic DuckDB corpus.

Corpus pattern mirrors tests/unit/test_duckdb_related.py: JSONL chunk files
read by DuckDbIndex's ``captures`` view. Timestamps are relative to
``datetime.now(UTC)`` so the 30-day velocity/replication windows are live.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from awareness.sourceintel.engine import SourceIntelEngine, UnknownDomainError
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
    domain: str,
    group: str | None = None,
    text: str = "body text here",
    title: str = "t",
    language: str = "en",
    source_type: str = "rss",
    url: str | None = None,
    days_ago: int = 1,
) -> None:
    day = root / "captures" / "2026" / "06" / "01"
    day.mkdir(parents=True, exist_ok=True)
    ts = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}",
        capture_id=f"cap-{idx}",
        parent_doc_or_dup_group=group,
        source_type=source_type,
        domain=domain,
        url=url or f"https://{domain}/{idx}",
        canonical_url=url or f"https://{domain}/{idx}",
        fetch_ts=ts,
        observed_ts=ts,
        title=title,
        text=text,
        language=language,
        content_hash=f"hash-{idx}",
    )
    (day / f"chunk-{idx}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


@pytest.fixture()
def engine(tmp_path: Path) -> Iterator[SourceIntelEngine]:
    idx = DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )
    yield SourceIntelEngine(idx)
    idx.close()


def test_replication_map_origin_to_replica(tmp_path: Path, engine: SourceIntelEngine) -> None:
    root = tmp_path / "jsonl"
    _write_doc(root, 0, domain="origin.com", group="grp-1", days_ago=10)
    _write_doc(root, 1, domain="replica.com", group="grp-1", days_ago=1)
    _write_doc(root, 2, domain="unique.com", group=None, days_ago=1)

    edges = engine.replication_map()
    assert len(edges) == 1
    edge = edges[0]
    assert edge.origin == "origin.com"
    assert edge.replica == "replica.com"
    assert edge.count == 1
    assert len(edge.sample_urls) == 2
    assert edge.sample_urls == ["https://origin.com/0", "https://replica.com/1"]


def test_replication_map_window_filters_old_groups(
    tmp_path: Path, engine: SourceIntelEngine
) -> None:
    root = tmp_path / "jsonl"
    _write_doc(root, 0, domain="old.com", group="grp-old", days_ago=40)
    _write_doc(root, 1, domain="new.com", group="grp-old", days_ago=30)

    assert engine.replication_map() == []
    edges = engine.replication_map(window_days=45)
    assert len(edges) == 1
    assert edges[0].origin == "old.com"


def test_replication_map_ignores_null_parent(tmp_path: Path, engine: SourceIntelEngine) -> None:
    root = tmp_path / "jsonl"
    _write_doc(root, 0, domain="a.com", group=None, days_ago=1)
    _write_doc(root, 1, domain="b.com", group=None, days_ago=1)

    assert engine.replication_map() == []
    assert all(s.replication_ratio == 0.0 for s in engine.domain_rank())


def test_rank_prefers_unique_high_quality_over_copycat(
    tmp_path: Path, engine: SourceIntelEngine
) -> None:
    root = tmp_path / "jsonl"
    long_unique = "unique market analysis report " * 60  # 2100 chars
    for i in range(3):
        _write_doc(root, i, domain="writer.com", group=None, text=long_unique, days_ago=2)
        _write_doc(root, 10 + i, domain="mirror.com", group="g-shared", days_ago=1)
    _write_doc(root, 20, domain="source.com", group="g-shared", days_ago=1)

    ranked = engine.domain_rank()
    by_domain = {s.domain: s for s in ranked}
    assert by_domain["writer.com"].score > by_domain["mirror.com"].score
    assert by_domain["mirror.com"].replication_ratio == 1.0
    assert by_domain["writer.com"].replication_ratio == 0.0
    assert ranked[0].domain == "writer.com"


def test_rank_deterministic_tie_break(tmp_path: Path, engine: SourceIntelEngine) -> None:
    root = tmp_path / "jsonl"
    text = "tie breaker content " * 20
    _write_doc(root, 0, domain="zeta.com", group=None, text=text, days_ago=2)
    _write_doc(root, 1, domain="alpha.com", group=None, text=text, days_ago=2)

    ranked = engine.domain_rank()
    assert [s.domain for s in ranked] == ["alpha.com", "zeta.com"]
    assert ranked[0].score == ranked[1].score


def test_rank_window_returns_empty_for_reversed_window(
    tmp_path: Path, engine: SourceIntelEngine
) -> None:
    root = tmp_path / "jsonl"
    _write_doc(root, 0, domain="a.com", group=None, days_ago=2)
    later = datetime.now(UTC) - timedelta(days=1)
    assert engine.domain_rank(start=later, end=datetime.now(UTC) - timedelta(days=2)) == []


def test_domain_profile_aggregates(tmp_path: Path, engine: SourceIntelEngine) -> None:
    root = tmp_path / "jsonl"
    en_text = "the market data analysis report"
    tr_text = "piyasa analiz raporu"
    _write_doc(root, 0, domain="a.com", text=en_text, language="en-US", days_ago=2)
    _write_doc(root, 1, domain="a.com", text=en_text, language="en", days_ago=2)
    _write_doc(root, 2, domain="a.com", text=tr_text, language="tr", source_type="gdelt", days_ago=5)

    profile = engine.domain_profile("https://www.a.com/story")
    assert profile.domain == "a.com"
    assert profile.total_captures == 3
    assert profile.first_seen is not None and profile.last_seen is not None
    assert profile.first_seen <= profile.last_seen
    assert profile.avg_doc_length == pytest.approx((2 * len(en_text) + len(tr_text)) / 3.0, abs=0.1)
    assert [lang.language for lang in profile.languages] == ["en", "tr"]
    assert profile.languages[0].count == 2
    terms = {t.term: t.count for t in profile.top_terms}
    assert terms["market"] == 2
    assert terms["data"] == 2
    assert "the" not in terms
    assert profile.captures_per_day > 0.0
    types = {t.source_type for t in profile.source_types}
    assert types == {"rss", "gdelt"}


def test_domain_profile_normalizes_bare_domain(tmp_path: Path, engine: SourceIntelEngine) -> None:
    root = tmp_path / "jsonl"
    _write_doc(root, 0, domain="Example.COM", group=None, days_ago=1)
    profile = engine.domain_profile("Example.COM")
    assert profile.domain == "example.com"


def test_domain_profile_unknown_raises(tmp_path: Path, engine: SourceIntelEngine) -> None:
    root = tmp_path / "jsonl"
    _write_doc(root, 0, domain="known.com", group=None, days_ago=1)
    with pytest.raises(UnknownDomainError):
        engine.domain_profile("nope.com")


def test_domain_profile_invalid_domain_raises(tmp_path: Path, engine: SourceIntelEngine) -> None:
    with pytest.raises(ValueError):
        engine.domain_profile("  ")


def test_top_replicators_ranks_copiers(tmp_path: Path, engine: SourceIntelEngine) -> None:
    root = tmp_path / "jsonl"
    _write_doc(root, 0, domain="orig.com", group="g1", days_ago=10)
    _write_doc(root, 1, domain="mirror-a.com", group="g1", days_ago=5)
    _write_doc(root, 2, domain="orig2.com", group="g2", days_ago=10)
    _write_doc(root, 3, domain="mirror-a.com", group="g2", days_ago=5)
    _write_doc(root, 4, domain="mirror-b.com", group="g1", days_ago=5)
    _write_doc(root, 5, domain="clean.com", group=None, days_ago=5)

    replicators = engine.top_replicators()
    assert replicators[0].domain == "mirror-a.com"
    assert replicators[0].score == 2.0
    names = {r.domain for r in replicators}
    assert "mirror-b.com" in names
    assert "clean.com" not in names


def test_freshness_report_staleness(tmp_path: Path, engine: SourceIntelEngine) -> None:
    root = tmp_path / "jsonl"
    _write_doc(root, 0, domain="active.com", group=None, days_ago=1)
    _write_doc(root, 1, domain="active.com", group=None, days_ago=2)
    _write_doc(root, 2, domain="stale.com", group=None, days_ago=400)

    report = {f.domain: f for f in engine.freshness_report()}
    assert report["active.com"].captures_7d == 2
    assert report["active.com"].captures_30d == 2
    assert report["active.com"].days_since_last == 1
    assert report["stale.com"].captures_7d == 0
    assert report["stale.com"].captures_30d == 0
    assert report["stale.com"].days_since_last >= 399


def test_empty_corpus_returns_empty_lists(tmp_path: Path, engine: SourceIntelEngine) -> None:
    assert engine.domain_rank() == []
    assert engine.replication_map() == []
    assert engine.top_replicators() == []
    assert engine.freshness_report() == []
    with pytest.raises(UnknownDomainError):
        engine.domain_profile("example.com")
