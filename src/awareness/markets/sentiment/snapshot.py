"""JSON-ready market and social sentiment snapshots (``/sentiment/market``, ``/sentiment/social``).

Every section is ``None`` (or an empty list) when the store holds no data for it. Nothing
is filled with defaults. Timestamps are ISO-8601 UTC strings.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from awareness.markets.models import IndicatorPoint, ensure_utc
from awareness.markets.sentiment.indicators import CNN_COMPONENT_LABELS
from awareness.markets.store import MarketsStore

SOCIAL_PREFIX = "social.{universe}.mentions:"
_FALLBACK_SCAN = 500


def _iso(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat()


def _value_at(store: MarketsStore, indicator: str, ts: datetime) -> float | None:
    rows = store.query_indicator(indicator, since=ts, until=ts)
    return rows[-1].value if rows else None


def _history(store: MarketsStore, indicator: str, since: datetime) -> list[IndicatorPoint]:
    return store.query_indicator(indicator, since=since)


def _latest_score_point(store: MarketsStore, indicator: str) -> IndicatorPoint | None:
    """Newest component reading on the 0-100 score scale (see ``indicators`` docstring)."""
    latest = store.latest_indicator(indicator)
    if latest is not None and latest.meta.get("scale") == "score":
        return latest
    for point in reversed(store.query_indicator(indicator, limit=_FALLBACK_SCAN)):
        if point.meta.get("scale") == "score":
            return point
    return None


def _cnn_section(store: MarketsStore, since: datetime) -> dict[str, Any] | None:
    latest = store.latest_indicator("cnn_fear_greed")
    if latest is None:
        return None
    components: list[dict[str, Any]] = []
    for key, label in CNN_COMPONENT_LABELS.items():
        point = _latest_score_point(store, f"cnn_fg.{key}")
        if point is None:
            continue
        components.append({
            "key": key,
            "label": label,
            "score": point.value,
            "rating": point.label,
            "as_of": _iso(point.ts),
        })
    return {
        "score": latest.value,
        "rating": latest.label,
        "as_of": _iso(latest.ts),
        "previous_close": latest.meta.get("previous_close"),
        "previous_1_week": latest.meta.get("previous_1_week"),
        "previous_1_month": latest.meta.get("previous_1_month"),
        "previous_1_year": latest.meta.get("previous_1_year"),
        "components": components,
        "history": [{"ts": _iso(p.ts), "value": p.value} for p in _history(store, "cnn_fear_greed", since)],
    }


def _crypto_section(store: MarketsStore, since: datetime) -> dict[str, Any] | None:
    latest = store.latest_indicator("crypto_fear_greed")
    if latest is None:
        return None
    return {
        "score": latest.value,
        "rating": latest.label,
        "as_of": _iso(latest.ts),
        "history": [{"ts": _iso(p.ts), "value": p.value}
                    for p in _history(store, "crypto_fear_greed", since)],
    }


def _vix_section(store: MarketsStore, since: datetime) -> dict[str, Any] | None:
    """VIX term structure. The 3-month and 9-day values are read at the VIX timestamp.

    ``structure`` is ``contango`` when VIX < VIX3M and ``backwardation`` otherwise (equal counts
    as backwardation). It stays ``None`` when VIX3M is unavailable at that timestamp.
    """
    latest = store.latest_indicator("vix")
    if latest is None:
        return None
    vix3m = _value_at(store, "vix3m", latest.ts)
    vix9d = _value_at(store, "vix9d", latest.ts)
    term_ratio: float | None = None
    structure: str | None = None
    if vix3m is not None and vix3m > 0:
        term_ratio = round(latest.value / vix3m, 3)
        structure = "contango" if latest.value < vix3m else "backwardation"
    vix3m_by_ts = {p.ts: p.value for p in _history(store, "vix3m", since)}
    return {
        "vix": latest.value,
        "vix9d": vix9d,
        "vix3m": vix3m,
        "as_of": _iso(latest.ts),
        "term_ratio": term_ratio,
        "structure": structure,
        "history": [
            {"ts": _iso(p.ts), "vix": p.value, "vix3m": vix3m_by_ts.get(p.ts)}
            for p in _history(store, "vix", since)
        ],
    }


def social_snapshot(
    store: MarketsStore,
    *,
    universe: Literal["stocks", "crypto"],
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Newest ApeWisdom ranking for one universe, sorted by rank (mentions as tie-break).

    Only tickers present in the newest collection run are included. A change is ``None``
    when its baseline is missing or zero.
    """
    prefix = SOCIAL_PREFIX.format(universe=universe)
    latest = store.latest_by_prefix(prefix)
    if not latest:
        return []
    newest = max(p.ts for p in latest)
    current = [p for p in latest if p.ts == newest]

    def order(p: IndicatorPoint) -> tuple[bool, int, float]:
        rank = p.meta.get("rank")
        return (rank is None, rank if rank is not None else 0, -p.value)

    rows: list[dict[str, Any]] = []
    for point in sorted(current, key=order)[:limit]:
        meta = point.meta
        mentions = round(point.value)
        before = meta.get("mentions_24h_ago")
        rank = meta.get("rank")
        rank_before = meta.get("rank_24h_ago")
        rows.append({
            "ticker": point.indicator[len(prefix):],
            "name": meta.get("name"),
            "rank": rank,
            "mentions": mentions,
            "upvotes": meta.get("upvotes"),
            "mentions_24h_ago": before,
            "mention_change_pct": round((mentions - before) / before * 100, 1) if before else None,
            "rank_change": rank_before - rank if rank is not None and rank_before is not None else None,
            "as_of": _iso(point.ts),
        })
    return rows


def market_sentiment_snapshot(store: MarketsStore, *, now: datetime, history_days: int = 90) -> dict[str, Any]:
    """Market-wide sentiment payload: CNN, crypto F&G, VIX term structure and social rankings.

    ``history`` lists cover the last ``history_days`` days ending at ``now``.
    """
    since = ensure_utc(now) - timedelta(days=history_days)  # type: ignore[operator]
    stocks_latest = store.latest_by_prefix(SOCIAL_PREFIX.format(universe="stocks"))
    crypto_latest = store.latest_by_prefix(SOCIAL_PREFIX.format(universe="crypto"))
    social_points = [*stocks_latest, *crypto_latest]
    apewisdom_as_of = max((p.ts for p in social_points), default=None)

    cnn_latest = store.latest_indicator("cnn_fear_greed")
    crypto_fng = store.latest_indicator("crypto_fear_greed")
    vix_latest = store.latest_indicator("vix")
    return {
        "cnn": _cnn_section(store, since),
        "crypto": _crypto_section(store, since),
        "vix": _vix_section(store, since),
        "social": {
            "stocks": social_snapshot(store, universe="stocks", limit=15),
            "crypto": social_snapshot(store, universe="crypto", limit=10),
        },
        "sources": {
            "cnn": _iso(cnn_latest.ts) if cnn_latest else None,
            "alternative_me": _iso(crypto_fng.ts) if crypto_fng else None,
            "cboe": _iso(vix_latest.ts) if vix_latest else None,
            "apewisdom": _iso(apewisdom_as_of) if apewisdom_as_of else None,
        },
    }
