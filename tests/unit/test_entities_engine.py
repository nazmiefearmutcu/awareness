"""Unit tests for the entity engine over a small DuckDB corpus."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from awareness.entities.engine import EntityEngine
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
    title: str = "",
    text: str = "",
    domain: str = "example.com",
) -> None:
    day = root / "captures" / f"{ts:%Y}" / f"{ts:%m}" / f"{ts:%d}"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}",
        capture_id=f"cap-{idx:04d}",
        source_type="rss",
        domain=domain,
        url=f"https://{domain}/{idx}",
        fetch_ts=ts.isoformat(),
        observed_ts=ts.isoformat(),
        title=title,
        text=text,
    )
    (day / f"chunk-{idx:04d}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _engine(jsonl_root: Path) -> EntityEngine:
    idx = DuckDbIndex(
        db_path=jsonl_root / "duckdb" / "metadata.duckdb",
        jsonl_dir=jsonl_root,
        iceberg_warehouse=None,
    )
    return EntityEngine(idx)


@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "jsonl"
    now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    base = now - timedelta(days=2)
    # Two docs about Apple Inc + New York, one about sports, one about BTC.
    _write_doc(
        root, 1, ts=base,
        title="Apple Inc expands in New York",
        text="Apple Inc announced plans to hire across New York City offices.",
    )
    _write_doc(
        root, 2, ts=base + timedelta(hours=2),
        title="Apple Inc earnings beat",
        text="Apple Inc reported strong earnings and mentioned New York.",
    )
    _write_doc(
        root, 3, ts=base + timedelta(hours=5),
        title="Sports roundup",
        text="The team won the final match yesterday.",
    )
    _write_doc(
        root, 4, ts=base + timedelta(hours=8),
        title="Bitcoin rally",
        text="$BTC surged to a record high while ETH followed.",
    )
    return root


def test_extract_from_corpus_aggregates(corpus: Path) -> None:
    engine = _engine(corpus)
    entities = engine.extract_from_corpus(limit_docs=100)
    by_name = {e.text: e for e in entities}
    assert by_name["Apple Inc"].kind == "ORG"
    assert by_name["Apple Inc"].count >= 2
    assert by_name["New York"].kind == "PLACE"
    assert by_name["BTC"].kind == "TICKER"


def test_extract_from_corpus_empty(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    assert engine.extract_from_corpus() == []


def test_co_occurrence(corpus: Path) -> None:
    engine = _engine(corpus)
    co = engine.co_occurrence("Apple Inc", window_days=30)
    names = {c.entity for c in co}
    assert "New York" in names


def test_co_occurrence_unknown(corpus: Path) -> None:
    engine = _engine(corpus)
    assert engine.co_occurrence("Zzzzznope", window_days=30) == []


def test_entity_trend(corpus: Path) -> None:
    engine = _engine(corpus)
    trend = engine.entity_trend("Apple Inc", window_days=7)
    assert sum(b.count for b in trend) >= 2
    assert all(b.count >= 0 for b in trend)


def test_correlation_same_series(corpus: Path) -> None:
    engine = _engine(corpus)
    # BTC and ETH appear in the same single doc → counts correlate strongly.
    res = engine.correlation("BTC", "ETH", window_days=7)
    assert res.n > 0
    assert res.r > 0.5


def test_correlation_zero_variance(corpus: Path) -> None:
    engine = _engine(corpus)
    res = engine.correlation("Zzzzznope", "BTC", window_days=7)
    assert res.r == 0.0
    assert res.best_r == 0.0
