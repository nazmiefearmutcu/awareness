"""Market-sentiment indicator parsers and collectors (CNN, crypto F&G, CBOE VIX, ApeWisdom, GDELT)."""

from __future__ import annotations

import asyncio
import csv
import io
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from awareness.markets.collectors.base import CollectorContext
from awareness.markets.config import MarketsSettings
from awareness.markets.http import Fetcher, FetchError
from awareness.markets.models import IndicatorPoint
from awareness.markets.sentiment.indicators import (
    ApeWisdomCollector,
    CboeVixCollector,
    CnnFearGreedCollector,
    CryptoFearGreedCollector,
    GdeltToneCollector,
    parse_apewisdom,
    parse_cboe_history_csv,
    parse_cnn_fear_greed,
    parse_crypto_fear_greed,
    parse_gdelt_timelinetone,
)
from awareness.markets.store import MarketsStore

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
FixturePath = Callable[[str], Path]


def _load(fixture_path: FixturePath, name: str) -> Any:
    return json.loads(fixture_path(name).read_text(encoding="utf-8"))


async def _no_sleep(_: float) -> None:
    return None


def _fetcher(settings: MarketsSettings, handler: Callable[[httpx.Request], httpx.Response]) -> Fetcher:
    return Fetcher(
        settings,
        None,
        transport=httpx.MockTransport(handler),
        sleep=_no_sleep,
        host_intervals={"production.dataviz.cnn.io": 0.0, "api.alternative.me": 0.0,
                        "cdn.cboe.com": 0.0, "apewisdom.io": 0.0, "api.gdeltproject.org": 0.0},
    )


def _ctx(settings: MarketsSettings, store: MarketsStore, http: Fetcher) -> CollectorContext:
    return CollectorContext(
        settings=settings,
        store=store,
        http=http,
        registry=None,  # type: ignore[arg-type]  # sentiment collectors never touch the publisher registry
        now=lambda: NOW,
    )


def _assert_utc(points: list[IndicatorPoint]) -> None:
    for p in points:
        assert p.ts.tzinfo is not None and p.ts.utcoffset().total_seconds() == 0


# ── CNN Fear & Greed ──────────────────────────────────────────────────────


def test_cnn_history_and_current_point_deduplicated(fixture_path: FixturePath) -> None:
    payload = _load(fixture_path, "fng_cnn.json")
    points = parse_cnn_fear_greed(payload, fetched_at=NOW, url="https://cnn.example/fng")
    main = [p for p in points if p.indicator == "cnn_fear_greed"]
    # 30 historical points; the current reading shares the last historical timestamp -> one point.
    assert len(main) == 30
    assert len({p.ts for p in main}) == 30
    _assert_utc(points)
    assert all(0.0 <= p.value <= 100.0 for p in main)
    current = max(main, key=lambda p: p.ts)
    assert current.ts == datetime(2026, 11, 6, 23, 59, 54, tzinfo=UTC)
    assert current.value == pytest.approx(22.0792756539235)
    assert current.label == "extreme fear"
    assert current.meta["previous_close"] == pytest.approx(19.8775510204082)
    assert set(current.meta) >= {"previous_close", "previous_1_week", "previous_1_month", "previous_1_year"}
    assert current.source.provider == "cnn"
    assert current.source.url == "https://cnn.example/fng"
    assert current.source.fetched_at == NOW
    # Only the current point carries the previous_* meta.
    assert all("previous_close" not in p.meta for p in main if p is not current)


