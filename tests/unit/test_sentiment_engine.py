"""Unit tests for the sentiment engine (lexicon scoring, time series, heat).

Builds small in-memory corpora through the same JSONL-chunk pattern as the
analytics unit tests and drives
:class:`~awareness.sentiment.engine.SentimentEngine` against them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from awareness.sentiment.engine import SentimentEngine
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


def _engine(tmp_path: Path) -> tuple[SentimentEngine, Path]:
    index = DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )
    return SentimentEngine(index), tmp_path / "jsonl"


def _bare() -> SentimentEngine:
    """Engine whose index is never touched (score_text uses only the lexicon)."""
    return SentimentEngine(None)  # type: ignore[arg-type]


# ── score_text ───────────────────────────────────────────────────────────────


def test_score_text_positive_doc_scores_above_zero() -> None:
    result = _bare().score_text(
        "Bitcoin surges to a record high as the rally gains momentum"
    )
    assert result["pos"] > 0
    assert result["neg"] == 0
    assert result["score"] > 0
    assert result["classified"] is True


def test_score_text_negative_doc_scores_below_zero() -> None:
    result = _bare().score_text(
        "stocks crash as panic spreads and losses deepen"
    )
    assert result["neg"] > 0
    assert result["pos"] == 0
    assert result["score"] < 0
    assert result["classified"] is True


def test_score_text_neutral_and_empty() -> None:
    result = _bare().score_text(
        "the meeting starts at noon on tuesday"
    )
    assert result == {
        "pos": 0.0,
        "neg": 0.0,
        "score": 0.0,
        "tokens_scanned": 7,
        "classified": False,
    }
    empty = _bare().score_text("")
    assert empty["score"] == 0.0
    assert empty["tokens_scanned"] == 0
    assert empty["classified"] is False


def test_score_text_negation_flips_polarity() -> None:
    flipped = _bare().score_text("not good")
    assert flipped["neg"] > 0
    assert flipped["pos"] == 0
    assert flipped["score"] < 0

    within_window = _bare().score_text(
        "the market did not rally today"
    )
    assert within_window["score"] < 0

    outside_window = _bare().score_text(
        "not a single mention of rally in the text today"
    )
    # "not" is 5 tokens before "rally" → beyond the 3-token window, no flip.
    assert outside_window["score"] > 0


def test_score_text_intensifier_adds_weight() -> None:
    plain = _bare().score_text("good")
    intense = _bare().score_text("very good")
    assert intense["pos"] == pytest.approx(1.5)
    assert intense["pos"] > plain["pos"]

    negated_intense = _bare().score_text("not very good")
    assert negated_intense["score"] < 0
    assert negated_intense["neg"] == pytest.approx(1.5)


# ── term_sentiment_over_time ─────────────────────────────────────────────────


def test_trend_buckets_daily_counts_and_scores(tmp_path: Path) -> None:
    engine, root = _engine(tmp_path)
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    _write_doc(root, 1, ts=base, title="Bitcoin rally continues", text="strong gains")
    _write_doc(root, 2, ts=base + timedelta(hours=3), title="Bitcoin crash", text="panic")
    _write_doc(root, 3, ts=base + timedelta(days=1), title="Bitcoin surges", text="record high")
    _write_doc(root, 4, ts=base + timedelta(days=1, hours=5), title="Sports", text="nothing")

    # Corpus tail ends 06-02 17:00; a 3-day window reaches back to 05-30 17:00.
    buckets = engine.term_sentiment_over_time("bitcoin", window_days=3)
    assert [b.ts for b in buckets] == [
        datetime(2026, 5, 30, tzinfo=UTC),
        datetime(2026, 5, 31, tzinfo=UTC),
        datetime(2026, 6, 1, tzinfo=UTC),
        datetime(2026, 6, 2, tzinfo=UTC),
    ]
    day1 = buckets[2]
    assert day1.doc_count == 2
    assert day1.pos_count == 1
    assert day1.neg_count == 1
    assert day1.avg_score == pytest.approx(0.0)  # +1.0 and -1.0 average out
    assert buckets[3].doc_count == 1
    assert buckets[3].pos_count == 1
    assert buckets[3].avg_score == pytest.approx(1.0)
    assert buckets[0].doc_count == 0  # zero-filled
    assert buckets[0].avg_score == 0.0


def test_trend_month_granularity_does_not_drift(tmp_path: Path) -> None:
    engine, root = _engine(tmp_path)
    june = datetime(2026, 6, 30, 9, 0, tzinfo=UTC)
    july = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    _write_doc(root, 1, ts=june, title="Bitcoin rally june", text="gains")
    _write_doc(root, 2, ts=july, title="Bitcoin crash july", text="panic")

    buckets = engine.term_sentiment_over_time("bitcoin", window_days=30, granularity="month")
    # Calendar-month buckets: exactly June 1 and July 1, no day+31 drift.
    assert [b.ts for b in buckets] == [
        datetime(2026, 6, 1, tzinfo=UTC),
        datetime(2026, 7, 1, tzinfo=UTC),
    ]
    assert buckets[0].doc_count == 1
    assert buckets[0].pos_count == 1
    assert buckets[1].doc_count == 1
    assert buckets[1].neg_count == 1


def test_trend_week_granularity_floors_to_monday(tmp_path: Path) -> None:
    engine, root = _engine(tmp_path)
    wednesday = datetime(2026, 6, 3, 9, 0, tzinfo=UTC)  # a Wednesday
    _write_doc(root, 1, ts=wednesday, title="Bitcoin rally", text="gains")

    buckets = engine.term_sentiment_over_time("bitcoin", window_days=7, granularity="week")
    # Window start floors to Monday 05-25; the doc buckets into Monday 06-01.
    assert [b.ts for b in buckets] == [
        datetime(2026, 5, 25, tzinfo=UTC),
        datetime(2026, 6, 1, tzinfo=UTC),
    ]
    assert buckets[1].doc_count == 1
    assert buckets[0].doc_count == 0


def test_trend_unknown_term_is_empty_and_zero_filled(tmp_path: Path) -> None:
    engine, root = _engine(tmp_path)
    base = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    _write_doc(root, 1, ts=base, title="Bitcoin rally", text="gains")

    buckets = engine.term_sentiment_over_time("dogecoin", window_days=3)
    assert len(buckets) == 4
    assert all(b.doc_count == 0 for b in buckets)
    assert all(b.avg_score == 0.0 for b in buckets)


# ── market_heat ──────────────────────────────────────────────────────────────


def test_market_heat_aggregates_and_trends(tmp_path: Path) -> None:
    engine, root = _engine(tmp_path)
    base = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    _write_doc(root, 1, ts=base, title="Bitcoin slump", text="losses")
    _write_doc(root, 2, ts=base + timedelta(days=1), title="Bitcoin crash", text="panic")
    _write_doc(root, 3, ts=base + timedelta(days=2), title="Bitcoin rally", text="gains")

    heat = engine.market_heat("bitcoin", window_days=2)
    assert heat["total_docs"] == 3
    assert heat["pos_docs"] == 1
    assert heat["neg_docs"] == 2
    assert heat["sentiment_ratio"] == pytest.approx(-1 / 3, abs=1e-3)
    assert heat["volatility"] > 0
    assert heat["last_7d_trend"] > 0  # scores rise over the last days


def test_market_heat_empty_corpus_is_zeroed(tmp_path: Path) -> None:
    engine, _ = _engine(tmp_path)
    assert engine.market_heat("bitcoin") == {
        "total_docs": 0,
        "pos_docs": 0,
        "neg_docs": 0,
        "sentiment_ratio": 0.0,
        "volatility": 0.0,
        "last_7d_trend": 0.0,
    }


# ── validation ───────────────────────────────────────────────────────────────


def test_bad_inputs_raise_value_error(tmp_path: Path) -> None:
    engine, root = _engine(tmp_path)
    base = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    _write_doc(root, 1, ts=base, title="Bitcoin", text="bitcoin")

    with pytest.raises(ValueError):
        engine.term_sentiment_over_time("")
    with pytest.raises(ValueError):
        engine.term_sentiment_over_time("x" * 201)
    with pytest.raises(ValueError):
        engine.term_sentiment_over_time("bitcoin", granularity="hourly")
    with pytest.raises(ValueError):
        engine.term_sentiment_over_time("bitcoin", window_days=0)
    with pytest.raises(ValueError):
        engine.term_sentiment_over_time("bitcoin", window_days=400)
    with pytest.raises(ValueError):
        engine.market_heat("")
    with pytest.raises(ValueError):
        engine.market_heat("bitcoin", window_days=-1)
