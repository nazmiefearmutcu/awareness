"""Market-sentiment indicators: parsers and collectors.

Series stored as :class:`IndicatorPoint` rows (see ``store.upsert_indicator_points``):

* ``cnn_fear_greed`` - CNN Fear & Greed index (0-100), history + current reading.
* ``cnn_fg.<component>`` - CNN component readings. The *current* point carries the
  0-100 ``score`` as value with ``meta["scale"] == "score"`` and the component's native
  input under ``meta["raw"]``. CNN publishes only native inputs (e.g. the S&P 500 level,
  the VIX level) for history, so history points carry ``y`` as value with
  ``meta["scale"] == "raw"``. Consumers that want a 0-100 series must filter on
  ``scale == "score"``; the snapshot does.
* ``crypto_fear_greed`` - alternative.me crypto Fear & Greed (0-100).
* ``vix`` / ``vix9d`` / ``vix3m`` - CBOE closes. Each daily row is stamped at 16:15 New York
  local time on its date, converted to UTC (21:15 UTC in EST, 20:15 UTC in EDT).
* ``social.<universe>.mentions:<TICKER>`` - ApeWisdom mention counts, stamped at
  ``fetched_at`` floored to 30 minutes. Crypto ``.X`` suffixes are stripped.
* ``gdelt.tone:<query_key>`` - GDELT average tone over the last 7 days, hourly.

Every parser raises (or returns no rows for a valid but empty payload) rather than
inventing values; unparseable individual rows are skipped and their fields stay absent.
"""

from __future__ import annotations

import csv
import html
import io
import logging
import math
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, time
from typing import Any, Literal
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from awareness.markets.collectors.base import CollectorContext, CollectorResult
from awareness.markets.http import FetchError, FetchResult
from awareness.markets.models import IndicatorPoint, ProviderRef, ensure_utc

log = logging.getLogger(__name__)

CNN_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
CRYPTO_URL = "https://api.alternative.me/fng/?limit=60"
CBOE_URL_TEMPLATE = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{name}_History.csv"
CBOE_SERIES: tuple[tuple[str, str], ...] = (("VIX", "vix"), ("VIX9D", "vix9d"), ("VIX3M", "vix3m"))
APEWISDOM_URLS: tuple[tuple[Literal["stocks", "crypto"], str], ...] = (
    ("stocks", "https://apewisdom.io/api/v1.0/filter/all-stocks/page/1"),
    ("stocks", "https://apewisdom.io/api/v1.0/filter/all-stocks/page/2"),
    ("crypto", "https://apewisdom.io/api/v1.0/filter/all-crypto/page/1"),
)
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_CURSOR_KEY = "gdelt.tone.cursor"
GDELT_BATCH = 6

CBOE_CLOSE_LOCAL = time(16, 15)  # CBOE closes at 16:15 New York local time (DST-aware)
CBOE_CLOSE_TZ = ZoneInfo("America/New_York")
CBOE_HEADER = ["DATE", "OPEN", "HIGH", "LOW", "CLOSE"]

CNN_COMPONENT_LABELS: dict[str, str] = {
    "market_momentum_sp500": "Market momentum (S&P 500)",
    "stock_price_strength": "Stock price strength",
    "stock_price_breadth": "Stock price breadth",
    "put_call_options": "Put/call options",
    "market_volatility_vix": "Market volatility (VIX)",
    "junk_bond_demand": "Junk bond demand",
    "safe_haven_demand": "Safe haven demand",
}
# CNN publishes two moving-average series beside its seven components. They are not components:
# each one is kept only as meta (moving_average_score / _rating) of its parent component.
CNN_MOVING_AVERAGE_PARENTS: dict[str, str] = {
    "market_momentum_sp125": "market_momentum_sp500",
    "market_volatility_vix_50": "market_volatility_vix",
}
CNN_PREVIOUS_KEYS = ("previous_close", "previous_1_week", "previous_1_month", "previous_1_year")