def test_cnn_components_emitted_with_score_scale_for_current(fixture_path: FixturePath) -> None:
    payload = _load(fixture_path, "fng_cnn.json")
    points = parse_cnn_fear_greed(payload, fetched_at=NOW, url="https://cnn.example/fng")
    comp = [p for p in points if p.indicator.startswith("cnn_fg.")]
    keys = {p.indicator.removeprefix("cnn_fg.") for p in comp}
    assert keys == {
        "market_momentum_sp500", "market_momentum_sp125", "stock_price_strength", "stock_price_breadth",
        "put_call_options", "market_volatility_vix", "market_volatility_vix_50", "junk_bond_demand",
        "safe_haven_demand",
    }
    current = [p for p in comp if p.meta.get("scale") == "score"]
    assert len(current) == 9
    for p in current:
        assert 0.0 <= p.value <= 100.0
        assert "raw" in p.meta and p.label
    sp500 = next(p for p in current if p.indicator == "cnn_fg.market_momentum_sp500")
    assert sp500.value == 8.0
    assert sp500.meta["raw"] == pytest.approx(7376.86)
    assert sp500.label == "extreme fear"
    history = [p for p in comp if p.meta.get("scale") == "raw"]
    assert history  # native-unit history points are kept, flagged, never mixed with score points


def test_cnn_rejects_payload_without_current_reading() -> None:
    with pytest.raises((KeyError, TypeError, ValueError)):
        parse_cnn_fear_greed({"fear_and_greed_historical": {"data": []}}, fetched_at=NOW, url="u")


# ── Crypto Fear & Greed ───────────────────────────────────────────────────


def test_crypto_fear_greed_points(fixture_path: FixturePath) -> None:
    payload = _load(fixture_path, "fng_crypto.json")
    points = parse_crypto_fear_greed(payload, fetched_at=NOW, url="https://api.alternative.me/fng/?limit=60")
    assert len(points) == 10
    assert all(p.indicator == "crypto_fear_greed" for p in points)
    _assert_utc(points)
    assert all(0.0 <= p.value <= 100.0 for p in points)
    latest = max(points, key=lambda p: p.ts)
    assert latest.ts == datetime.fromtimestamp(1794096000, UTC)
    assert latest.value == 28.0
    assert latest.label == "Fear"
    assert latest.source.provider == "alternative_me"


def test_crypto_rejects_out_of_range_values() -> None:
    payload = {"data": [{"value": "140", "value_classification": "Bad", "timestamp": "1794096000"},
                        {"value": "55", "value_classification": "Neutral", "timestamp": "1794182400"}]}
    points = parse_crypto_fear_greed(payload, fetched_at=NOW, url="u")
    assert [p.value for p in points] == [55.0]


# ── CBOE VIX family ───────────────────────────────────────────────────────


def test_cboe_csv_last_rows_as_utc_closes(fixture_path: FixturePath) -> None:
    text = fixture_path("cboe_vix_history.csv").read_text(encoding="utf-8")
    points = parse_cboe_history_csv(text, indicator="vix", fetched_at=NOW,
                                    url="https://cdn.cboe.com/VIX_History.csv")
    assert len(points) == 90
    _assert_utc(points)
    assert all(p.indicator == "vix" and p.value > 0 for p in points)
    last = max(points, key=lambda p: p.ts)
    assert last.ts == datetime(2026, 11, 6, 21, 15, tzinfo=UTC)
    assert last.value == pytest.approx(25.64)
    assert last.meta == {"open": pytest.approx(27.68), "high": pytest.approx(27.76), "low": pytest.approx(23.47)}
    assert last.source.provider == "cboe"
    assert last.source.url == "https://cdn.cboe.com/VIX_History.csv"


def test_cboe_max_rows_keeps_most_recent(fixture_path: FixturePath) -> None:
    text = fixture_path("cboe_vix3m_history.csv").read_text(encoding="utf-8")
    points = parse_cboe_history_csv(text, indicator="vix3m", fetched_at=NOW, url="u", max_rows=5)
    assert len(points) == 5
    assert max(p.ts for p in points) == datetime(2026, 11, 6, 21, 15, tzinfo=UTC)
    rows = list(csv.reader(io.StringIO(text)))[1:]
    fifth_last = datetime.strptime(rows[-5][0], "%m/%d/%Y").replace(hour=21, minute=15, tzinfo=UTC)
    assert min(p.ts for p in points) == fifth_last


