"""Unit tests for the analytics engine (term frequency, spikes, breakdowns).

Builds small in-memory corpora through the same JSONL-chunk pattern as the
rest of the unit suite (see ``test_duckdb_related.py``) and drives
:class:`~awareness.analytics.engine.TermFrequencyEngine` against them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from awareness.analytics.engine import TermFrequencyEngine
from awareness.analytics.models import LanguageCount
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
    language: str | None = None,
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
    )
    (day / f"chunk-{idx:04d}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _index(tmp_path: Path) -> DuckDbIndex:
    return DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )


def _engine(tmp_path: Path) -> tuple[TermFrequencyEngine, Path]:
    root = tmp_path / "jsonl"
    return TermFrequencyEngine(_index(tmp_path)), root


# ── term frequency ──────────────────────────────────────────────────────────


def test_term_frequency_day_counts_with_zero_fill(tmp_path: Path) -> None:
    engine, root = _engine(tmp_path)
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    _write_doc(root, 1, ts=base, title="Bitcoin hits record", text="market rally")
    _write_doc(root, 2, ts=base + timedelta(hours=3), title="bitcoin crash", text="dip")
    _write_doc(root, 3, ts=base + timedelta(days=1), title="Sports roundup", text="nothing here")
    _write_doc(root, 4, ts=base + timedelta(days=1, hours=5), title="Bitcoin analysis", text="deep dive")
    _write_doc(root, 5, ts=base + timedelta(days=2), title="Daily wrap", text="bitcoin market recap")

    buckets = engine.term_frequency_over_time(
        "bitcoin", start=base, end=base + timedelta(days=2)
    )
    assert [b.ts for b in buckets] == [
        datetime(2026, 6, 1, tzinfo=UTC),
        datetime(2026, 6, 2, tzinfo=UTC),
        datetime(2026, 6, 3, tzinfo=UTC),
    ]
    assert [b.count for b in buckets] == [2, 1, 1]


def test_term_frequency_default_window_uses_corpus_tail(tmp_path: Path) -> None:
    engine, root = _engine(tmp_path)
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    _write_doc(root, 1, ts=base, title="Bitcoin day one", text="x")
    _write_doc(root, 2, ts=base + timedelta(days=1), title="Bitcoin day two", text="x")
    _write_doc(root, 3, ts=base + timedelta(days=1, hours=2), title="unrelated", text="bitcoin mention")

    buckets = engine.term_frequency_over_time("bitcoin", window_days=2)
    # Window ends at the latest fetch_ts; 2 days back → 3 buckets (zero-filled).
    assert len(buckets) == 3
    assert sum(b.count for b in buckets) == 3


def test_term_matching_uses_word_boundaries(tmp_path: Path) -> None:
    engine, root = _engine(tmp_path)
    base = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    _write_doc(root, 1, ts=base, title="The cat sat", text="catalog of cats everywhere")
    _write_doc(root, 2, ts=base + timedelta(hours=1), title="Cathedral news", text="scattered cats")
    _write_doc(root, 3, ts=base + timedelta(hours=2), title="CAT", text="a cat")

    buckets = engine.term_frequency_over_time(
        "cat", start=base, end=base + timedelta(hours=3)
    )
    # Doc 3 counts (title "CAT" is an exact word); "catalog"/"Cathedral"/"cats" do not.
    assert [b.count for b in buckets] == [2]


def test_term_frequency_mode_restricts_fields(tmp_path: Path) -> None:
    engine, root = _engine(tmp_path)
    base = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    _write_doc(root, 1, ts=base, title="Bitcoin in title only", text="no crypto here")
    _write_doc(root, 2, ts=base + timedelta(hours=1), title="No match", text="bitcoin only in body")

    title_only = engine.term_frequency_over_time(
        "bitcoin", mode="title", start=base, end=base + timedelta(hours=3)
    )
    assert [b.count for b in title_only] == [1]


def test_granularity_week_and_month(tmp_path: Path) -> None:
    engine, root = _engine(tmp_path)
    monday = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)  # 2026-06-01 is a Monday
    _write_doc(root, 1, ts=monday, title="Bitcoin week one", text="x")
    _write_doc(root, 2, ts=monday + timedelta(days=2), title="Bitcoin midweek", text="x")
    _write_doc(root, 3, ts=monday + timedelta(days=28), title="Bitcoin monday later", text="x")
    _write_doc(root, 4, ts=monday + timedelta(days=33), title="Bitcoin july", text="x")

    weekly = engine.term_frequency_over_time(
        "bitcoin", granularity="week", start=monday, end=monday + timedelta(days=33)
    )
    assert weekly[0].ts == datetime(2026, 6, 1, tzinfo=UTC)
    assert weekly[-1].ts == datetime(2026, 6, 29, tzinfo=UTC)
    assert [b.count for b in weekly] == [2, 0, 0, 0, 2]

    monthly = engine.term_frequency_over_time(
        "bitcoin", granularity="month", start=monday, end=monday + timedelta(days=33)
    )
    assert [b.ts for b in monthly] == [
        datetime(2026, 6, 1, tzinfo=UTC),
        datetime(2026, 7, 1, tzinfo=UTC),
    ]
    assert [b.count for b in monthly] == [3, 1]


# ── spikes ──────────────────────────────────────────────────────────────────


def test_spikes_detects_outlier_day(tmp_path: Path) -> None:
    engine, root = _engine(tmp_path)
    base = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)
    for i in range(14):
        _write_doc(root, i + 1, ts=base + timedelta(days=i), title="Pump report", text="daily pump volume")
    spike_day = base + timedelta(days=14)
    for i in range(10):
        _write_doc(root, 100 + i, ts=spike_day + timedelta(hours=i), title="Pump alert", text="bitcoin pump")

    spikes = engine.detect_spikes("pump", window_days=14)
    assert len(spikes) == 1
    spike = spikes[0]
    assert spike.bucket == datetime(2026, 6, 15, tzinfo=UTC)
    assert spike.count == 10
    assert spike.zscore > 2.5
    assert spike.vs_mean > 0
    assert spike.mean == pytest.approx(24 / 15)
    assert spike.std > 0


def test_spikes_flat_series_has_no_spikes(tmp_path: Path) -> None:
    engine, root = _engine(tmp_path)
    base = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)
    for i in range(10):
        _write_doc(root, i + 1, ts=base + timedelta(days=i), title="steady", text="bitcoin steady volume")

    assert engine.detect_spikes("bitcoin", window_days=10) == []


def test_spikes_respects_min_absolute(tmp_path: Path) -> None:
    engine, root = _engine(tmp_path)
    base = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)
    for i in range(14):
        _write_doc(root, i + 1, ts=base + timedelta(days=i), title="Pump report", text="daily pump")
    spike_day = base + timedelta(days=14)
    for i in range(10):
        _write_doc(root, 100 + i, ts=spike_day + timedelta(hours=i), title="Pump alert", text="bitcoin pump")

    # A high min_absolute suppresses the otherwise-clear spike.
    assert engine.detect_spikes("pump", window_days=14, min_absolute=100) == []


# ── top terms / co-occurrence ───────────────────────────────────────────────


def test_top_terms_excludes_stopwords_and_sorts(tmp_path: Path) -> None:
    engine, root = _engine(tmp_path)
    base = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    texts = [
        "the quick brown fox jumps over the lazy dog",
        "the market bitcoin bitcoin rally",
        "bitcoin market rally bitcoin",
        "the the the market bitcoin",
    ]
    for i, text in enumerate(texts, start=1):
        _write_doc(root, i, ts=base + timedelta(hours=i), text=text)

    top = engine.top_terms(limit=5, min_count=2)
    assert [t.term for t in top] == ["bitcoin", "market", "rally"]
    assert [t.count for t in top] == [5, 3, 2]
    assert all(t.term != "the" for t in top)

    limited = engine.top_terms(limit=2, min_count=1)
    assert [t.term for t in limited] == ["bitcoin", "market"]


def test_entity_term_counts_cooccurrence(tmp_path: Path) -> None:
    engine, root = _engine(tmp_path)
    base = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    _write_doc(root, 1, ts=base, text="Bitcoin price surges above record high in market")
    _write_doc(root, 2, ts=base + timedelta(hours=1), text="Bitcoin adoption grows in global market")

    co = engine.entity_term_counts("bitcoin")
    assert co[0].term == "market"
    assert co[0].count == 2
    assert all(item.term != "bitcoin" for item in co)

    broad = engine.entity_term_counts("bitcoin", min_count=1)
    assert len(broad) == 8
    assert {item.term for item in broad} == {
        "adoption", "global", "grows", "high", "market", "price", "record", "surges",
    }


def test_entity_term_counts_unknown_term_is_empty(tmp_path: Path) -> None:
    engine, root = _engine(tmp_path)
    base = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    _write_doc(root, 1, ts=base, text="Bitcoin price surges")
    assert engine.entity_term_counts("dogecoin") == []


# ── breakdowns ──────────────────────────────────────────────────────────────


def test_domain_breakdown_orders_by_count(tmp_path: Path) -> None:
    engine, root = _engine(tmp_path)
    base = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    _write_doc(root, 1, ts=base, domain="news.example", text="a")
    _write_doc(root, 2, ts=base + timedelta(hours=1), domain="news.example", text="b")
    _write_doc(root, 3, ts=base + timedelta(hours=2), domain="news.example", text="c")
    _write_doc(root, 4, ts=base + timedelta(hours=3), domain="blog.example", text="d")

    doms = engine.domain_breakdown(limit=10)
    assert [d.domain for d in doms] == ["news.example", "blog.example"]
    assert [d.count for d in doms] == [3, 1]


def test_language_breakdown_rolls_up_and_keeps_none(tmp_path: Path) -> None:
    engine, root = _engine(tmp_path)
    base = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    _write_doc(root, 1, ts=base, language="en", text="a")
    _write_doc(root, 2, ts=base + timedelta(hours=1), language="en-US", text="b")
    _write_doc(root, 3, ts=base + timedelta(hours=2), language="tr", text="c")
    _write_doc(root, 4, ts=base + timedelta(hours=3), language=None, text="d")

    langs = engine.language_breakdown()
    assert langs[0] == LanguageCount(language="en", count=2)
    assert {lang.language: lang.count for lang in langs} == {"en": 2, "tr": 1, None: 1}


def test_breakdowns_respect_window(tmp_path: Path) -> None:
    engine, root = _engine(tmp_path)
    base = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    _write_doc(root, 1, ts=base, domain="inside.example", text="a")
    _write_doc(root, 2, ts=base + timedelta(days=30), domain="outside.example", text="b")

    doms = engine.domain_breakdown(start=base, end=base + timedelta(days=1))
    assert [d.domain for d in doms] == ["inside.example"]


# ── empty corpus & validation ───────────────────────────────────────────────


def test_empty_corpus_returns_empty_lists(tmp_path: Path) -> None:
    engine = TermFrequencyEngine(_index(tmp_path))
    assert engine.term_frequency_over_time("bitcoin") == []
    assert engine.top_terms() == []
    assert engine.detect_spikes("bitcoin") == []
    assert engine.domain_breakdown() == []
    assert engine.language_breakdown() == []
    assert engine.entity_term_counts("bitcoin") == []


def test_bad_inputs_raise_value_error(tmp_path: Path) -> None:
    engine, root = _engine(tmp_path)
    base = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    _write_doc(root, 1, ts=base, title="Bitcoin", text="bitcoin")

    with pytest.raises(ValueError):
        engine.term_frequency_over_time("")
    with pytest.raises(ValueError):
        engine.term_frequency_over_time("x" * 201)
    with pytest.raises(ValueError):
        engine.term_frequency_over_time("bitcoin", granularity="hourly")
    with pytest.raises(ValueError):
        engine.term_frequency_over_time("bitcoin", mode="abstract")
    with pytest.raises(ValueError):
        engine.term_frequency_over_time("bitcoin", window_days=0)
    with pytest.raises(ValueError):
        engine.term_frequency_over_time("bitcoin", window_days=400)
    with pytest.raises(ValueError):
        engine.term_frequency_over_time(
            "bitcoin", start=base + timedelta(days=2), end=base
        )
    with pytest.raises(ValueError):
        engine.detect_spikes("bitcoin", zscore_threshold=0)
    with pytest.raises(ValueError):
        engine.detect_spikes("bitcoin", window_days=-1)
    with pytest.raises(ValueError):
        engine.entity_term_counts("")
