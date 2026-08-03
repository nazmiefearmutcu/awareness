"""Unit tests for the corpus-quality engine (topic matrix + quality snapshot).

Builds a small in-memory corpus through the same JSONL-chunk pattern as the
rest of the unit suite (see ``test_analytics_engine.py``) with known
exact-duplicate hashes and near-dup parent groups, then drives
:class:`~awareness.corpusx.engine.CorpusXEngine` against it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from awareness.analytics.models import DomainCount
from awareness.corpusx.engine import CorpusXEngine
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

BASE = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

# (idx, domain, ts_delta, title, text, language, content_hash, parent)
# Exact dups: 1/2/3 share H1 (group root doc-1); 8/9 share H2 (group root doc-8).
_CORPUS: tuple[tuple[int, str, timedelta, str, str, str | None, str | None, str | None], ...] = (
    (1, "alpha.example", timedelta(0), "Alpha one", "bitcoin surges", "en", "H1", "doc-1"),
    (2, "alpha.example", timedelta(hours=1), "Alpha two", "bitcoin dips", "en", "H1", "doc-1"),
    (3, "alpha.example", timedelta(hours=2), "Alpha three", "bitcoin rally", "en", "H1", "doc-1"),
    (4, "alpha.example", timedelta(days=1), "Alpha four", "bitcoin boom", "en", None, "doc-4"),
    (5, "alpha.example", timedelta(days=1, hours=1), "Alpha five", "ethereum rises", "en", None, "doc-5"),
    (6, "alpha.example", timedelta(days=2), "Alpha six", "ethereum gains", "en", None, "doc-6"),
    (7, "alpha.example", timedelta(days=2, hours=1), "Alpha seven", "", None, None, "doc-7"),
    (8, "beta.example", timedelta(days=1, hours=2), "Beta one", "bitcoin beta news", "en", "H2", "doc-8"),
    (9, "beta.example", timedelta(days=1, hours=3), "Beta two", "bitcoin beta recap", "en", "H2", "doc-8"),
    (10, "beta.example", timedelta(days=2, hours=2), "Beta three", "bitcoin flows", "en", None, "doc-10"),
    (11, "beta.example", timedelta(days=2, hours=3), "Beta four", "ethereum beta", "en", None, "doc-11"),
    (12, "beta.example", timedelta(days=2, hours=4), "Beta five", "sports roundup", "tr", None, "doc-12"),
    (13, "gamma.example", timedelta(days=2, hours=5), "Gamma one", "bitcoin gamma", "tr", None, "doc-13"),
    (14, "gamma.example", timedelta(days=3), "Gamma two", "ethereum gamma one", "tr", None, "doc-14"),
    (15, "gamma.example", timedelta(days=3), "Gamma three", "ethereum gamma two", None, None, "doc-15"),
)


def _write_doc(
    root: Path,
    idx: int,
    *,
    ts: datetime,
    title: str = "",
    text: str = "",
    domain: str = "example.com",
    language: str | None = None,
    content_hash: str | None = None,
    parent_doc_or_dup_group: str | None = None,
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
        language=language,
        content_hash=content_hash,
        parent_doc_or_dup_group=parent_doc_or_dup_group,
    )
    (day / f"chunk-{idx:04d}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _write_corpus(root: Path) -> None:
    for idx, domain, delta, title, text, language, content_hash, parent in _CORPUS:
        _write_doc(
            root,
            idx,
            ts=BASE + delta,
            title=title,
            text=text,
            domain=domain,
            language=language,
            content_hash=content_hash,
            parent_doc_or_dup_group=parent,
        )


def _engine(tmp_path: Path) -> CorpusXEngine:
    return CorpusXEngine(
        DuckDbIndex(
            db_path=tmp_path / "duckdb" / "metadata.duckdb",
            jsonl_dir=tmp_path / "jsonl",
            iceberg_warehouse=None,
        )
    )


def _expected_avg_length() -> float:
    total = sum(len(entry[4]) for entry in _CORPUS)
    return total / len(_CORPUS)


# ── topic matrix ────────────────────────────────────────────────────────────


def test_topic_matrix_rectangular_with_totals(tmp_path: Path) -> None:
    _write_corpus(tmp_path / "jsonl")
    engine = _engine(tmp_path)

    matrix = engine.topic_matrix(["bitcoin", "ethereum"], window_days=30, top_domains=3)

    assert matrix.terms == ["bitcoin", "ethereum"]
    assert matrix.domains == ["alpha.example", "beta.example", "gamma.example"]
    # Rectangular: one cell per (term, domain) pair, zero counts included.
    assert len(matrix.cells) == 2 * 3
    by_key = {(c.term, c.domain): c.count for c in matrix.cells}
    assert by_key == {
        ("bitcoin", "alpha.example"): 4,
        ("bitcoin", "beta.example"): 3,
        ("bitcoin", "gamma.example"): 1,
        ("ethereum", "alpha.example"): 2,
        ("ethereum", "beta.example"): 1,
        ("ethereum", "gamma.example"): 2,
    }
    assert matrix.totals["terms"] == {"bitcoin": 8, "ethereum": 5}
    assert matrix.totals["domains"] == {
        "alpha.example": 7,
        "beta.example": 5,
        "gamma.example": 3,
    }


def test_topic_matrix_zero_fill_for_missing_term(tmp_path: Path) -> None:
    _write_corpus(tmp_path / "jsonl")
    engine = _engine(tmp_path)

    matrix = engine.topic_matrix(["dogecoin"], window_days=30, top_domains=3)

    assert matrix.domains == ["alpha.example", "beta.example", "gamma.example"]
    assert all(c.count == 0 for c in matrix.cells)
    assert matrix.totals["terms"] == {"dogecoin": 0}


def test_topic_matrix_respects_window(tmp_path: Path) -> None:
    _write_corpus(tmp_path / "jsonl")
    engine = _engine(tmp_path)

    # Window = last day (fetch_ts >= max - 1 day): docs 6..15 qualify.
    matrix = engine.topic_matrix(["bitcoin", "ethereum"], window_days=1, top_domains=3)

    assert matrix.domains == ["beta.example", "gamma.example", "alpha.example"]
    by_key = {(c.term, c.domain): c.count for c in matrix.cells}
    assert by_key == {
        ("bitcoin", "beta.example"): 1,
        ("bitcoin", "gamma.example"): 1,
        ("bitcoin", "alpha.example"): 0,
        ("ethereum", "beta.example"): 1,
        ("ethereum", "gamma.example"): 2,
        ("ethereum", "alpha.example"): 1,
    }
    assert matrix.totals["terms"] == {"bitcoin": 2, "ethereum": 4}
    assert matrix.totals["domains"] == {
        "beta.example": 3,
        "gamma.example": 3,
        "alpha.example": 2,
    }


def test_topic_matrix_top_domains_caps_columns(tmp_path: Path) -> None:
    _write_corpus(tmp_path / "jsonl")
    engine = _engine(tmp_path)

    matrix = engine.topic_matrix(["bitcoin"], window_days=30, top_domains=2)

    assert matrix.domains == ["alpha.example", "beta.example"]
    assert len(matrix.cells) == 2
    assert matrix.totals["terms"] == {"bitcoin": 7}


def test_topic_matrix_empty_corpus_has_empty_domains(tmp_path: Path) -> None:
    engine = _engine(tmp_path)

    matrix = engine.topic_matrix(["bitcoin"])

    assert matrix.terms == ["bitcoin"]
    assert matrix.domains == []
    assert matrix.cells == []
    assert matrix.totals == {"terms": {"bitcoin": 0}, "domains": {}}


def test_topic_matrix_validation(tmp_path: Path) -> None:
    _write_corpus(tmp_path / "jsonl")
    engine = _engine(tmp_path)

    with pytest.raises(ValueError):
        engine.topic_matrix([])
    with pytest.raises(ValueError):
        engine.topic_matrix([""])
    with pytest.raises(ValueError):
        engine.topic_matrix(["x" * 201])
    with pytest.raises(ValueError):
        engine.topic_matrix([f"t{i}" for i in range(21)])
    with pytest.raises(ValueError):
        engine.topic_matrix(["bitcoin"], window_days=0)
    with pytest.raises(ValueError):
        engine.topic_matrix(["bitcoin"], window_days=400)


# ── quality snapshot ────────────────────────────────────────────────────────


def test_quality_snapshot_metrics(tmp_path: Path) -> None:
    _write_corpus(tmp_path / "jsonl")
    engine = _engine(tmp_path)

    snap = engine.quality_snapshot()

    assert snap.total_captures == 15
    assert snap.empty_text == 1  # doc 7 has empty text
    assert snap.duplicate_ratio == pytest.approx(5 / 15)  # H1 x3 + H2 x2 docs
    assert snap.near_duplicate_ratio == pytest.approx(3 / 15)  # non-root group members
    assert snap.avg_length == pytest.approx(_expected_avg_length())
    assert snap.languages == {"en": 10, "tr": 3, "unknown": 2}
    assert snap.top_domains == [
        DomainCount(domain="alpha.example", count=7),
        DomainCount(domain="beta.example", count=5),
        DomainCount(domain="gamma.example", count=3),
    ]
    assert snap.dedup_group_count == 2  # doc-1 (3 members) + doc-8 (2 members)
    # Span is exactly 3 days (BASE → BASE+3d): 15 / 3 = 5.
    assert snap.capture_rate_per_day == pytest.approx(5.0)


def test_quality_snapshot_respects_window(tmp_path: Path) -> None:
    _write_corpus(tmp_path / "jsonl")
    engine = _engine(tmp_path)

    snap = engine.quality_snapshot(window_days=1)

    # Window = last day: docs 6..15 (8 docs); doc 7 is the only empty text.
    assert snap.total_captures == 8
    assert snap.empty_text == 1
    # All duplicate hashes / groups predate the window.
    assert snap.duplicate_ratio == 0.0
    assert snap.near_duplicate_ratio == 0.0
    assert snap.dedup_group_count == 0
    assert snap.languages == {"en": 3, "tr": 3, "unknown": 2}
    assert snap.top_domains == [
        DomainCount(domain="beta.example", count=3),
        DomainCount(domain="gamma.example", count=3),
        DomainCount(domain="alpha.example", count=2),
    ]
    assert snap.capture_rate_per_day == pytest.approx(8.0)  # 8 docs / 1 day


def test_quality_snapshot_empty_corpus_is_zeroed(tmp_path: Path) -> None:
    engine = _engine(tmp_path)

    snap = engine.quality_snapshot()
    assert snap.total_captures == 0
    assert snap.empty_text == 0
    assert snap.duplicate_ratio == 0.0
    assert snap.near_duplicate_ratio == 0.0
    assert snap.avg_length == 0.0
    assert snap.languages == {}
    assert snap.top_domains == []
    assert snap.dedup_group_count == 0
    assert snap.capture_rate_per_day == 0.0

    windowed = engine.quality_snapshot(window_days=7)
    assert windowed.total_captures == 0


def test_quality_snapshot_validation(tmp_path: Path) -> None:
    _write_corpus(tmp_path / "jsonl")
    engine = _engine(tmp_path)

    with pytest.raises(ValueError):
        engine.quality_snapshot(window_days=0)
    with pytest.raises(ValueError):
        engine.quality_snapshot(window_days=400)