def test_cboe_skips_unparseable_rows() -> None:
    text = "DATE,OPEN,HIGH,LOW,CLOSE\n11/04/2026,1,2,0.5,1.5\nnot-a-date,1,2,0.5,1.5\n11/05/2026,1,2,0.5,abc\n11/06/2026,1,2,0.5,2.5\n"
    points = parse_cboe_history_csv(text, indicator="vix9d", fetched_at=NOW, url="u")
    assert [p.value for p in sorted(points, key=lambda p: p.ts)] == [1.5, 2.5]


def test_cboe_rejects_html_body() -> None:
    with pytest.raises(ValueError):
        parse_cboe_history_csv("<html><body>Access denied</body></html>", indicator="vix", fetched_at=NOW, url="u")


# ── ApeWisdom ─────────────────────────────────────────────────────────────


def test_apewisdom_stocks_points(fixture_path: FixturePath) -> None:
    payload = _load(fixture_path, "apewisdom_all_stocks_p1.json")
    fetched = datetime(2026, 11, 8, 12, 17, tzinfo=UTC)
    points = parse_apewisdom(payload, universe="stocks", fetched_at=fetched, url="https://apewisdom.io/x")
    assert len(points) == len(payload["results"])
    nvda = next(p for p in points if p.indicator == "social.stocks.mentions:NVDA")
    assert nvda.value == 91.0
    assert nvda.ts == datetime(2026, 11, 8, 12, 0, tzinfo=UTC)  # floored to 30 minutes
    assert nvda.meta == {"rank": 1, "name": "NVIDIA", "upvotes": 1808, "rank_24h_ago": 4, "mentions_24h_ago": 234}
    spy = next(p for p in points if p.indicator == "social.stocks.mentions:SPY")
    assert spy.meta["name"] == "SPDR S&P 500 ETF Trust"  # HTML entities decoded
    _assert_utc(points)
    assert all(p.source.provider == "apewisdom" for p in points)


def test_apewisdom_crypto_strips_suffix(fixture_path: FixturePath) -> None:
    payload = _load(fixture_path, "apewisdom_all_crypto_p1.json")
    points = parse_apewisdom(payload, universe="crypto", fetched_at=NOW, url="u")
    names = {p.indicator for p in points}
    assert "social.crypto.mentions:BTC" in names
    assert "social.crypto.mentions:ETH" in names
    assert not any(n.endswith(".X") for n in names)


def test_apewisdom_missing_numeric_fields_stay_none() -> None:
    payload = {"results": [{"rank": 3, "ticker": "XYZ", "name": "Xyz", "mentions": 5}]}
    (point,) = parse_apewisdom(payload, universe="stocks", fetched_at=NOW, url="u")
    assert point.meta["upvotes"] is None and point.meta["rank_24h_ago"] is None
    assert point.meta["mentions_24h_ago"] is None


# ── GDELT timelinetone ────────────────────────────────────────────────────


def test_gdelt_timeline_points(fixture_path: FixturePath) -> None:
    payload = _load(fixture_path, "gdelt_timelinetone_sample.json")
    points = parse_gdelt_timelinetone(payload, query_key="nvidia", fetched_at=NOW, url="https://gdelt/x")
    assert len(points) >= 6
    assert all(p.indicator == "gdelt.tone:nvidia" for p in points)
    _assert_utc(points)
    first = min(points, key=lambda p: p.ts)
    assert first.ts == datetime(2026, 11, 3, 2, 0, tzinfo=UTC)
    assert first.value == pytest.approx(-0.42)
    assert all(-100.0 <= p.value <= 100.0 for p in points)
    assert first.source.provider == "gdelt"


def test_gdelt_structure_without_timeline_is_error() -> None:
    with pytest.raises(ValueError):
        parse_gdelt_timelinetone({"error": "Timespan is too short"}, query_key="x", fetched_at=NOW, url="u")


# ── collectors ────────────────────────────────────────────────────────────


