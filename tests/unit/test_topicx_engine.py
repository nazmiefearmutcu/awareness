"""Unit tests for the topicx engine (topic lifecycle, emerging, impact).

Builds small in-memory corpora through the JSONL-chunk pattern used across
the unit suite (see ``tests/unit/test_analytics_engine.py`` /
``tests/unit/test_sourceintel_engine.py``). Timestamps are relative to
``datetime.now(UTC)`` so the phase windows and the replication windows are
live, mirroring the sourceintel suite.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from awareness.storage.duckdb_index import DuckDbIndex
from awareness.topicx.engine import TopicEngine
from awareness.util.timeutil import floor_to_day

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
    title: str = "",
    text: str = "",
    group: str | None = None,
    language: str = "en",
    days_ago: int = 0,
) -> None:
    ts = (datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)).isoformat()
    day = root / "captures" / f"{ts[:4]}" / f"{ts[5:7]}" / f"{ts[8:10]}"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}",
        capture_id=f"cap-{idx:04d}",
        parent_doc_or_dup_group=group,
        source_type="rss",
        domain=domain,
        url=f"https://{domain}/{idx}",
        fetch_ts=ts,
        observed_ts=ts,
        title=title,
        text=text,
        language=language,
        content_hash=f"hash-{idx}",
    )
    (day / f"chunk-{idx:04d}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _index(tmp_path: Path) -> DuckDbIndex:
    return DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )


def _engine(tmp_path: Path) -> TopicEngine:
    return TopicEngine(_index(tmp_path))


def _today() -> datetime:
    return floor_to_day(datetime.now(UTC))


# ── lifecycle phases ─────────────────────────────────────────────────────────


def test_lifecycle_phase_classification(tmp_path: Path) -> None:
    root = tmp_path / "jsonl"
    today = _today()
    # A: EMERGING — 3 docs today, none before.
    _write_doc(root, 1, domain="example.com", title="Alphaflare report", text="alphaflare signal")
    _write_doc(root, 2, domain="news.example", title="Alphaflare news", text="alphaflare update")
    _write_doc(root, 3, domain="example.com", title="Alphaflare recap", text="alphaflare recap")
    # B: EXPANDING — 1 doc yesterday, 3 docs today (growing).
    _write_doc(root, 4, domain="example.com", days_ago=1, title="Betawave", text="betawave mention")
    _write_doc(root, 5, domain="example.com", title="Betawave grows", text="betawave betawave")
    _write_doc(root, 6, domain="news.example", title="Betawave rising", text="betawave")
    _write_doc(root, 7, domain="example.com", title="Betawave more", text="betawave again")
    # C: DECLINING — 3 docs 10 days ago only.
    _write_doc(root, 8, domain="example.com", days_ago=10, title="Gammasink peak", text="gammasink gammasink")
    _write_doc(root, 9, domain="news.example", days_ago=10, title="Gammasink story", text="gammasink")
    _write_doc(root, 10, domain="example.com", days_ago=10, title="Gammasink end", text="gammasink")
    # D: DORMANT — 1 doc 2 days ago.
    _write_doc(root, 11, domain="example.com", days_ago=2, title="Deltadrip", text="deltadrip")

    engine = _engine(tmp_path)

    a = engine.lifecycle("alphaflare")
    assert a.phase == "EMERGING"
    assert a.peak_count == 3
    assert a.peak_date == today
    assert a.first_seen == today
    assert a.last_seen == today
    assert a.slope_7d > 0
    assert sum(b.count for b in a.counts) == 3
    assert len(a.counts) == 31  # window_days=30 -> 31 zero-filled buckets

    b = engine.lifecycle("betawave")
    assert b.phase == "EXPANDING"
    assert b.slope_7d > 0
    assert b.first_seen == today - timedelta(days=1)

    c = engine.lifecycle("gammasink")
    assert c.phase == "DECLINING"
    assert c.slope_7d == 0.0  # trailing-7d tail is all zeros (zero-variance)
    assert c.peak_count == 3
    assert c.first_seen == today - timedelta(days=10)

    d = engine.lifecycle("deltadrip")
    assert d.phase == "DORMANT"
    assert d.peak_count == 1


def test_lifecycle_flat_presence_is_stable(tmp_path: Path) -> None:
    root = tmp_path / "jsonl"
    for i in range(14):
        _write_doc(root, i + 1, days_ago=13 - i, domain="example.com", text="steadyflow")
    lc = _engine(tmp_path).lifecycle("steadyflow")
    assert lc.phase == "STABLE"
    assert lc.slope_7d == 0.0
    assert lc.peak_count == 1
    assert sum(b.count for b in lc.counts) == 14


def test_lifecycle_burst_then_drop_is_peaking(tmp_path: Path) -> None:
    root = tmp_path / "jsonl"
    _write_doc(root, 1, days_ago=5, domain="example.com", text="spikewave steady")
    _write_doc(root, 2, days_ago=2, domain="example.com", text="spikewave")
    _write_doc(root, 3, days_ago=2, domain="news.example", text="spikewave")
    _write_doc(root, 4, days_ago=2, domain="example.com", text="spikewave")
    _write_doc(root, 5, days_ago=2, domain="news.example", text="spikewave")
    _write_doc(root, 6, days_ago=1, domain="example.com", text="spikewave")
    _write_doc(root, 7, days_ago=1, domain="news.example", text="spikewave")
    _write_doc(root, 8, days_ago=1, domain="example.com", text="spikewave")
    lc = _engine(tmp_path).lifecycle("spikewave", window_days=14)
    assert lc.phase == "PEAKING"
    assert lc.peak_count == 4
    assert lc.slope_7d > 0  # 1 -> 3 -> 3 still climbs on the tail


def test_compare_lifecycles_and_validation(tmp_path: Path) -> None:
    root = tmp_path / "jsonl"
    _write_doc(root, 1, domain="example.com", text="alphaflare")
    _write_doc(root, 2, domain="example.com", text="betawave")
    engine = _engine(tmp_path)

    results = engine.compare_lifecycles(["alphaflare", "betawave"])
    assert [r.term for r in results] == ["alphaflare", "betawave"]

    with pytest.raises(ValueError):
        engine.compare_lifecycles([])
    with pytest.raises(ValueError):
        engine.compare_lifecycles([f"t{i}" for i in range(11)])

    with pytest.raises(ValueError):
        engine.lifecycle("")
    with pytest.raises(ValueError):
        engine.lifecycle("x" * 201)
    with pytest.raises(ValueError):
        engine.lifecycle("alphaflare", window_days=0)
    with pytest.raises(ValueError):
        engine.lifecycle("alphaflare", window_days=400)


def test_lifecycle_empty_corpus_is_zeroed_dormant(tmp_path: Path) -> None:
    lc = _engine(tmp_path).lifecycle("anything")
    assert lc.phase == "DORMANT"
    assert lc.counts == []
    assert lc.slope_7d == 0.0
    assert lc.peak_count == 0
    assert lc.first_seen is None
    assert lc.last_seen is None
    assert lc.peak_date is None


# ── emerging topics ──────────────────────────────────────────────────────────


def test_top_emerging_finds_recent_material_terms(tmp_path: Path) -> None:
    root = tmp_path / "jsonl"
    _write_doc(root, 1, domain="example.com", text="alphaflare signal")
    _write_doc(root, 2, domain="news.example", text="alphaflare update")
    _write_doc(root, 3, domain="example.com", text="alphaflare recap")
    _write_doc(root, 4, days_ago=1, domain="example.com", text="betawave mention")
    _write_doc(root, 5, domain="example.com", text="betawave rising betawave")
    _write_doc(root, 6, days_ago=2, domain="example.com", text="deltadrip")
    _write_doc(root, 7, days_ago=10, domain="example.com", text="gammasink")

    emerging = _engine(tmp_path).top_emerging(window_days=7, limit=20)
    by_term = {e.term: e for e in emerging}

    # alphaflare: 3 docs today -> emerging, across 2 domains.
    assert by_term["alphaflare"].count == 3
    assert by_term["alphaflare"].first_seen == _today()
    assert by_term["alphaflare"].domains_covered == 2
    # betawave: 1 doc yesterday + 2 today -> first seen yesterday, still emerging.
    assert by_term["betawave"].count == 3
    assert by_term["betawave"].first_seen == _today() - timedelta(days=1)
    # deltadrip (1 doc) and gammasink (10 days ago) are not emerging.
    assert "deltadrip" not in by_term
    assert "gammasink" not in by_term


def test_top_emerging_empty_corpus(tmp_path: Path) -> None:
    assert _engine(tmp_path).top_emerging() == []


# ── source impact ────────────────────────────────────────────────────────────


def test_source_impact_ranks_origin_above_pure_replicator(tmp_path: Path) -> None:
    root = tmp_path / "jsonl"
    # origin.com published first; replica.com copied it within the same
    # dup-group; unique.com never shares a group.
    _write_doc(root, 1, domain="origin.com", days_ago=10, group="grp-shared", text="shared market analysis")
    _write_doc(root, 2, domain="replica.com", days_ago=1, group="grp-shared", text="shared market analysis")
    _write_doc(root, 3, domain="unique.com", days_ago=1, group=None, text="original only story")

    result = _engine(tmp_path).source_impact(window_days=30, limit=20)

    assert len(result) == 1  # only origins carry impact rows
    row = result[0]
    assert row.domain == "origin.com"
    assert row.replica_edges == 1
    assert row.captures == 1
    assert row.impact_score > 1.0  # replica copies + full captures_norm
    # lead = 10 days ago vs 1 day ago -> ~9 days = 12960 minutes.
    assert row.avg_lead_minutes == pytest.approx(9 * 24 * 60, abs=120)


def test_source_impact_no_replication_is_empty(tmp_path: Path) -> None:
    root = tmp_path / "jsonl"
    _write_doc(root, 1, domain="a.com", group=None, text="story one")
    _write_doc(root, 2, domain="b.com", group=None, text="story two")
    assert _engine(tmp_path).source_impact(window_days=30) == []


# ── topic dominance ──────────────────────────────────────────────────────────


def test_topic_dominance_fractions_sum_to_one(tmp_path: Path) -> None:
    root = tmp_path / "jsonl"
    for i in range(3):
        _write_doc(
            root, i + 1, domain="news.example",
            text=f"dominwave rally report {i}",
        )
    _write_doc(root, 10, domain="blog.example", text="dominwave side note")

    result = _engine(tmp_path).topic_dominance("dominwave", window_days=14, limit=10)

    assert [r.domain for r in result] == ["news.example", "blog.example"]
    assert result[0].doc_count == 3
    assert result[0].doc_fraction == pytest.approx(0.75)
    assert result[1].doc_fraction == pytest.approx(0.25)
    assert sum(r.doc_fraction for r in result) == pytest.approx(1.0, abs=1e-6)
    assert all(-1.0 <= r.avg_sentiment <= 1.0 for r in result)
    assert result[0].avg_sentiment > 0.0  # "rally" is a lexicon positive


def test_topic_dominance_unknown_term_and_empty_corpus(tmp_path: Path) -> None:
    root = tmp_path / "jsonl"
    _write_doc(root, 1, domain="example.com", text="some other story")
    assert _engine(tmp_path).topic_dominance("ghostword") == []
    assert _engine(tmp_path).topic_dominance("ghostword", window_days=14) == []
    with pytest.raises(ValueError):
        _engine(tmp_path).topic_dominance("")