# Ticker -> quoted GDELT query text. Tickers without an entry are not queried (no guessing).
GDELT_QUERY_NAMES: dict[str, str] = {
    "AAPL": '"Apple"',
    "MSFT": '"Microsoft"',
    "NVDA": '"Nvidia"',
    "AMZN": '"Amazon"',
    "GOOGL": '"Google"',
    "META": '"Meta Platforms"',
    "TSLA": '"Tesla"',
    "BTC": '"Bitcoin"',
    "ETH": '"Ethereum"',
    "SOL": '"Solana"',
    "SPY": '"stock market"',
    "QQQ": '"Nasdaq"',
}
GDELT_FIXED_QUERIES: tuple[tuple[str, str], ...] = (
    ("federal_reserve", '"Federal Reserve"'),
    ("turkey_economy", '"Turkey economy"'),
)


# ── value helpers ─────────────────────────────────────────────────────────


def _float_or_none(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _score_or_none(value: object) -> float | None:
    number = _float_or_none(value)
    if number is None or not 0.0 <= number <= 100.0:
        return None
    return number


def _int_or_none(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError, OverflowError):
        return None


def _epoch_ms(value: object) -> datetime:
    return datetime.fromtimestamp(float(value) / 1000.0, UTC)  # type: ignore[arg-type]


def _any_timestamp(value: object) -> datetime:
    """Accept epoch milliseconds (number or numeric string) or an ISO-8601 string."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _epoch_ms(value)
    text = str(value).strip()
    try:
        return _epoch_ms(float(text))
    except ValueError:
        return ensure_utc(datetime.fromisoformat(text))  # type: ignore[return-value]


def _floor_to_30_minutes(value: datetime) -> datetime:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    utc = aware.astimezone(UTC)
    return utc.replace(minute=utc.minute - utc.minute % 30, second=0, microsecond=0)


def _row_epoch(row: Mapping[str, Any]) -> datetime | None:
    """Epoch-millisecond ``x`` of a CNN row, or ``None`` when missing or non-numeric."""
    try:
        return _epoch_ms(row["x"])
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None


def _cboe_stamp(day: date) -> datetime:
    return datetime.combine(day, CBOE_CLOSE_LOCAL, CBOE_CLOSE_TZ).astimezone(UTC)


def _dedupe(points: Iterable[IndicatorPoint]) -> list[IndicatorPoint]:
    """Keep one point per (indicator, ts); the last one written wins."""
    merged: dict[tuple[str, datetime], IndicatorPoint] = {}
    for point in points:
        merged[(point.indicator, point.ts)] = point
    return list(merged.values())


# ── CNN Fear & Greed ──────────────────────────────────────────────────────


def _moving_average_meta(payload: Mapping[str, Any], parent: str) -> dict[str, Any]:
    """Current score and rating of the moving-average series whose parent is ``parent`` (if any)."""
    for ma_key, ma_parent in CNN_MOVING_AVERAGE_PARENTS.items():
        block = payload.get(ma_key)
        if ma_parent != parent or not isinstance(block, Mapping):
            continue
        score = _score_or_none(block.get("score"))
        if score is None:
            return {}
        return {"moving_average_score": score, "moving_average_rating": block.get("rating")}
    return {}


def parse_cnn_fear_greed(payload: dict, *, fetched_at: datetime, url: str) -> list[IndicatorPoint]:
    """Parse the CNN ``graphdata`` payload into index and component points.

    Raises ``KeyError``/``ValueError`` when the current reading is missing or out of range.
    See the module docstring for the component ``scale`` convention.
    """
    source = ProviderRef(provider="cnn", url=url, fetched_at=fetched_at)
    current = payload["fear_and_greed"]
    current_ts = _any_timestamp(current["timestamp"])
    current_score = _score_or_none(current["score"])
    if current_score is None:
        raise ValueError("CNN fear_and_greed.score missing or outside 0-100")
    current_meta = {key: _float_or_none(current.get(key)) for key in CNN_PREVIOUS_KEYS}

    points: list[IndicatorPoint] = []
    historical = payload.get("fear_and_greed_historical") or {}
    for row in historical.get("data") or []:
        ts = _row_epoch(row)
        value = _score_or_none(row.get("y"))
        if ts is None or value is None or ts == current_ts:
            continue
        points.append(IndicatorPoint(indicator="cnn_fear_greed", ts=ts, value=value,
                                     label=row.get("rating"), source=source))
    points.append(IndicatorPoint(indicator="cnn_fear_greed", ts=current_ts, value=current_score,
                                 label=current.get("rating"), meta=current_meta, source=source))

    for key in CNN_COMPONENT_LABELS:
        block = payload.get(key)
        if not isinstance(block, Mapping):
            continue
        indicator = f"cnn_fg.{key}"
        rows = [r for r in (block.get("data") or []) if isinstance(r, Mapping)]
        timed = [(_row_epoch(r), r) for r in rows]
        try:
            block_ts = _any_timestamp(block["timestamp"])
        except (KeyError, TypeError, ValueError):
            block_ts = None
        block_score = _score_or_none(block.get("score"))
        if block_ts is not None and block_score is not None:
            raw_now = next((_float_or_none(r.get("y")) for ts, r in timed if ts == block_ts), None)
            meta: dict[str, Any] = {"scale": "score", "raw": raw_now}
            meta.update(_moving_average_meta(payload, key))
            points.append(IndicatorPoint(indicator=indicator, ts=block_ts, value=block_score,
                                         label=block.get("rating"), meta=meta, source=source))
        for ts, row in timed:
            raw = _float_or_none(row.get("y"))
            if ts is None or raw is None or ts == block_ts:
                continue
            points.append(IndicatorPoint(indicator=indicator, ts=ts, value=raw, label=row.get("rating"),
                                         meta={"scale": "raw", "raw": raw}, source=source))
    return _dedupe(points)


# ── crypto Fear & Greed ───────────────────────────────────────────────────


def parse_crypto_fear_greed(payload: dict, *, fetched_at: datetime, url: str) -> list[IndicatorPoint]:
    """Parse alternative.me ``/fng/`` rows; out-of-range or undated rows are skipped."""
    source = ProviderRef(provider="alternative_me", url=url, fetched_at=fetched_at)
    points: list[IndicatorPoint] = []
    for row in payload["data"]:
        value = _score_or_none(row.get("value"))
        try:
            ts = datetime.fromtimestamp(int(float(row["timestamp"])), UTC)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            continue
        if value is None:
            continue
        points.append(IndicatorPoint(indicator="crypto_fear_greed", ts=ts, value=value,
                                     label=row.get("value_classification"), source=source))
    return _dedupe(points)


# ── CBOE VIX family ───────────────────────────────────────────────────────


def parse_cboe_history_csv(
    text: str, *, indicator: str, fetched_at: datetime, url: str, max_rows: int = 400
) -> list[IndicatorPoint]:
    """Parse a CBOE ``*_History.csv`` (``DATE,OPEN,HIGH,LOW,CLOSE``, MM/DD/YYYY).

    Keeps the most recent ``max_rows`` usable rows. Rows with an unparseable date or a
    non-positive close are skipped. A body without the expected header raises ``ValueError``
    (an HTML error page, for example).
    """
    source = ProviderRef(provider="cboe", url=url, fetched_at=fetched_at)
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")))
    header = next(reader, None)
    if not header or [h.strip().upper() for h in header[:5]] != CBOE_HEADER:
        raise ValueError(f"unexpected CBOE CSV header for {indicator}")
    rows: list[tuple[date, float | None, float | None, float | None, float]] = []
    for record in reader:
        if len(record) < 5:
            continue
        try:
            day = datetime.strptime(record[0].strip(), "%m/%d/%Y").date()
        except ValueError:
            continue
        close = _float_or_none(record[4])
        if close is None or close <= 0:
            continue
        rows.append((day, _float_or_none(record[1]), _float_or_none(record[2]), _float_or_none(record[3]), close))
    points = [
        IndicatorPoint(indicator=indicator, ts=_cboe_stamp(day), value=close,
                       meta={"open": o, "high": h, "low": lo}, source=source)
        for day, o, h, lo, close in (rows[-max_rows:] if max_rows > 0 else [])
    ]
    return _dedupe(points)


# ── ApeWisdom ─────────────────────────────────────────────────────────────


def parse_apewisdom(
    payload: dict,
    *,
    universe: Literal["stocks", "crypto"],
    fetched_at: datetime,
    url: str,
) -> list[IndicatorPoint]:
    """Parse an ApeWisdom ``filter`` page into ``social.<universe>.mentions:<TICKER>`` points.

    The first occurrence of a ticker on the page wins (ApeWisdom orders by rank).
    """
    source = ProviderRef(provider="apewisdom", url=url, fetched_at=fetched_at)
    stamp = _floor_to_30_minutes(fetched_at)
    prefix = f"social.{universe}.mentions:"
    seen: dict[str, IndicatorPoint] = {}
    for row in payload["results"]:
        ticker = str(row.get("ticker") or "").strip().upper()
        if universe == "crypto":
            ticker = ticker.removesuffix(".X")
        mentions = _float_or_none(row.get("mentions"))
        if not ticker or mentions is None or ticker in seen:
            continue
        name = row.get("name")
        seen[ticker] = IndicatorPoint(
            indicator=f"{prefix}{ticker}",
            ts=stamp,
            value=mentions,
            meta={
                "rank": _int_or_none(row.get("rank")),
                "name": html.unescape(str(name)) if name else None,
                "upvotes": _int_or_none(row.get("upvotes")),
                "rank_24h_ago": _int_or_none(row.get("rank_24h_ago")),
                "mentions_24h_ago": _int_or_none(row.get("mentions_24h_ago")),
            },
            source=source,
        )
    return list(seen.values())


# ── GDELT timelinetone ────────────────────────────────────────────────────


def parse_gdelt_timelinetone(payload: dict, *, query_key: str, fetched_at: datetime, url: str) -> list[IndicatorPoint]:
    """Parse a GDELT ``mode=timelinetone`` payload into ``gdelt.tone:<query_key>`` points.

    A payload without a ``timeline`` list raises ``ValueError`` (GDELT error messages arrive
    as JSON objects such as ``{"error": ...}``). An empty timeline is a valid, empty result.
    """
    timeline = payload.get("timeline")
    if not isinstance(timeline, list):
        raise ValueError("GDELT payload has no timeline")
    source = ProviderRef(provider="gdelt", url=url, fetched_at=fetched_at)
    points: list[IndicatorPoint] = []
    for series in timeline:
        if not isinstance(series, Mapping):
            raise ValueError("GDELT timeline entry is not an object")
        data = series.get("data") or []
        if not isinstance(data, list):
            raise ValueError("GDELT timeline data is not a list")
        for row in data:
            if not isinstance(row, Mapping):
                continue
            value = _float_or_none(row.get("value"))
            try:
                ts = datetime.strptime(str(row["date"]), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            except (KeyError, TypeError, ValueError):
                continue
            if value is None:
                continue
            points.append(IndicatorPoint(indicator=f"gdelt.tone:{query_key}", ts=ts, value=value,
                                         meta={"series": series.get("series")}, source=source))
    return _dedupe(points)


# ── collectors ────────────────────────────────────────────────────────────


def _json_body(res: FetchResult, url: str) -> Any:
    """Decode a JSON body; a non-JSON 200 response is a provider failure."""
    try:
        return res.json()
    except ValueError as exc:
        raise FetchError(url, res.status, f"non-JSON body: {res.text[:120]!r}") from exc


class CnnFearGreedCollector:
    """CNN Fear & Greed index with its component readings."""

    name = "sent.cnn_fng"
    interval_s = 1800
    lock_group: str | None = None

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        res = await ctx.http.get(CNN_URL, profile="browser")
        points = parse_cnn_fear_greed(_json_body(res, CNN_URL), fetched_at=res.fetched_at, url=CNN_URL)
        return CollectorResult(items=ctx.store.upsert_indicator_points(points))


class CryptoFearGreedCollector:
    """alternative.me crypto Fear & Greed index."""

    name = "sent.crypto_fng"
    interval_s = 3600
    lock_group: str | None = None

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        res = await ctx.http.get(CRYPTO_URL, profile="bot")
        points = parse_crypto_fear_greed(_json_body(res, CRYPTO_URL), fetched_at=res.fetched_at, url=CRYPTO_URL)
        return CollectorResult(items=ctx.store.upsert_indicator_points(points))


class CboeVixCollector:
    """CBOE VIX, VIX9D and VIX3M daily history. One failing series is tolerated."""

    name = "sent.cboe_vix"
    interval_s = 21600
    lock_group: str | None = None

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        points: list[IndicatorPoint] = []
        notes: list[str] = []
        for series_name, indicator in CBOE_SERIES:
            url = CBOE_URL_TEMPLATE.format(name=series_name)
            try:
                res = await ctx.http.get(url, profile="browser")
                parsed = parse_cboe_history_csv(res.text, indicator=indicator, fetched_at=res.fetched_at, url=url)
                if not parsed:
                    raise ValueError("no usable rows")
            except (FetchError, ValueError) as exc:
                notes.append(f"{series_name}: {exc}")
                log.warning("CBOE %s unavailable: %s", series_name, exc)
                continue
            points.extend(parsed)
        if not points:
            raise FetchError(CBOE_URL_TEMPLATE.format(name="VIX"), None,
                             "all CBOE series failed: " + "; ".join(notes))
        return CollectorResult(items=ctx.store.upsert_indicator_points(points), notes=notes)


class ApeWisdomCollector:
    """ApeWisdom mention rankings for stocks (two pages) and crypto (one page)."""

    name = "sent.apewisdom"
    interval_s = 1800
    lock_group: str | None = None

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        responses = [(universe, url, await ctx.http.get(url, profile="bot")) for universe, url in APEWISDOM_URLS]
        points: list[IndicatorPoint] = []
        seen: set[str] = set()  # first occurrence across pages wins (page 1 outranks later pages)
        for universe, url, res in responses:
            for point in parse_apewisdom(_json_body(res, url), universe=universe,
                                         fetched_at=res.fetched_at, url=url):
                if point.indicator in seen:
                    continue
                seen.add(point.indicator)
                points.append(point)
        return CollectorResult(items=ctx.store.upsert_indicator_points(points))


def build_gdelt_queries(tickers: Iterable[str]) -> list[tuple[str, str]]:
    """``(query_key, quoted query text)`` pairs: mapped watchlist tickers, then fixed topics."""
    queries: list[tuple[str, str]] = []
    for ticker in tickers:
        symbol = str(ticker).strip().upper()
        text = GDELT_QUERY_NAMES.get(symbol)
        if text is not None and symbol not in {k for k, _ in queries}:
            queries.append((symbol, text))
    queries.extend(GDELT_FIXED_QUERIES)
    return queries


class GdeltToneCollector:
    """GDELT average tone, rotating through the queries six at a time via a kv cursor."""

    name = "sent.gdelt_tone"
    interval_s = 3600
    lock_group: str | None = None

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        tickers = ctx.store.get_watchlist() or list(ctx.settings.watchlist)
        queries = build_gdelt_queries(tickers)
        total = len(queries)
        start = int(ctx.store.kv_get(GDELT_CURSOR_KEY) or 0) % total
        batch = [queries[(start + offset) % total] for offset in range(min(GDELT_BATCH, total))]
        ctx.store.kv_set(GDELT_CURSOR_KEY, (start + len(batch)) % total)

        points: list[IndicatorPoint] = []
        notes: list[str] = []
        for query_key, text in batch:
            params = {"query": text, "mode": "timelinetone", "timespan": "7d", "format": "json"}
            full_url = f"{GDELT_URL}?{urlencode(params)}"
            try:
                res = await ctx.http.get(GDELT_URL, profile="bot", params=params)
                parsed = parse_gdelt_timelinetone(_json_body(res, full_url), query_key=query_key,
                                                  fetched_at=res.fetched_at, url=full_url)
            except (FetchError, ValueError, KeyError, TypeError, AttributeError) as exc:
                notes.append(f"{query_key}: {exc}")
                continue
            points.extend(parsed)
        if len(notes) == len(batch):
            raise FetchError(GDELT_URL, None, "all GDELT queries failed: " + "; ".join(notes))
        return CollectorResult(items=ctx.store.upsert_indicator_points(points), notes=notes)