def test_collector_metadata() -> None:
    assert (CnnFearGreedCollector.name, CnnFearGreedCollector.interval_s) == ("sent.cnn_fng", 1800)
    assert (CryptoFearGreedCollector.name, CryptoFearGreedCollector.interval_s) == ("sent.crypto_fng", 3600)
    assert (CboeVixCollector.name, CboeVixCollector.interval_s) == ("sent.cboe_vix", 21600)
    assert (ApeWisdomCollector.name, ApeWisdomCollector.interval_s) == ("sent.apewisdom", 1800)
    assert (GdeltToneCollector.name, GdeltToneCollector.interval_s) == ("sent.gdelt_tone", 3600)
    for cls in (CnnFearGreedCollector, CryptoFearGreedCollector, CboeVixCollector, ApeWisdomCollector,
                GdeltToneCollector):
        assert cls.lock_group is None


def test_cnn_collector_upserts(fixture_path: FixturePath, markets_settings: MarketsSettings,
                               store: MarketsStore) -> None:
    body = fixture_path("fng_cnn.json").read_bytes()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    async def run() -> int:
        http = _fetcher(markets_settings, handler)
        try:
            result = await CnnFearGreedCollector().run(_ctx(markets_settings, store, http))
        finally:
            await http.aclose()
        return result.items

    count = asyncio.run(run())
    assert seen and seen[0].url.host == "production.dataviz.cnn.io"
    assert count > 30
    stored = store.query_indicator("cnn_fear_greed")
    assert len(stored) == 30
    assert stored[-1].source.provider == "cnn"
    assert store.latest_indicator("cnn_fg.market_momentum_sp500") is not None


def test_crypto_collector_upserts(fixture_path: FixturePath, markets_settings: MarketsSettings,
                                  store: MarketsStore) -> None:
    body = fixture_path("fng_crypto.json").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("limit") == "60"
        return httpx.Response(200, content=body)

    async def run() -> int:
        http = _fetcher(markets_settings, handler)
        try:
            return (await CryptoFearGreedCollector().run(_ctx(markets_settings, store, http))).items
        finally:
            await http.aclose()

    assert asyncio.run(run()) == 10
    assert len(store.query_indicator("crypto_fear_greed")) == 10


def test_cboe_collector_tolerates_one_failing_csv(fixture_path: FixturePath, markets_settings: MarketsSettings,
                                                  store: MarketsStore) -> None:
    bodies = {
        "VIX_History.csv": fixture_path("cboe_vix_history.csv").read_bytes(),
        "VIX9D_History.csv": fixture_path("cboe_vix9d_history.csv").read_bytes(),
        "VIX3M_History.csv": fixture_path("cboe_vix3m_history.csv").read_bytes(),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        if name == "VIX9D_History.csv":
            return httpx.Response(404, text="not found")
        return httpx.Response(200, content=bodies[name])

    async def run() -> Any:
        http = _fetcher(markets_settings, handler)
        try:
            return await CboeVixCollector().run(_ctx(markets_settings, store, http))
        finally:
            await http.aclose()

    result = asyncio.run(run())
    assert result.items == 180
    assert len(store.query_indicator("vix")) == 90
    assert len(store.query_indicator("vix3m")) == 90
    assert store.query_indicator("vix9d") == []
    assert any("VIX9D" in note for note in result.notes)


def test_cboe_collector_raises_when_all_fail(markets_settings: MarketsSettings, store: MarketsStore) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="nope")

    async def run() -> None:
        http = _fetcher(markets_settings, handler)
        try:
            await CboeVixCollector().run(_ctx(markets_settings, store, http))
        finally:
            await http.aclose()

    with pytest.raises(FetchError):
        asyncio.run(run())


