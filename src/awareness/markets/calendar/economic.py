"""Economic calendar: Nasdaq, ForexFactory and BEA parsers, and their collectors.

Merge and value rules live in ``econ_merge``; parsing helpers in ``econ_common``.
Names re-exported here are the public surface for this subsystem.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from dateutil import parser as dateparser

from awareness.markets.calendar.econ_common import (
    CURRENCY_BY_ISO,
    FF_CURRENCY_ISO,
    NASDAQ_COUNTRY_ISO,
    classify,
    clean_text,
    display,
    euro_member_iso,
    event_id,
    normalize_title,
    parse_value,
    utc_of,
)
from awareness.markets.calendar.econ_merge import MergeState, finalize_event, merge_econ_events
from awareness.markets.collectors.base import CollectorContext, CollectorResult
from awareness.markets.http import FetchError
from awareness.markets.models import EconEvent, ProviderRef

__all__ = [
    "BEA_URL",
    "FF_URL",
    "NASDAQ_URL",
    "BeaCollector",
    "ForexFactoryCollector",
    "MergeState",
    "NasdaqEconCollector",
    "day_bounds",
    "event_id",
    "finalize_event",
    "merge_econ_events",
    "normalize_title",
    "padded_bounds",
    "parse_bea_release_dates",
    "parse_forexfactory",
    "parse_nasdaq_econ",
    "parse_value",
    "store_merge",
]

log = logging.getLogger(__name__)

NASDAQ_URL = "https://api.nasdaq.com/api/calendar/economicevents"
FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
BEA_URL = "https://apps.bea.gov/API/signup/release_dates.json"
STATE_KEY = "econ.merge_state"
_FF_IMPACT: dict[str, int] = {"high": 3, "medium": 2, "low": 1, "non-economic": 1}


def nasdaq_url(day: date) -> str:
    return f"{NASDAQ_URL}?date={day.isoformat()}"


def _nasdaq_time(gmt: Any, day: date) -> tuple[datetime, bool]:
    text = str(gmt or "").strip()
    parts = text.split(":")
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        hour, minute = int(parts[0]), int(parts[1])
        if 0 <= hour < 24 and 0 <= minute < 60:
            return datetime.combine(day, time(hour, minute), UTC), False
    # "24H", "All Day", "Tentative", blank: the date is known, the time is not
    return datetime.combine(day, time(0, 0), UTC), True


def parse_nasdaq_econ(payload: dict[str, Any], *, day: date, fetched_at: datetime) -> list[EconEvent]:
    """Parse one day of the Nasdaq economic-events API payload.

    Rows whose country or title cannot be mapped are skipped and counted in one log line.
    """
    if not isinstance(payload, dict):
        raise ValueError("unexpected Nasdaq payload shape")
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise ValueError("unexpected Nasdaq data shape")
    rows = data.get("rows") or []
    ref = ProviderRef(provider="nasdaq", url=nasdaq_url(day), fetched_at=utc_of(fetched_at))

    prepared: list[tuple[dict[str, Any], str, str, datetime, bool]] = []
    skipped = 0
    for row in rows:
        iso = NASDAQ_COUNTRY_ISO.get(display(row.get("country")) or "")
        title = display(row.get("eventName"))
        if iso is None or title is None:
            skipped += 1
            continue
        scheduled, all_day = _nasdaq_time(row.get("gmt"), day)
        prepared.append((row, iso, title, scheduled, all_day))
    if skipped:
        log.info("nasdaq econ %s: skipped %d row(s) with unmapped country or title", day.isoformat(), skipped)

    group_sizes = Counter((iso, normalize_title(title), at.date()) for _, iso, title, at, _ in prepared)
    seen: Counter[tuple[str, str, date]] = Counter()
    events: list[EconEvent] = []
    for row, iso, title, scheduled, all_day in prepared:
        key = (iso, normalize_title(title), scheduled.date())
        id_title = title
        if group_sizes[key] > 1:
            seen[key] += 1
            id_title = f"{title} [#{seen[key]}]"  # same-day duplicates stay distinct
        actual = display(row.get("actual"))
        consensus = display(row.get("consensus"))
        previous = display(row.get("previous"))
        actual_value, actual_unit = parse_value(actual)
        forecast_value, forecast_unit = parse_value(consensus)
        previous_value, previous_unit = parse_value(previous)
        importance, category = classify(title)
        events.append(
            EconEvent(
                id=event_id(iso, id_title, scheduled),
                country=iso,
                currency=CURRENCY_BY_ISO.get(iso),
                title=title,
                category=category,
                scheduled_at=scheduled,
                all_day=all_day,
                importance=importance,
                actual=actual,
                forecast=consensus,
                previous=previous,
                actual_value=actual_value,
                forecast_value=forecast_value,
                previous_value=previous_value,
                unit=next((u for u in (actual_unit, forecast_unit, previous_unit) if u), None),
                description=clean_text(row.get("description")),
                sources=[ref],
            )
        )
    return events


def parse_forexfactory(payload: list[dict[str, Any]], *, fetched_at: datetime) -> list[EconEvent]:
    """Parse the ForexFactory 'this week' JSON.

    Holidays are skipped. Impact High/Medium/Low maps to 3/2/1; an unrecognized impact
    falls back to 1 (the rule table can still raise it). Importance = max(impact, rule).
    EUR rows get the member-state country from the title prefix (German -> DE), else EU;
    the display title is never changed.
    """
    if not isinstance(payload, list):
        raise ValueError("unexpected ForexFactory payload shape")
    ref = ProviderRef(provider="forexfactory", url=FF_URL, fetched_at=utc_of(fetched_at))
    out: dict[str, EconEvent] = {}
    for item in payload:
        impact = str(item.get("impact") or "").strip()
        if impact.lower() == "holiday":
            continue
        currency = str(item.get("country") or "").strip().upper()
        title = display(item.get("title"))
        if title is None or currency not in FF_CURRENCY_ISO:
            log.debug("forexfactory: skipping currency=%r title=%r", currency, title)
            continue
        iso = euro_member_iso(title) if currency == "EUR" else FF_CURRENCY_ISO[currency]
        try:
            when = dateparser.isoparse(str(item["date"]))
        except (KeyError, ValueError, TypeError):
            log.debug("forexfactory: unparseable date for %r", title)
            continue
        all_day = "all day" in title.lower()
        scheduled = datetime.combine(when.date(), time(0, 0), UTC) if all_day else utc_of(when)
        table_importance, category = classify(title)
        forecast = display(item.get("forecast"))
        previous = display(item.get("previous"))
        actual = display(item.get("actual"))
        actual_value, actual_unit = parse_value(actual)
        forecast_value, forecast_unit = parse_value(forecast)
        previous_value, previous_unit = parse_value(previous)
        event = EconEvent(
            id=event_id(iso, title, scheduled),
            country=iso,
            currency=currency,
            title=title,
            category=category,
            scheduled_at=scheduled,
            all_day=all_day,
            importance=max(_FF_IMPACT.get(impact.lower(), 1), table_importance),
            actual=actual,
            forecast=forecast,
            previous=previous,
            actual_value=actual_value,
            forecast_value=forecast_value,
            previous_value=previous_value,
            unit=next((u for u in (actual_unit, forecast_unit, previous_unit) if u), None),
            sources=[ref],
        )
        out.setdefault(event.id, event)
    return list(out.values())


def parse_bea_release_dates(
    payload: dict[str, Any], *, fetched_at: datetime, start: date, end: date
) -> list[EconEvent]:
    """US release dates from BEA, filtered to ``[start, end]`` (UTC dates, inclusive)."""
    ref = ProviderRef(provider="bea", url=BEA_URL, fetched_at=utc_of(fetched_at), basis="native")
    out: dict[str, EconEvent] = {}
    for name, info in (payload or {}).items():
        title = display(name)
        if title is None or not isinstance(info, dict):
            continue  # top-level metadata such as "file_last_updated" is not a release
        importance, category = classify(title)
        for raw in info.get("release_dates") or []:
            try:
                when = utc_of(dateparser.isoparse(str(raw)))
            except (ValueError, TypeError):
                log.debug("bea: unparseable release date %r for %r", raw, title)
                continue
            if not start <= when.date() <= end:
                continue
            event = EconEvent(
                id=event_id("US", title, when),
                country="US",
                currency="USD",
                title=title,
                category=category,
                scheduled_at=when,
                all_day=when.time() == time(0, 0),
                importance=importance,
                sources=[ref],
            )
            out.setdefault(event.id, event)
    return list(out.values())


# ── collectors ────────────────────────────────────────────────────────────


def store_merge(
    ctx: CollectorContext, incoming: list[EconEvent], start: datetime, end: datetime, now: datetime
) -> list[EconEvent]:
    """Merge ``incoming`` into the stored window and persist the changed rows and merge state."""
    existing = ctx.store.query_econ(start, end)
    state = MergeState.from_json(ctx.store.kv_get(STATE_KEY))
    changed = merge_econ_events(existing, incoming, now=now, state=state)
    ctx.store.upsert_econ_events(changed)
    ctx.store.kv_set(STATE_KEY, state.to_json())
    return changed


def day_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """UTC bounds covering whole days ``start`` through ``end`` inclusive."""
    return datetime.combine(start, time(0, 0), UTC), datetime.combine(end, time(23, 59, 59), UTC)


def padded_bounds(events: list[EconEvent]) -> tuple[datetime, datetime] | None:
    """Window covering the events, padded by one day on each side (None when empty)."""
    if not events:
        return None
    lowest = min(e.scheduled_at for e in events) - timedelta(days=1)
    highest = max(e.scheduled_at for e in events) + timedelta(days=1)
    return lowest, highest


class NasdaqEconCollector:
    """Daily Nasdaq economic-events pages for ``today - econ_days_back .. today + econ_days_ahead``.

    A failing day is logged and skipped; the collector raises only when every day fails.
    """

    name = "econ.nasdaq"
    interval_s = 1800
    lock_group: str | None = "econ"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        now = utc_of(ctx.now())
        today = now.date()
        start_day = today - timedelta(days=ctx.settings.econ_days_back)
        end_day = today + timedelta(days=ctx.settings.econ_days_ahead)
        days = [start_day + timedelta(days=i) for i in range((end_day - start_day).days + 1)]
        incoming: list[EconEvent] = []
        failed: list[date] = []
        for day in days:
            try:
                response = await ctx.http.get(nasdaq_url(day), profile="browser")
                incoming.extend(parse_nasdaq_econ(response.json(), day=day, fetched_at=response.fetched_at))
            except (FetchError, ValueError) as exc:
                failed.append(day)
                ctx.log.warning("nasdaq econ %s failed: %s", day.isoformat(), exc)
        if len(failed) == len(days):
            raise FetchError(nasdaq_url(start_day), None, "every Nasdaq economic-events day failed")
        start, end = day_bounds(start_day, end_day)
        changed = store_merge(ctx, incoming, start, end, now)
        notes = [f"{len(failed)} of {len(days)} day(s) failed"] if failed else []
        return CollectorResult(items=len(changed), notes=notes)


class ForexFactoryCollector:
    """ForexFactory 'this week' calendar."""

    name = "econ.forexfactory"
    interval_s = 3600
    lock_group: str | None = "econ"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        now = utc_of(ctx.now())
        response = await ctx.http.get(FF_URL, profile="browser")
        incoming = parse_forexfactory(response.json(), fetched_at=response.fetched_at)
        bounds = padded_bounds(incoming)
        if bounds is None:
            return CollectorResult(items=0, notes=["no events in feed"])
        changed = store_merge(ctx, incoming, bounds[0], bounds[1], now)
        return CollectorResult(items=len(changed))


class BeaCollector:
    """BEA release dates for ``today - 7 .. today + 120`` days."""

    name = "econ.bea"
    interval_s = 86400
    lock_group: str | None = "econ"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        now = utc_of(ctx.now())
        today = now.date()
        start_day, end_day = today - timedelta(days=7), today + timedelta(days=120)
        response = await ctx.http.get(BEA_URL, profile="browser")
        incoming = parse_bea_release_dates(
            response.json(), fetched_at=response.fetched_at, start=start_day, end=end_day
        )
        start, end = day_bounds(start_day, end_day)
        changed = store_merge(ctx, incoming, start, end, now)
        return CollectorResult(items=len(changed))
