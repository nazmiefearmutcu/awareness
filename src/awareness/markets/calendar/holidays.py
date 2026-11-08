"""Public holidays (Nager.Date) and exchange closures / early closes (exchange_calendars).

Nager gives national public holidays over HTTP. exchange_calendars is offline:
it derives weekday closures, named regular holidays, ad-hoc closures and early
closes from each exchange's rules. Its records carry ``basis="derived"`` because
they are computed, not published by the exchange.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

from awareness.markets.collectors.base import CollectorContext, CollectorResult
from awareness.markets.http import FetchError
from awareness.markets.models import Holiday, ProviderRef, stable_id

log = logging.getLogger(__name__)

NAGER_PROVIDER = "nager"
NAGER_URL = "https://date.nager.at/api/v3/PublicHolidays/{year}/{country}"
EXCHANGE_PROVIDER = "exchange_calendars"

# Exchange MIC -> ISO-3166 country of the venue.
MIC_COUNTRY: dict[str, str] = {
    "XNYS": "US", "XNAS": "US", "XIST": "TR", "XLON": "GB", "XETR": "DE", "XPAR": "FR",
    "XTKS": "JP", "XHKG": "HK", "XSHG": "CN", "XTSE": "CA", "XASX": "AU", "XSWX": "CH",
    "XAMS": "NL", "XMIL": "IT", "XMAD": "ES", "XKRX": "KR", "XBOM": "IN",
}

EXCHANGE_WINDOW_BACK_DAYS = 7
EXCHANGE_WINDOW_AHEAD_DAYS = 400


def parse_nager(payload: object, *, country: str, fetched_at: datetime, url: str) -> list[Holiday]:
    """Public holidays from a Nager ``PublicHolidays`` response.

    National holidays (``global`` true) are kept. Regional entries are kept only when
    they carry no ``counties`` restriction.
    """
    if not isinstance(payload, list):  # e.g. an error object returned with HTTP 200
        raise ValueError(f"Nager payload for {country} is not a list")
    cc = country.upper()
    provider = ProviderRef(provider=NAGER_PROVIDER, url=url, fetched_at=fetched_at)
    out: list[Holiday] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        if row.get("global") is not True and row.get("counties") is not None:
            continue
        name = str(row.get("name") or "").strip()
        raw_date = row.get("date")
        if not name or not isinstance(raw_date, str):
            continue
        try:
            day = date.fromisoformat(raw_date)
        except ValueError:
            continue
        local = str(row.get("localName") or "").strip() or None
        out.append(
            Holiday(
                id=stable_id("holiday", cc, day, name),
                date=day,
                country=cc,
                name=name,
                local_name=local,
                kind="public",
                source=provider,
            )
        )
    return out


def exchange_holidays(mic: str, start: date, end: date, *, fetched_at: datetime) -> list[Holiday]:
    """Closures and early closes for ``mic`` between ``start`` and ``end`` (inclusive).

    Raises ``exchange_calendars`` errors for an unknown MIC. Dates outside the
    calendar's supported range are clipped rather than guessed.
    """
    import exchange_calendars as xcals  # noqa: PLC0415 - pandas-backed, loaded only when needed

    calendar = xcals.get_calendar(mic)
    lo = max(start, calendar.first_session.date())
    hi = min(end, calendar.last_session.date())
    if lo > hi:
        return []
    lo_s, hi_s = lo.isoformat(), hi.isoformat()
    provider = ProviderRef(provider=EXCHANGE_PROVIDER, url=None, fetched_at=fetched_at, basis="derived")
    exchange = mic.upper()
    country = MIC_COUNTRY.get(exchange)

    sessions = {ts.date() for ts in calendar.sessions_in_range(lo_s, hi_s)}
    # Some calendars publish no named regular-holiday rules; their closures stay "Market closed".
    regular_names: dict[date, str] = {}
    if calendar.regular_holidays is not None:
        regular = calendar.regular_holidays.holidays(lo_s, hi_s, return_name=True)
        regular_names = {ts.date(): str(name) for ts, name in regular.items()}

    out: list[Holiday] = []
    day = lo
    while day <= hi:
        if day.weekday() < 5 and day not in sessions:
            name = regular_names.get(day, "Market closed")
            out.append(_exchange_holiday(exchange, country, day, "market_closed", name, provider))
        day += timedelta(days=1)

    for session in calendar.early_closes:
        session_day = session.date()
        if not lo <= session_day <= hi:
            continue
        close_utc = calendar.closes.loc[session.strftime("%Y-%m-%d")]
        local_close = close_utc.tz_convert(calendar.tz).time()
        out.append(
            _exchange_holiday(
                exchange, country, session_day, "early_close", "Early close", provider, early_close_time=local_close
            )
        )
    return out


def _exchange_holiday(
    exchange: str,
    country: str | None,
    day: date,
    kind: Literal["market_closed", "early_close"],
    name: str,
    provider: ProviderRef,
    *,
    early_close_time: time | None = None,
) -> Holiday:
    return Holiday(
        id=stable_id("xholiday", exchange, day, kind),
        date=day,
        country=country,
        exchange=exchange,
        name=name,
        kind=kind,
        early_close_time=early_close_time,
        source=provider,
    )


class NagerHolidayCollector:
    """National public holidays for ``settings.holiday_countries``, this year and next."""

    name = "holidays.public"
    interval_s = 86400
    lock_group: str | None = None

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        today = ctx.now().astimezone(UTC).date()
        years = (today.year, today.year + 1)
        holidays: list[Holiday] = []
        errors: list[str] = []
        ok = 0
        for country in ctx.settings.holiday_countries:
            for year in years:
                url = NAGER_URL.format(year=year, country=country)
                try:
                    result = await ctx.http.get(url, profile="bot")
                    holidays.extend(
                        parse_nager(result.json(), country=country, fetched_at=result.fetched_at, url=url)
                    )
                    ok += 1
                except FetchError as exc:
                    if exc.status == 404:  # Nager does not cover this country/year
                        continue
                    errors.append(f"{url}: {exc}")
                except ValueError as exc:
                    errors.append(f"{url}: {exc}")
        if ok == 0 and errors:
            raise RuntimeError(f"all Nager requests failed; last: {errors[-1]}")
        notes = [f"{len(errors)} Nager requests failed"] if errors else []
        upserted = ctx.store.upsert_holidays(holidays)
        return CollectorResult(items=upserted, notes=notes)


def exchange_coverage_end(mic: str) -> date:
    """Last date the installed exchange_calendars version covers for ``mic``.

    Beyond this date the store holds no closures for the exchange; the coverage end is
    recorded in kv as ``holidays.coverage_end.<MIC>`` so consumers can say "unknown".
    """
    import exchange_calendars as xcals  # noqa: PLC0415 - pandas-backed, loaded only when needed

    return xcals.get_calendar(mic).last_session.date()


def _build_exchange(mic: str, start: date, end: date, fetched_at: datetime) -> tuple[list[Holiday], date]:
    return exchange_holidays(mic, start, end, fetched_at=fetched_at), exchange_coverage_end(mic)


class ExchangeHolidayCollector:
    """Closures and early closes for ``settings.exchanges`` over a rolling window."""

    name = "holidays.exchanges"
    interval_s = 86400
    lock_group: str | None = None

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        now = ctx.now().astimezone(UTC)
        today = now.date()
        start = today - timedelta(days=EXCHANGE_WINDOW_BACK_DAYS)
        end = today + timedelta(days=EXCHANGE_WINDOW_AHEAD_DAYS)
        from exchange_calendars.errors import InvalidCalendarName  # noqa: PLC0415 - lazy, pandas-backed

        holidays: list[Holiday] = []
        errors: list[str] = []
        ok = 0
        for mic in ctx.settings.exchanges:
            try:
                found, coverage_end = await asyncio.to_thread(_build_exchange, mic, start, end, now)
            except InvalidCalendarName as exc:
                errors.append(f"{mic}: unknown calendar")
                log.warning("holidays.exchanges skipped unknown MIC %s: %s", mic, exc)
                continue
            except Exception as exc:  # calendar bug or data problem: keep the traceback, skip this exchange
                errors.append(f"{mic}: {exc}")
                log.exception("holidays.exchanges failed for %s", mic)
                continue
            holidays.extend(found)
            ctx.store.kv_set(f"holidays.coverage_end.{mic.upper()}", coverage_end.isoformat())
            ok += 1
        if ok == 0 and errors:
            raise RuntimeError(f"no exchange calendar could be built; last: {errors[-1]}")
        notes = [f"skipped {len(errors)} exchange(s)"] if errors else []
        upserted = ctx.store.upsert_holidays(holidays)
        return CollectorResult(items=upserted, notes=notes)