def test_apewisdom_collector_upsert_count(fixture_path: FixturePath, markets_settings: MarketsSettings,
                                          store: MarketsStore) -> None:
    stocks = fixture_path("apewisdom_all_stocks_p1.json").read_bytes()
    crypto = fixture_path("apewisdom_all_crypto_p1.json").read_bytes()
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if "all-crypto" in request.url.path:
            return httpx.Response(200, content=crypto)
        if request.url.path.endswith("/page/2"):
            return httpx.Response(200, content=b'{"count": 0, "pages": 2, "current_page": 2, "results": []}')
        return httpx.Response(200, content=stocks)

    async def run() -> Any:
        http = _fetcher(markets_settings, handler)
        try:
            return await ApeWisdomCollector().run(_ctx(markets_settings, store, http))
        finally:
            await http.aclose()

    result = asyncio.run(run())
    assert any("all-stocks/page/1" in u for u in urls)
    assert any("all-stocks/page/2" in u for u in urls)
    assert any("all-crypto/page/1" in u for u in urls)
    stock_rows = json.loads(stocks)["results"]
    crypto_rows = json.loads(crypto)["results"]
    expected_stock = {r["ticker"] for r in stock_rows}
    expected_crypto = {r["ticker"].removesuffix(".X") for r in crypto_rows}
    assert result.items == len(stock_rows) + len(crypto_rows)
    stored = store.latest_by_prefix("social.stocks.mentions:")
    assert {p.indicator.split(":", 1)[1] for p in stored} == expected_stock
    stored_crypto = store.latest_by_prefix("social.crypto.mentions:")
    assert {p.indicator.split(":", 1)[1] for p in stored_crypto} == expected_crypto


def _gdelt_body(query: str) -> bytes:
    return json.dumps({
        "query_details": {"title": query, "date_resolution": "hour"},
        "timeline": [{"series": "Average Tone", "data": [
            {"date": "20261103T020000Z", "value": 0.1}, {"date": "20261103T030000Z", "value": -0.2}]}],
    }).encode()


QUERY_TEXT: dict[str, str] = {
    "AAPL": '"Apple"', "MSFT": '"Microsoft"', "NVDA": '"Nvidia"', "AMZN": '"Amazon"', "GOOGL": '"Google"',
    "META": '"Meta Platforms"', "TSLA": '"Tesla"', "BTC": '"Bitcoin"', "ETH": '"Ethereum"', "SOL": '"Solana"',
    "SPY": '"stock market"', "QQQ": '"Nasdaq"', "federal_reserve": '"Federal Reserve"',
    "turkey_economy": '"Turkey economy"',
}


def test_gdelt_collector_rotates_cursor_and_treats_html_as_failure(
    markets_settings: MarketsSettings, store: MarketsStore
) -> None:
    store.set_watchlist(["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "BTC", "ETH", "SOL", "SPY", "QQQ"])
    queried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        queried.append(query)
        if query == QUERY_TEXT["AAPL"]:
            return httpx.Response(200, text="<html>Please retry later</html>", headers={"content-type": "text/html"})
        return httpx.Response(200, content=_gdelt_body(query))

    async def run() -> Any:
        http = _fetcher(markets_settings, handler)
        try:
            return await GdeltToneCollector().run(_ctx(markets_settings, store, http))
        finally:
            await http.aclose()

    def keys_for(texts: list[str]) -> set[str]:
        return {key for key, text in QUERY_TEXT.items() if text in texts}

    result = asyncio.run(run())
    first_window = keys_for(queried)
    assert len(queried) == 6
    assert "AAPL" in first_window  # watchlist order is alphabetical here, so AAPL is in the first window
    assert store.kv_get("gdelt.tone.cursor") == 6
    assert store.latest_indicator("gdelt.tone:AAPL") is None  # HTML body is never stored
    stored_first = {p.indicator for p in store.latest_by_prefix("gdelt.tone:")}
    assert stored_first == {f"gdelt.tone:{k}" for k in first_window - {"AAPL"}}
    assert any("AAPL" in note for note in result.notes)

    queried.clear()
    asyncio.run(run())
    second_window = keys_for(queried)
    assert len(queried) == 6
    assert store.kv_get("gdelt.tone.cursor") == 12
    assert not second_window & first_window  # the next window covers different queries


def test_gdelt_collector_raises_when_every_query_fails(markets_settings: MarketsSettings,
                                                       store: MarketsStore) -> None:
    store.set_watchlist(["AAPL"])

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="plain text error")

    async def run() -> None:
        http = _fetcher(markets_settings, handler)
        try:
            await GdeltToneCollector().run(_ctx(markets_settings, store, http))
        finally:
            await http.aclose()

    with pytest.raises(FetchError):
        asyncio.run(run())
    assert store.kv_get("gdelt.tone.cursor") is not None  # rotation still advances on failure
