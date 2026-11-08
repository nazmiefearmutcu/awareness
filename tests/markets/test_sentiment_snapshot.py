"""Market and social sentiment snapshot payloads built from the indicator store."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from awareness.markets.models import IndicatorPoint, ProviderRef
from awareness.markets.sentiment.indicators import (
    parse_apewisdom,
    parse_cboe_history_csv,
    parse_cnn_fear_greed,
    parse_crypto_fear_greed,
)
from awareness.markets.sentiment.snapshot import market_sentiment_snapshot, social_snapshot
from awareness.markets.store import MarketsStore

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
FixturePath = Callable[[str], Path]


def _seed(store: MarketsStore, fixture_path: FixturePath) -> None:
    """Parse the recorded fixtures with the real parsers and persist them."""
    store.upsert_indicator_points(parse_cnn_fear_greed(
        json.loads(fixture_path("fng_cnn.json").read_text(encoding="utf-8")),
        fetched_at=NOW, url="https://production.dataviz.cnn.io/index/fearandgreed/graphdata"))
    store.upsert_indicator_points(parse_crypto_fear_greed(
        json.loads(fixture_path("fng_crypto.json").read_text(encoding="utf-8")),
        fetched_at=NOW, url="https://api.alternative.me/fng/?limit=60"))
    for name, indicator in (("cboe_vix_history.csv", "vix"), ("cboe_vix9d_history.csv", "vix9d"),
                            ("cboe_vix3m_history.csv", "vix3m")):
        store.upsert_indicator_points(parse_cboe_history_csv(
            fixture_path(name).read_text(encoding="utf-8"), indicator=indicator, fetched_at=NOW,
            url=f"https://cdn.cboe.com/{name}"))
    store.upsert_indicator_points(parse_apewisdom(
        json.loads(fixture_path("apewisdom_all_stocks_p1.json").read_text(encoding="utf-8")),
        universe="stocks", fetched_at=NOW, url="https://apewisdom.io/api/v1.0/filter/all-stocks/page/1"))
    store.upsert_indicator_points(parse_apewisdom(
        json.loads(fixture_path("apewisdom_all_crypto_p1.json").read_text(encoding="utf-8")),
        universe="crypto", fetched_at=NOW, url="https://apewisdom.io/api/v1.0/filter/all-crypto/page/1"))


def _last_close(fixture_path: FixturePath, name: str) -> float:
    rows = list(csv.reader(io.StringIO(fixture_path(name).read_text(encoding="utf-8"))))[1:]
    return float(rows[-1][4])


def test_empty_store_returns_none_sections(store: MarketsStore) -> None:
    snap = market_sentiment_snapshot(store, now=NOW)
    assert snap["cnn"] is None
    assert snap["crypto"] is None
    assert snap["vix"] is None
    assert snap["social"] == {"stocks": [], "crypto": []}
    assert snap["sources"] == {"cnn": None, "alternative_me": None, "cboe": None, "apewisdom": None}


def test_social_snapshot_empty_store(store: MarketsStore) -> None:
    assert social_snapshot(store, universe="stocks") == []
    assert social_snapshot(store, universe="crypto", limit=10) == []


def test_populated_snapshot_cnn_section(store: MarketsStore, fixture_path: FixturePath) -> None:
    _seed(store, fixture_path)
    cnn = market_sentiment_snapshot(store, now=NOW)["cnn"]
    assert cnn is not None
    assert cnn["score"] == pytest.approx(22.0792756539235)
    assert cnn["rating"] == "extreme fear"
    assert cnn["as_of"] == "2026-11-06T23:59:54+00:00"
    assert cnn["previous_close"] == pytest.approx(19.8775510204082)
    assert cnn["previous_1_year"] == pytest.approx(24.342857142857145)
    assert len(cnn["history"]) == 30
    assert cnn["history"][0].keys() == {"ts", "value"}
    labels = {c["key"]: c["label"] for c in cnn["components"]}
    assert len(cnn["components"]) >= 5
    assert labels["market_momentum_sp500"] == "Market momentum (S&P 500)"
    assert labels["put_call_options"] == "Put/call options"
    sp500 = next(c for c in cnn["components"] if c["key"] == "market_momentum_sp500")
    assert sp500["score"] == 8.0
    assert sp500["rating"] == "extreme fear"
    assert sp500["as_of"] == "2026-11-06T21:37:09+00:00"
    assert all(0.0 <= c["score"] <= 100.0 for c in cnn["components"])


def test_populated_snapshot_crypto_section(store: MarketsStore, fixture_path: FixturePath) -> None:
    _seed(store, fixture_path)
    crypto = market_sentiment_snapshot(store, now=NOW)["crypto"]
    assert crypto is not None
    assert crypto["score"] == 28.0
    assert crypto["rating"] == "Fear"
    assert crypto["as_of"] == datetime.fromtimestamp(1794096000, UTC).isoformat()
    assert len(crypto["history"]) == 10


def test_vix_term_structure_from_fixture_closes(store: MarketsStore, fixture_path: FixturePath) -> None:
    _seed(store, fixture_path)
    vix = market_sentiment_snapshot(store, now=NOW)["vix"]
    assert vix is not None
    close_vix = _last_close(fixture_path, "cboe_vix_history.csv")
    close_vix3m = _last_close(fixture_path, "cboe_vix3m_history.csv")
    close_vix9d = _last_close(fixture_path, "cboe_vix9d_history.csv")
    assert vix["vix"] == pytest.approx(close_vix)
    assert vix["vix9d"] == pytest.approx(close_vix9d)
    assert vix["vix3m"] == pytest.approx(close_vix3m)
    assert vix["as_of"] == "2026-11-06T21:15:00+00:00"
    assert vix["term_ratio"] == round(close_vix / close_vix3m, 3)
    expected = "contango" if close_vix < close_vix3m else "backwardation"
    assert vix["structure"] == expected
    # 90-day window ending 2026-11-08 keeps only rows dated on/after 2026-08-10 (the fixture has 90 rows).
    rows = list(csv.reader(io.StringIO(fixture_path("cboe_vix_history.csv").read_text(encoding="utf-8"))))[1:]
    in_window = [r for r in rows if datetime.strptime(r[0], "%m/%d/%Y").date() >= datetime(2026, 8, 10).date()]
    assert len(vix["history"]) == len(in_window) == 65
    assert set(vix["history"][-1]) == {"ts", "vix", "vix3m"}
    assert vix["history"][-1]["vix"] == pytest.approx(close_vix)


def test_vix_structure_is_contango_when_vix_below_vix3m(store: MarketsStore) -> None:
    ts = datetime(2026, 11, 6, 21, 15, tzinfo=UTC)
    src = ProviderRef(provider="cboe", url="u", fetched_at=NOW)
    store.upsert_indicator_points([
        IndicatorPoint(indicator="vix", ts=ts, value=18.0, source=src),
        IndicatorPoint(indicator="vix3m", ts=ts, value=20.0, source=src),
    ])
    vix = market_sentiment_snapshot(store, now=NOW)["vix"]
    assert vix["structure"] == "contango"
    assert vix["term_ratio"] == 0.9
    assert vix["vix9d"] is None  # not provided -> None, not a default


def test_vix_structure_equal_counts_as_backwardation(store: MarketsStore) -> None:
    ts = datetime(2026, 11, 6, 21, 15, tzinfo=UTC)
    src = ProviderRef(provider="cboe", url="u", fetched_at=NOW)
    store.upsert_indicator_points([
        IndicatorPoint(indicator="vix", ts=ts, value=20.0, source=src),
        IndicatorPoint(indicator="vix3m", ts=ts, value=20.0, source=src),
    ])
    vix = market_sentiment_snapshot(store, now=NOW)["vix"]
    assert vix["structure"] == "backwardation"
    assert vix["term_ratio"] == 1.0


def test_vix_structure_backwardation_when_vix_above_vix3m(store: MarketsStore) -> None:
    ts = datetime(2026, 11, 6, 21, 15, tzinfo=UTC)
    src = ProviderRef(provider="cboe", url="u", fetched_at=NOW)
    store.upsert_indicator_points([
        IndicatorPoint(indicator="vix", ts=ts, value=30.0, source=src),
        IndicatorPoint(indicator="vix3m", ts=ts, value=25.0, source=src),
    ])
    vix = market_sentiment_snapshot(store, now=NOW)["vix"]
    assert vix["structure"] == "backwardation"
    assert vix["term_ratio"] == 1.2


def test_history_window_is_days_not_points(store: MarketsStore, fixture_path: FixturePath) -> None:
    _seed(store, fixture_path)
    snap = market_sentiment_snapshot(store, now=NOW, history_days=1)
    assert snap["cnn"]["history"] == []  # newest CNN reading is more than a day before NOW
    assert snap["cnn"]["score"] is not None  # the current reading is never windowed away
    later = market_sentiment_snapshot(store, now=datetime(2026, 11, 7, 0, 30, tzinfo=UTC), history_days=3)
    assert later["cnn"]["history"]
    assert all(datetime.fromisoformat(h["ts"]) >= datetime(2026, 11, 4, 0, 30, tzinfo=UTC)
               for h in later["cnn"]["history"])


def test_social_snapshot_stocks_sorted_and_changes(store: MarketsStore, fixture_path: FixturePath) -> None:
    _seed(store, fixture_path)
    rows = social_snapshot(store, universe="stocks", limit=15)
    assert len(rows) == 15
    assert [r["rank"] for r in rows] == sorted(r["rank"] for r in rows)
    top = rows[0]
    assert top["ticker"] == "NVDA"
    assert top["name"] == "NVIDIA"
    assert top["mentions"] == 91
    assert top["mentions_24h_ago"] == 234
    assert top["mention_change_pct"] == pytest.approx((91 - 234) / 234 * 100, abs=0.05)
    assert top["rank_change"] == 4 - 1  # rank_24h_ago - rank
    assert top["upvotes"] == 1808
    assert top["as_of"] == "2026-11-08T12:00:00+00:00"
    assert set(top) == {"ticker", "name", "rank", "mentions", "upvotes", "mentions_24h_ago",
                        "mention_change_pct", "rank_change", "as_of"}


def test_social_snapshot_crypto_strips_suffix(store: MarketsStore, fixture_path: FixturePath) -> None:
    _seed(store, fixture_path)
    rows = social_snapshot(store, universe="crypto", limit=10)
    assert rows[0]["ticker"] == "BTC"
    assert all(not r["ticker"].endswith(".X") for r in rows)
    assert len(rows) == 10


def test_social_snapshot_zero_or_missing_baseline_gives_none_change(store: MarketsStore) -> None:
    src = ProviderRef(provider="apewisdom", url="u", fetched_at=NOW)
    ts = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
    store.upsert_indicator_points([
        IndicatorPoint(indicator="social.stocks.mentions:AAA", ts=ts, value=10.0, source=src,
                       meta={"rank": 2, "name": "A", "upvotes": None, "rank_24h_ago": None,
                             "mentions_24h_ago": 0}),
        IndicatorPoint(indicator="social.stocks.mentions:BBB", ts=ts, value=4.0, source=src,
                       meta={"rank": 1, "name": "B", "upvotes": 3, "rank_24h_ago": 5,
                             "mentions_24h_ago": None}),
    ])
    rows = {r["ticker"]: r for r in social_snapshot(store, universe="stocks")}
    assert rows["AAA"]["mention_change_pct"] is None
    assert rows["AAA"]["rank_change"] is None
    assert rows["BBB"]["mention_change_pct"] is None
    assert rows["BBB"]["rank_change"] == 4


def test_social_snapshot_only_newest_timestamp(store: MarketsStore) -> None:
    src = ProviderRef(provider="apewisdom", url="u", fetched_at=NOW)
    old = NOW - timedelta(hours=3)
    store.upsert_indicator_points([
        IndicatorPoint(indicator="social.stocks.mentions:OLD", ts=old, value=50.0, source=src,
                       meta={"rank": 1, "name": "Old"}),
        IndicatorPoint(indicator="social.stocks.mentions:NEW", ts=NOW, value=5.0, source=src,
                       meta={"rank": 2, "name": "New"}),
    ])
    tickers = [r["ticker"] for r in social_snapshot(store, universe="stocks")]
    assert tickers == ["NEW"]


def test_partial_seed_leaves_other_sections_none(store: MarketsStore, fixture_path: FixturePath) -> None:
    store.upsert_indicator_points(parse_crypto_fear_greed(
        json.loads(fixture_path("fng_crypto.json").read_text(encoding="utf-8")),
        fetched_at=NOW, url="https://api.alternative.me/fng/?limit=60"))
    snap = market_sentiment_snapshot(store, now=NOW)
    assert snap["crypto"] is not None
    assert snap["cnn"] is None
    assert snap["vix"] is None
    assert snap["social"] == {"stocks": [], "crypto": []}
    assert snap["sources"]["alternative_me"] is not None
    assert snap["sources"]["cnn"] is None
