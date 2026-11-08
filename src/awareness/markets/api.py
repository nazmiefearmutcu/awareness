"""HTTP API for the markets layer (``/v1/markets``) — read-mostly, JSON envelopes.

Every response is ``{"data": …, "meta": {generated_at, count, providers, filters, notes, …}}``.
``meta.providers`` lists the health of the providers that feed the endpoint so a
consumer can show staleness honestly. Empty data is a normal 200 with notes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query

from awareness.markets.calendar.sec_submissions import ensure_symbol_filings
from awareness.markets.config import MarketsSettings
from awareness.markets.health import HealthTracker
from awareness.markets.http import Fetcher
from awareness.markets.models import QUALITY_ORDER, NewsItem, ProviderHealth
from awareness.markets.sources.registry import PublisherRegistry
from awareness.markets.store import MarketsStore, NewsQuery

log = logging.getLogger(__name__)

NEWS_PROVIDERS = ["news.feeds", "news.gnews"]
ECON_PROVIDERS = ["econ.nasdaq", "econ.forexfactory", "econ.bea", "econ.central_banks"]
CORP_PROVIDERS = ["corp.nasdaq"]
FILING_PROVIDERS = ["corp.sec_filings", "corp.sec_submissions"]
HOLIDAY_PROVIDERS = ["holidays.public", "holidays.exchanges"]
MARKET_SENT_PROVIDERS = ["sent.cnn_fng", "sent.crypto_fng", "sent.cboe_vix", "sent.apewisdom"]
SOCIAL_PROVIDERS = ["sent.apewisdom", "news.feeds"]
ENTITY_SENT_PROVIDERS = ["news.feeds", "news.gnews", "sent.gdelt_tone"]


def _csv(value: str | None, *, upper: bool = False) -> list[str]:
    if not value:
        return []
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return [p.upper() for p in parts] if upper else parts


def _parse_when(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    try:
        if len(raw) == 10:
            d = date.fromisoformat(raw)
            return datetime.combine(d, time.max if end_of_day else time.min, tzinfo=UTC)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid date/time: {value!r}") from exc


def _parse_day(value: str | None, default: date) -> date:
    if not value:
        return default
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid date: {value!r}") from exc


def _provider_summary(rows: Sequence[ProviderHealth]) -> list[dict[str, Any]]:
    return [
        {
            "provider": h.provider,
            "status": h.status,
            "last_ok_at": h.last_ok_at.isoformat() if h.last_ok_at else None,
            "last_error": h.last_error if h.status != "ok" else None,
            "items_last_ok": h.items_last_ok,
        }
        for h in rows
    ]


class MarketsAPI:
    """Holds dependencies; the router closes over one instance."""

    def __init__(
        self,
        *,
        settings: MarketsSettings,
        get_store: Callable[[], MarketsStore],
        get_registry: Callable[[], PublisherRegistry],
        get_runner: Callable[[], Any] | None = None,
        get_fetcher: Callable[[], Fetcher | None] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.get_store = get_store
        self.get_registry = get_registry
        self.get_runner = get_runner or (lambda: None)
        self.get_fetcher = get_fetcher or (lambda: None)
        self.now = now or (lambda: datetime.now(UTC))

    def providers(self, names: Sequence[str]) -> list[dict[str, Any]]:
        tracker = HealthTracker(self.get_store(), now=self.now)
        rows = {h.provider: h for h in tracker.snapshot()}
        out = []
        for name in names:
            row = rows.get(name) or ProviderHealth(provider=name)
            row.status = row.derive_status(self.now())
            out.append(row)
        return _provider_summary(out)

    def feed_health_summary(self) -> dict[str, int]:
        tracker = HealthTracker(self.get_store(), now=self.now)
        counts = {"ok": 0, "degraded": 0, "down": 0, "never": 0}
        for row in tracker.snapshot():
            if row.provider.startswith("feed."):
                counts[row.status] = counts.get(row.status, 0) + 1
        return counts

    def envelope(
        self,
        data: Any,
        *,
        providers: Sequence[str] = (),
        filters: dict[str, Any] | None = None,
        notes: list[str] | None = None,
        count: int | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        provider_rows = self.providers(providers) if providers else []
        all_notes = list(notes or [])
        stale = [p["provider"] for p in provider_rows if p["status"] in ("down", "degraded")]
        if stale:
            all_notes.append("stale or failing providers: " + ", ".join(stale))
        never = [p["provider"] for p in provider_rows if p["status"] == "never"]
        if never:
            all_notes.append("providers not collected yet: " + ", ".join(never))
        if count is None and isinstance(data, list):
            count = len(data)
        meta: dict[str, Any] = {
            "generated_at": self.now().isoformat(),
            "count": count,
            "providers": provider_rows,
            "filters": {k: v for k, v in (filters or {}).items() if v not in (None, [], "")},
            "notes": all_notes,
        }
        meta.update(extra)
        return {"data": data, "meta": meta}


def _story_brief(store: MarketsStore, story_id: str | None, registry: PublisherRegistry) -> dict[str, Any] | None:
    if not story_id:
        return None
    story = store.get_story(story_id)
    if story is None:
        return None
    items = store.get_news_many(story.item_ids)
    publishers = []
    for pid in story.publishers:
        pub = registry.get(pid)
        publishers.append({"id": pid, "name": pub.name if pub else pid, "tier": pub.tier if pub else None})
    return {
        "id": story.id,
        "title": story.title,
        "corroboration": story.corroboration,
        "first_seen_at": story.first_seen_at.isoformat(),
        "last_updated_at": story.last_updated_at.isoformat(),
        "publishers": publishers,
        "items": [
            {
                "id": i.id,
                "title": i.title,
                "publisher_name": i.publisher_name,
                "publisher_tier": i.publisher_tier,
                "url": i.url,
                "published_at": i.published_at.isoformat(),
                "quality_label": i.quality_label,
            }
            for i in items
        ],
    }


def _news_dict(item: NewsItem, story_sizes: dict[str, int] | None = None) -> dict[str, Any]:
    data = item.public_dict()
    if story_sizes is not None and item.story_id:
        data["story_size"] = story_sizes.get(item.story_id, 1)
    return data


def create_markets_router(api: MarketsAPI, *, prefix: str = "/v1/markets") -> APIRouter:  # noqa: PLR0915
    router = APIRouter(prefix=prefix, tags=["markets"])

    def require_key(authorization: Annotated[str | None, Header()] = None) -> None:
        key = api.settings.api_key
        if key and authorization != f"Bearer {key}":
            raise HTTPException(status_code=401, detail="missing or invalid bearer token")

    # ── news ──────────────────────────────────────────────────────────────

    @router.get("/news")
    def list_news(
        symbols: str | None = None,
        countries: str | None = None,
        topics: str | None = None,
        asset_classes: str | None = None,
        publishers: str | None = None,
        content_types: str | None = None,
        min_quality: Literal["premium", "standard", "low", "rejected"] = "standard",
        min_importance: float | None = Query(default=None, ge=0, le=100),
        since: str | None = None,
        until: str | None = None,
        hours: float | None = Query(default=None, gt=0, le=24 * 60),
        q: str | None = Query(default=None, max_length=200),
        story: bool = True,
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str | None = None,
    ) -> dict[str, Any]:
        store = api.get_store()
        since_dt = _parse_when(since)
        if since_dt is None and hours:
            since_dt = api.now() - timedelta(hours=hours)
        query = NewsQuery(
            symbols=_csv(symbols, upper=True),
            countries=_csv(countries, upper=True),
            topics=_csv(topics),
            asset_classes=_csv(asset_classes),
            publishers=_csv(publishers),
            content_types=_csv(content_types),
            min_quality=min_quality,
            min_importance=min_importance,
            since=since_dt,
            until=_parse_when(until, end_of_day=True),
            q=q,
            collapse_stories=story,
            limit=limit,
            cursor=cursor,
        )
        items, next_cursor = store.query_news(query)
        sizes: dict[str, int] = {}
        for sid in {i.story_id for i in items if i.story_id}:
            st = store.get_story(sid)
            sizes[sid] = len(st.item_ids) if st else 1
        notes = []
        if not items:
            notes.append("no news matched the filters" + (" (quality floor: " + min_quality + ")" if min_quality else ""))
        return api.envelope(
            [_news_dict(i, sizes) for i in items],
            providers=NEWS_PROVIDERS,
            filters={
                "symbols": query.symbols, "countries": query.countries, "topics": query.topics,
                "asset_classes": query.asset_classes, "publishers": query.publishers,
                "content_types": query.content_types, "min_quality": min_quality,
                "min_importance": min_importance, "since": since_dt.isoformat() if since_dt else None,
                "q": q, "story": story,
            },
            notes=notes,
            next_cursor=next_cursor,
            feed_health=api.feed_health_summary(),
        )

    @router.get("/news/{item_id}")
    def get_news(item_id: str) -> dict[str, Any]:
        store = api.get_store()
        item = store.get_news(item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="news item not found")
        data = item.public_dict()
        data["story"] = _story_brief(store, item.story_id, api.get_registry())
        return api.envelope(data, providers=NEWS_PROVIDERS)

    @router.get("/stories")
    def list_stories(
        symbols: str | None = None,
        min_corroboration: int = Query(default=1, ge=1, le=20),
        hours: float = Query(default=24, gt=0, le=24 * 30),
        limit: int = Query(default=30, ge=1, le=100),
    ) -> dict[str, Any]:
        store = api.get_store()
        stories = store.query_stories(
            since=api.now() - timedelta(hours=hours),
            min_corroboration=min_corroboration,
            symbols=_csv(symbols, upper=True),
            limit=limit,
        )
        leads = {i.id: i for i in store.get_news_many([s.lead_item_id for s in stories])}
        out = []
        for s in stories:
            lead = leads.get(s.lead_item_id)
            if lead is None or lead.story_id != s.id or QUALITY_ORDER[lead.quality_label] < QUALITY_ORDER["standard"]:
                continue  # stale story row (lead re-clustered elsewhere) or hidden lead
            row = s.model_dump(mode="json")
            row["lead"] = _news_dict(lead)
            out.append(row)
        return api.envelope(out, providers=NEWS_PROVIDERS, filters={"symbols": symbols, "hours": hours})

    # ── sentiment ─────────────────────────────────────────────────────────

    @router.get("/sentiment/entity/{entity_id}")
    def entity_sentiment(
        entity_id: str,
        window: Literal["1d", "7d", "30d"] = "7d",
        bucket: Literal["1h", "1d"] = "1h",
    ) -> dict[str, Any]:
        try:
            from awareness.markets.sentiment.aggregate import summarize_entity_sentiment
        except ImportError as exc:  # pragma: no cover - only when the module is missing
            raise HTTPException(status_code=503, detail=f"sentiment aggregation unavailable: {exc}") from exc
        store = api.get_store()
        now = api.now()
        span = {"1d": timedelta(days=1), "7d": timedelta(days=7), "30d": timedelta(days=30)}[window]
        eid = entity_id.upper()
        rows = store.entity_sentiment_rows(eid, now - span, now)
        baseline = store.entity_sentiment_rows(eid, now - timedelta(days=30), now - timedelta(days=1))
        report = summarize_entity_sentiment(
            rows, entity_id=eid, now=now, window=span, bucket=bucket, baseline_rows=baseline
        )
        headlines, _ = store.query_news(NewsQuery(symbols=[eid], since=now - span, limit=12, collapse_stories=True))
        data = report.to_dict()
        data["headlines"] = [
            {
                "id": h.id,
                "title": h.title,
                "publisher_name": h.publisher_name,
                "url": h.url,
                "published_at": h.published_at.isoformat(),
                "sentiment": (h.entity_sentiment.get(eid) or h.sentiment).model_dump() if (h.entity_sentiment.get(eid) or h.sentiment) else None,
                "quality_label": h.quality_label,
            }
            for h in headlines
        ]
        tone = store.query_indicator(f"gdelt.tone:{eid}", since=now - span)
        data["gdelt_tone"] = [{"ts": p.ts.isoformat(), "value": p.value} for p in tone]
        notes = [] if report.count else [f"no scored news for {eid} in the last {window}"]
        return api.envelope(data, providers=ENTITY_SENT_PROVIDERS, filters={"window": window, "bucket": bucket}, notes=notes, count=report.count)

    @router.get("/sentiment/market")
    def market_sentiment(history_days: int = Query(default=90, ge=1, le=400)) -> dict[str, Any]:
        try:
            from awareness.markets.sentiment.snapshot import market_sentiment_snapshot
        except ImportError as exc:  # pragma: no cover
            raise HTTPException(status_code=503, detail=f"market sentiment unavailable: {exc}") from exc
        data = market_sentiment_snapshot(api.get_store(), now=api.now(), history_days=history_days)
        missing = [k for k in ("cnn", "crypto", "vix") if not data.get(k)]
        notes = [f"no data yet for: {', '.join(missing)}"] if missing else []
        return api.envelope(data, providers=MARKET_SENT_PROVIDERS, notes=notes, count=None)

    @router.get("/sentiment/social")
    def social_sentiment(limit: int = Query(default=25, ge=1, le=100)) -> dict[str, Any]:
        try:
            from awareness.markets.sentiment.snapshot import social_snapshot
        except ImportError as exc:  # pragma: no cover
            raise HTTPException(status_code=503, detail=f"social sentiment unavailable: {exc}") from exc
        store = api.get_store()
        since = api.now() - timedelta(hours=24)
        reddit_items, _ = store.query_news(
            NewsQuery(content_types=["social"], since=since, min_quality="low", limit=200, collapse_stories=False)
        )
        mentions: dict[str, int] = {}
        for item in reddit_items:
            for sym in item.symbols:
                mentions[sym] = mentions.get(sym, 0) + 1
        data = {
            "stocks": social_snapshot(store, universe="stocks", limit=limit),
            "crypto": social_snapshot(store, universe="crypto", limit=limit),
            "reddit_24h": {
                "posts": len(reddit_items),
                "top_symbols": sorted(
                    ({"symbol": s, "posts": n} for s, n in mentions.items()), key=lambda r: -r["posts"]
                )[:20],
            },
        }
        return api.envelope(data, providers=SOCIAL_PROVIDERS, count=None)

    # ── calendars ─────────────────────────────────────────────────────────

    @router.get("/calendar/economic")
    def economic_calendar(
        from_: Annotated[str | None, Query(alias="from")] = None,
        to: str | None = None,
        countries: str | None = None,
        min_importance: int = Query(default=1, ge=1, le=3),
        categories: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        today = api.now().date()
        start = _parse_day(from_, today)
        end = _parse_day(to, start + timedelta(days=7))
        if end < start:
            raise HTTPException(status_code=422, detail="'to' is before 'from'")
        if (end - start).days > 120:
            raise HTTPException(status_code=422, detail="window too large (max 120 days)")
        events = api.get_store().query_econ(
            datetime.combine(start, time.min, tzinfo=UTC),
            datetime.combine(end, time.max, tzinfo=UTC),
            countries=_csv(countries, upper=True),
            min_importance=min_importance,
            categories=_csv(categories),
            statuses=_csv(status),
        )
        notes = [] if events else ["no economic events matched the filters"]
        return api.envelope(
            [e.model_dump(mode="json") for e in events],
            providers=ECON_PROVIDERS,
            filters={"from": start.isoformat(), "to": end.isoformat(), "countries": countries,
                     "min_importance": min_importance, "categories": categories, "status": status},
            notes=notes,
        )

    @router.get("/calendar/corporate")
    def corporate_calendar(
        from_: Annotated[str | None, Query(alias="from")] = None,
        to: str | None = None,
        types: str | None = None,
        symbols: str | None = None,
        limit: int = Query(default=2000, ge=1, le=5000),
    ) -> dict[str, Any]:
        today = api.now().date()
        start = _parse_day(from_, today)
        end = _parse_day(to, start + timedelta(days=7))
        if end < start:
            raise HTTPException(status_code=422, detail="'to' is before 'from'")
        wanted_types = _csv(types) or ["earnings", "dividend", "split", "ipo"]
        events = api.get_store().query_corp(start, end, types=wanted_types, symbols=_csv(symbols, upper=True), limit=limit)
        providers = list(CORP_PROVIDERS) + (FILING_PROVIDERS if "filing" in wanted_types else [])
        notes = [] if events else ["no corporate events matched the filters"]
        return api.envelope(
            [e.model_dump(mode="json") for e in events],
            providers=providers,
            filters={"from": start.isoformat(), "to": end.isoformat(), "types": wanted_types, "symbols": symbols},
            notes=notes,
        )

    @router.get("/calendar/holidays")
    def holidays(
        from_: Annotated[str | None, Query(alias="from")] = None,
        to: str | None = None,
        countries: str | None = None,
        exchanges: str | None = None,
    ) -> dict[str, Any]:
        today = api.now().date()
        start = _parse_day(from_, today)
        end = _parse_day(to, start + timedelta(days=60))
        rows = api.get_store().query_holidays(
            start, end, countries=_csv(countries, upper=True), exchanges=_csv(exchanges, upper=True)
        )
        return api.envelope(
            [h.model_dump(mode="json") for h in rows],
            providers=HOLIDAY_PROVIDERS,
            filters={"from": start.isoformat(), "to": end.isoformat(), "countries": countries, "exchanges": exchanges},
            notes=[] if rows else ["no holidays in range for the filters"],
        )

    @router.get("/calendar/sessions")
    def sessions(
        exchange: str,
        from_: Annotated[str | None, Query(alias="from")] = None,
        to: str | None = None,
    ) -> dict[str, Any]:
        from awareness.markets.calendar.sessions import exchange_sessions

        today = api.now().date()
        start = _parse_day(from_, today)
        end = _parse_day(to, start + timedelta(days=14))
        if (end - start).days > 400:
            raise HTTPException(status_code=422, detail="window too large (max 400 days)")
        try:
            data = exchange_sessions(exchange, start, end)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return api.envelope(
            data,
            filters={"exchange": exchange.upper(), "from": start.isoformat(), "to": end.isoformat()},
            count=len(data["sessions"]),
            notes=["computed offline from exchange_calendars rule sets"],
        )

    @router.get("/filings")
    async def filings(
        symbols: str | None = None,
        forms: str | None = None,
        days: int = Query(default=7, ge=1, le=400),
        material_only: bool = False,
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> dict[str, Any]:
        today = api.now().date()
        notes: list[str] = []
        tickers = [t.replace(".", "-") for t in _csv(symbols, upper=True)]  # BRK.B is stored as BRK-B
        if tickers:
            fetcher = api.get_fetcher()
            if fetcher is None:
                notes.append("SEC submissions backfill skipped: no fetcher configured")
            else:
                notes.extend(
                    await ensure_symbol_filings(
                        store=api.get_store(), fetcher=fetcher, settings=api.settings, symbols=tickers, now=api.now()
                    )
                )
        events = api.get_store().query_corp(
            today - timedelta(days=days), today, types=["filing"], symbols=tickers, limit=5000
        )
        wanted_forms = {f.upper() for f in _csv(forms)}
        out = []
        for e in sorted(events, key=lambda ev: (ev.datetime_utc or datetime.combine(ev.date, time.min, tzinfo=UTC)), reverse=True):
            form = str(e.fields.get("form", "")).upper()
            if wanted_forms and form not in wanted_forms:
                continue
            if material_only and not e.fields.get("material"):
                continue
            out.append(e.model_dump(mode="json"))
            if len(out) >= limit:
                break
        return api.envelope(
            out,
            providers=FILING_PROVIDERS,
            filters={"symbols": symbols, "forms": forms, "days": days, "material_only": material_only},
            notes=notes,
        )

    # ── operations ────────────────────────────────────────────────────────

    @router.get("/sources")
    def sources() -> dict[str, Any]:
        registry = api.get_registry()
        tracker = HealthTracker(api.get_store(), now=api.now)
        health = {h.provider: h for h in tracker.snapshot()}
        out = []
        for pub in sorted(registry.all(), key=lambda p: (p.tier, p.name)):
            feeds = []
            for feed in pub.feeds:
                h = health.get(f"feed.{feed.id}")
                feeds.append(
                    {
                        "id": feed.id,
                        "url": feed.url,
                        "categories": feed.categories,
                        "poll_seconds": feed.poll_seconds,
                        "status": h.status if h else "never",
                        "last_ok_at": h.last_ok_at.isoformat() if h and h.last_ok_at else None,
                        "last_error": h.last_error if h and h.status != "ok" else None,
                    }
                )
            out.append(
                {
                    "id": pub.id, "name": pub.name, "domain": pub.domain, "tier": pub.tier, "kind": pub.kind,
                    "region": pub.region, "languages": pub.languages, "paywalled": pub.paywalled, "feeds": feeds,
                }
            )
        return api.envelope(out, feed_health=api.feed_health_summary())

    @router.get("/health")
    def health() -> dict[str, Any]:
        store = api.get_store()
        tracker = HealthTracker(store, now=api.now)
        rows = tracker.snapshot()
        core = [r for r in rows if not r.provider.startswith(("feed.", "gnews."))]
        gnews = {"ok": 0, "degraded": 0, "down": 0, "never": 0}
        for r in rows:
            if r.provider.startswith("gnews."):
                gnews[r.status] = gnews.get(r.status, 0) + 1
        runner = api.get_runner()
        data = {
            "status": "ok" if all(r.status in ("ok", "never") for r in core) else "degraded",
            "providers": _provider_summary(core),
            "feeds": api.feed_health_summary(),
            "gnews_queries": gnews,
            "counts": store.counts(),
            "runner": runner.status() if runner is not None else {"running": False, "collectors": []},
            "watchlist": store.get_watchlist(),
        }
        return api.envelope(data, count=None)

    @router.get("/watchlist")
    def get_watchlist() -> dict[str, Any]:
        return api.envelope(api.get_store().get_watchlist())

    @router.post("/watchlist", dependencies=[Depends(require_key)])
    def set_watchlist(symbols: Annotated[list[str], Body(embed=True, max_length=200)]) -> dict[str, Any]:
        clean = api.get_store().set_watchlist(symbols)
        runner = api.get_runner()
        if runner is not None:
            runner.trigger("news.gnews")
            runner.trigger("sent.gdelt_tone")
        return api.envelope(clean)

    @router.post("/collect/{name}", dependencies=[Depends(require_key)])
    def collect(name: str) -> dict[str, Any]:
        runner = api.get_runner()
        if runner is None:
            raise HTTPException(status_code=409, detail="collector runner is not running in this process")
        if not runner.trigger(name):
            raise HTTPException(status_code=404, detail=f"unknown collector {name!r}")
        return api.envelope({"triggered": name})

    return router
