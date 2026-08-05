"""Unit tests for the cross-view engine (topic lifecycle x X sentiment).

Builds a small news corpus through the JSONL-chunk pattern (like
``test_corpusx_engine.py``) and a temp X store via
:class:`~awareness.xscraper.store.SessionStore` (sessions created with
:func:`~awareness.xscraper.simulate.simulate_session` and, for the
deterministic sign/alignment cases, hand-crafted
:class:`~awareness.xscraper.models.TweetRecord` rows), then drives
:class:`~awareness.crossx.engine.CrossXEngine` against both.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from awareness.crossx.engine import CrossXEngine
from awareness.storage.duckdb_index import DuckDbIndex
from awareness.xscraper.models import SearchRequest, TweetRecord
from awareness.xscraper.simulate import simulate_session
from awareness.xscraper.store import SessionStore

_FULL_KEYS = (
    "doc_id", "capture_id", "parent_doc_or_dup_group", "source_type",
    "source_name", "source_locator", "source_shard",
    "source_offset_or_record_id", "discovery_channel", "job_id", "batch_id",
    "ingest_version", "url", "canonical_url", "domain", "fetch_ts",
    "observed_ts", "published_ts", "last_modified", "content_type",
    "http_status", "etag", "title", "text", "language", "content_hash",
    "near_dup_hash", "robots_decision", "terms_note_if_relevant",
)

NOW = datetime.now(tz=UTC)

_POSITIVE = ("bitcoin surges", "bitcoin rallies", "bitcoin soars")
_NEGATIVE = ("bitcoin crashes", "bitcoin slumps", "bitcoin panics")


def _write_doc(root: Path, idx: int, *, ts: datetime, text: str) -> None:
    day = root / "captures" / f"{ts:%Y}" / f"{ts:%m}" / f"{ts:%d}"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}",
        capture_id=f"cap-{idx:04d}",
        source_type="rss",
        domain="news.example",
        url=f"https://news.example/{idx}",
        fetch_ts=ts.isoformat(),
        observed_ts=ts.isoformat(),
        title=f"Headline {idx}",
        text=text,
        language="en",
    )
    (day / f"chunk-{idx:04d}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _news_corpus(root: Path, texts: list[str]) -> None:
    """Write one positive news doc per text on consecutive trailing days."""
    for idx, text in enumerate(texts):
        _write_doc(root, idx, ts=NOW - timedelta(days=len(texts) - 1 - idx), text=text)


def _news_docs(root: Path, texts: list[str], days: list[int]) -> None:
    """Write one news doc per text at the given trailing-day offsets."""
    for idx, text in enumerate(texts):
        _write_doc(root, idx, ts=NOW - timedelta(days=days[idx]), text=text)


def _index(tmp_path: Path) -> DuckDbIndex:
    return DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )


async def _create_store(tmp_path: Path) -> SessionStore:
    store = SessionStore(tmp_path / "xscraper.sqlite")
    await store.open()
    await store.init()
    return store


def _tweet(session_id: str, idx: int, day_offset: int, text: str) -> TweetRecord:
    created = NOW - timedelta(days=day_offset)
    return TweetRecord(
        tweet_id=f"t-{idx:04d}",
        session_id=session_id,
        author_id="author-1",
        username="alice",
        display_name="Alice",
        text=text,
        created_at=created,
        fetched_at=NOW,
        url=f"https://x.com/alice/status/t-{idx:04d}",
        source="simulated",
        query="bitcoin",
        lang="en",
        metrics={"likes": 1, "retweets": 0, "replies": 0},
        raw={},
    )


async def _session_with_tweets(
    store: SessionStore,
    texts: list[str],
) -> str:
    """Create a session and store one tweet per *text* on consecutive days."""
    request = SearchRequest(keywords=["bitcoin"], title="crafted")
    session = await store.create_session(request, "bitcoin")
    tweets = [
        _tweet(session.session_id, i, len(texts) - 1 - i, text)
        for i, text in enumerate(texts)
    ]
    await store.store_tweets(session.session_id, tweets)
    return session.session_id


async def _session_on_days(
    store: SessionStore,
    texts: list[str],
    days: list[int],
) -> str:
    """Create a session and store one tweet per *text* at the day offsets."""
    request = SearchRequest(keywords=["bitcoin"], title="crafted")
    session = await store.create_session(request, "bitcoin")
    tweets = [
        _tweet(session.session_id, i, days[i], text)
        for i, text in enumerate(texts)
    ]
    await store.store_tweets(session.session_id, tweets)
    return session.session_id


async def _simulated_session(store: SessionStore, seed: int = 4) -> str:
    request = SearchRequest(keywords=["bitcoin"], title="sim", lookback="14d")
    session = await store.create_session(request, "bitcoin")
    await simulate_session(store, session.session_id, n_tweets=100, seed=seed)
    return session.session_id


# ── aligned series ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_combined_view_aligns_and_zero_fills(tmp_path: Path) -> None:
    _news_corpus(tmp_path / "jsonl", _POSITIVE * 3)
    store = await _create_store(tmp_path)
    session_id = await _session_with_tweets(store, ["bitcoin surges", "bitcoin rallies"])
    await store.close()
    index = _index(tmp_path)
    engine = CrossXEngine(index, x_store_path=tmp_path / "xscraper.sqlite")

    view = await engine.combined_view("bitcoin", session_id, window_days=14)

    news = view.news_sentiment
    x = view.x_sentiment
    assert x is not None
    # Both series zero-filled onto the same calendar days (window_days+1).
    assert len(news) == len(x) == 15
    assert [p.ts for p in news] == [p.ts for p in x]
    # Zero-filled days carry 0.0 on both sides.
    assert news[0].avg_score == 0.0
    assert x[0].avg_score == 0.0
    # News docs land on the last 9 days (positive), crafted tweets on the
    # last 2 days (positive) — the shared days are aligned in value.
    assert news[14].avg_score > 0.0
    assert x[14].avg_score > 0.0
    assert x[13].avg_score > 0.0
    assert x[12].avg_score == 0.0
    index.close()


@pytest.mark.asyncio
async def test_correlation_is_one_for_perfectly_aligned_series(tmp_path: Path) -> None:
    days = [0, 2, 4, 6, 8, 10, 12, 14]
    _news_docs(tmp_path / "jsonl", [_POSITIVE[i % 3] for i in range(len(days))], days)
    store = await _create_store(tmp_path)
    # Identical day pattern with a matching sign: y = x exactly.
    session_id = await _session_on_days(
        store, [_POSITIVE[i % 3] for i in range(len(days))], days
    )
    await store.close()
    engine = CrossXEngine(_index(tmp_path), x_store_path=tmp_path / "xscraper.sqlite")

    view = await engine.combined_view("bitcoin", session_id, window_days=14)

    assert view.correlation_r == pytest.approx(1.0)
    assert view.news_avg_score > 0.0
    assert view.x_avg_score > 0.0
    assert view.convergence == "aligned bullish"
    assert view.news_phase in {
        "EMERGING", "EXPANDING", "PEAKING", "DECLINING", "DORMANT", "STABLE",
    }


@pytest.mark.asyncio
async def test_correlation_is_negative_for_opposite_signs(tmp_path: Path) -> None:
    days = [0, 2, 4, 6, 8, 10, 12, 14]
    _news_docs(tmp_path / "jsonl", [_POSITIVE[i % 3] for i in range(len(days))], days)
    store = await _create_store(tmp_path)
    session_id = await _session_on_days(
        store, [_NEGATIVE[i % 3] for i in range(len(days))], days
    )
    await store.close()
    engine = CrossXEngine(_index(tmp_path), x_store_path=tmp_path / "xscraper.sqlite")

    view = await engine.combined_view("bitcoin", session_id, window_days=14)

    # y = -x on the shared days → perfect negative correlation + divergence.
    assert view.correlation_r == pytest.approx(-1.0)
    assert view.news_avg_score > 0.0
    assert view.x_avg_score < 0.0
    assert view.convergence == "divergence"


# ── unknown / missing X side ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_session_yields_x_none_with_note(tmp_path: Path) -> None:
    _news_corpus(tmp_path / "jsonl", list(_POSITIVE * 3))
    store = await _create_store(tmp_path)
    await _session_with_tweets(store, ["bitcoin surges"])
    await store.close()
    engine = CrossXEngine(_index(tmp_path), x_store_path=tmp_path / "xscraper.sqlite")

    view = await engine.combined_view("bitcoin", "does-not-exist", window_days=14)

    assert view.x_sentiment is None
    assert view.x_avg_score == 0.0
    assert view.correlation_r == 0.0
    assert view.convergence == "neutral"
    assert "news side only" in view.note
    # News side still fully present.
    assert len(view.news_series) == 15
    assert view.news_phase in {
        "EMERGING", "EXPANDING", "PEAKING", "DECLINING", "DORMANT", "STABLE",
    }


@pytest.mark.asyncio
async def test_no_store_path_yields_x_none(tmp_path: Path) -> None:
    _news_corpus(tmp_path / "jsonl", list(_POSITIVE * 3))
    store = await _create_store(tmp_path)
    session_id = await _session_with_tweets(store, ["bitcoin surges"])
    await store.close()
    engine = CrossXEngine(_index(tmp_path), x_store_path=None)

    view = await engine.combined_view("bitcoin", session_id, window_days=14)

    assert view.x_sentiment is None
    assert view.note != ""
    assert view.news_phase != ""


# ── convergence rules ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_convergence_aligned_bullish(tmp_path: Path) -> None:
    _news_corpus(tmp_path / "jsonl", list(_POSITIVE * 5))
    store = await _create_store(tmp_path)
    session_id = await _session_with_tweets(store, list(_POSITIVE * 5))
    await store.close()
    engine = CrossXEngine(_index(tmp_path), x_store_path=tmp_path / "xscraper.sqlite")

    view = await engine.combined_view("bitcoin", session_id, window_days=14)

    assert view.news_avg_score > 0.0
    assert view.x_avg_score > 0.0
    assert view.convergence == "aligned bullish"


@pytest.mark.asyncio
async def test_convergence_aligned_bearish(tmp_path: Path) -> None:
    _news_corpus(tmp_path / "jsonl", list(_NEGATIVE * 5))
    store = await _create_store(tmp_path)
    session_id = await _session_with_tweets(store, list(_NEGATIVE * 5))
    await store.close()
    engine = CrossXEngine(_index(tmp_path), x_store_path=tmp_path / "xscraper.sqlite")

    view = await engine.combined_view("bitcoin", session_id, window_days=14)

    assert view.news_avg_score < 0.0
    assert view.x_avg_score < 0.0
    assert view.convergence == "aligned bearish"


@pytest.mark.asyncio
async def test_convergence_neutral_when_x_side_silent(tmp_path: Path) -> None:
    _news_corpus(tmp_path / "jsonl", list(_POSITIVE * 5))
    store = await _create_store(tmp_path)
    # A session with no tweets: the X side exists but carries no signal.
    request = SearchRequest(keywords=["bitcoin"], title="empty")
    session = await store.create_session(request, "bitcoin")
    await store.close()
    engine = CrossXEngine(_index(tmp_path), x_store_path=tmp_path / "xscraper.sqlite")

    view = await engine.combined_view("bitcoin", session.session_id, window_days=14)

    assert view.x_sentiment is not None
    assert view.x_avg_score == 0.0
    assert view.convergence == "neutral"


# ── simulated session (the scraper's own deterministic generator) ──────────


@pytest.mark.asyncio
async def test_combined_view_with_simulated_session(tmp_path: Path) -> None:
    _news_corpus(tmp_path / "jsonl", list(_POSITIVE * 5))
    store = await _create_store(tmp_path)
    session_id = await _simulated_session(store, seed=4)
    await store.close()
    engine = CrossXEngine(_index(tmp_path), x_store_path=tmp_path / "xscraper.sqlite")

    view = await engine.combined_view("bitcoin", session_id, window_days=14)

    assert view.x_sentiment is not None
    assert len(view.x_sentiment) == len(view.news_sentiment) == 15
    assert -1.0 <= view.correlation_r <= 1.0
    assert view.x_avg_score > 0.0
    assert view.convergence == "aligned bullish"
    assert view.note == ""


@pytest.mark.asyncio
async def test_x_session_sentiment_returns_none_for_unknown(tmp_path: Path) -> None:
    store = await _create_store(tmp_path)
    await _simulated_session(store, seed=4)
    await store.close()
    engine = CrossXEngine(_index(tmp_path), x_store_path=tmp_path / "xscraper.sqlite")

    assert await engine.x_session_sentiment("nope") is None

    # Per-day averages are rounded to 4 places and keyed by YYYY-MM-DD.
    store = await _create_store(tmp_path)
    session_id = await _session_with_tweets(store, ["bitcoin surges", "bitcoin crashes"])
    await store.close()
    engine = CrossXEngine(_index(tmp_path), x_store_path=tmp_path / "xscraper.sqlite")
    by_date = await engine.x_session_sentiment(session_id)
    assert by_date is not None
    assert len(by_date) == 2
    assert all(key.count("-") == 2 for key in by_date)
    assert all(isinstance(value, float) for value in by_date.values())


# ── validation / empty corpus ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_combined_view_validates_inputs(tmp_path: Path) -> None:
    _news_corpus(tmp_path / "jsonl", list(_POSITIVE * 2))
    engine = CrossXEngine(_index(tmp_path), x_store_path=None)

    with pytest.raises(ValueError):
        await engine.combined_view("   ", "s1", window_days=14)
    with pytest.raises(ValueError):
        await engine.combined_view("bitcoin", "  ", window_days=14)
    with pytest.raises(ValueError):
        await engine.combined_view("bitcoin", "s1", window_days=0)
    with pytest.raises(ValueError):
        await engine.combined_view("x" * 201, "s1", window_days=14)


@pytest.mark.asyncio
async def test_empty_corpus_returns_zeroed_view(tmp_path: Path) -> None:
    store = await _create_store(tmp_path)
    session_id = await _simulated_session(store, seed=4)
    await store.close()
    engine = CrossXEngine(_index(tmp_path), x_store_path=tmp_path / "xscraper.sqlite")

    view = await engine.combined_view("bitcoin", session_id, window_days=14)

    assert view.news_phase == "DORMANT"
    assert view.news_series == []
    assert view.news_sentiment == []
    assert view.news_avg_score == 0.0
    assert view.correlation_r == 0.0
    assert view.convergence == "neutral"
