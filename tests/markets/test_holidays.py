"""Public holidays (Nager.Date) and exchange closures / early closes (exchange_calendars)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

import httpx
import pytest

from awareness.markets.calendar.holidays import (
    ExchangeHolidayCollector,
    NagerHolidayCollector,
    exchange_holidays,
    parse_nager,
)
from awareness.markets.collectors.base import CollectorContext
from awareness.markets.config import MarketsSettings
from awareness.markets.http import Fetcher
from awareness.markets.sources.registry import PublisherRegistry
from awareness.markets.store import MarketsStore

FETCHED = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
NAGER_TR = "https://date.nager.at/api/v3/PublicHolidays/2026/TR"


async def _no_sleep(_: float) -> None:
    return None


def _settings(tmp_path: Path, **overrides: Any) -> MarketsSettings:
    return MarketsSettings(
        data_dir=tmp_path / "markets", autostart_runner=False, sentiment_backend="lexicon", **overrides
    )


def test_parse_nager_turkish_holidays(fixture_bytes: Callable[[str], bytes]) -> None:
    payload = json.loads(fixture_bytes("nager_TR_2026.json").decode("utf-8"))
    holidays = parse_nager(payload, country="TR", fetched_at=FETCHED, url=NAGER_TR)
    assert len(holidays) == len(payload) == 14
    republic = next(h for h in holidays if h.date == date(2026, 10, 29))
    assert republic.name == "Republic Day"
    assert republic.country == "TR"
    assert republic.kind == "public"
    assert republic.exchange is None
    assert republic.source.provider == "nager"
    assert republic.source.url == NAGER_TR
    assert republic.source.basis == "native"
    again = parse_nager(payload, country="TR", fetched_at=FETCHED, url=NAGER_TR)
    assert [h.id for h in again] == [h.id for h in holidays]


def test_parse_nager_rejects_non_list_body() -> None:
    with pytest.raises(ValueError):
        parse_nager({"message": "error"}, country="TR", fetched_at=FETCHED, url=NAGER_TR)


def test_coverage_end_recorded(tmp_path: Path, store: MarketsStore) -> None:
    settings = _settings(tmp_path, exchanges=["XNYS"])
    ctx = CollectorContext(
        settings=settings,
        store=store,
        http=Fetcher(settings, store, transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        registry=PublisherRegistry([]),
        now=lambda: FETCHED,
    )

    async def run() -> None:
        try:
            await ExchangeHolidayCollector().run(ctx)
        finally:
            await ctx.http.aclose()

    asyncio.run(run())
    assert store.kv_get("holidays.coverage_end.XNYS") is not None


def test_parse_nager_regional_rule() -> None:
    payload = [
        {"date": "2026-04-03", "name": "Regional A", "localName": "A", "global": False, "counties": ["US-TX"]},
        {"date": "2026-04-04", "name": "Unscoped", "localName": None, "global": False, "counties": None},
        {"date": "2026-07-04", "name": "Independence Day", "localName": None, "global": True, "counties": None},
        {"date": "not-a-date", "name": "Broken", "global": True, "counties": None},
        "garbage",
    ]
    holidays = parse_nager(payload, country="us", fetched_at=FETCHED, url=NAGER_TR)
    assert [h.name for h in holidays] == ["Unscoped", "Independence Day"]
    assert all(h.country == "US" for h in holidays)


def test_exchange_closures_and_early_close_xnys() -> None:
    holidays = exchange_holidays("XNYS", date(2026, 11, 1), date(2026, 11, 30), fetched_at=FETCHED)
    closed = {h.date: h for h in holidays if h.kind == "market_closed"}
    thanksgiving = closed[date(2026, 11, 26)]
    assert thanksgiving.name == "Thanksgiving"
    assert thanksgiving.exchange == "XNYS" and thanksgiving.country == "US"
    assert thanksgiving.source.provider == "exchange_calendars"
    assert thanksgiving.source.url is None
    assert thanksgiving.source.basis == "derived"

    early = next(h for h in holidays if h.kind == "early_close")
    assert early.date == date(2026, 11, 27)
    assert early.early_close_time == time(13, 0)
    assert early.name == "Early close"

    assert all(day.weekday() < 5 for day in closed)  # no weekend closures emitted
    assert date(2026, 11, 28) not in closed and date(2026, 11, 30) not in closed


def test_exchange_closures_xist_republic_day() -> None:
    holidays = exchange_holidays("XIST", date(2026, 10, 20), date(2026, 11, 5), fetched_at=FETCHED)
    republic = next(h for h in holidays if h.date == date(2026, 10, 29))
    assert republic.kind == "market_closed"
    assert republic.name == "Republic Day"
    assert republic.country == "TR" and republic.exchange == "XIST"
    eve = next(h for h in holidays if h.kind == "early_close")
    assert eve.date == date(2026, 10, 28)
    assert eve.early_close_time == time(12, 30)
    assert all(day.weekday() < 5 for day in (h.date for h in holidays if h.kind == "market_closed"))


def test_exchange_ids_are_deterministic_and_clipped() -> None:
    import exchange_calendars  # noqa: PLC0415

    first = exchange_holidays("XNYS", date(2026, 11, 1), date(2026, 11, 30), fetched_at=FETCHED)
    second = exchange_holidays("XNYS", date(2026, 11, 1), date(2026, 11, 30), fetched_at=FETCHED)
    assert [h.id for h in first] == [h.id for h in second]
    far = exchange_holidays("XNYS", date(2026, 11, 1), date(2099, 1, 1), fetched_at=FETCHED)
    last = exchange_calendars.get_calendar("XNYS").last_session.date()
    assert far and max(h.date for h in far) <= last  # calendar's last supported session


def test_unknown_mic_raises() -> None:
    import exchange_calendars  # noqa: PLC0415

    with pytest.raises(exchange_calendars.errors.InvalidCalendarName):
        exchange_holidays("XNOPE", date(2026, 11, 1), date(2026, 11, 30), fetched_at=FETCHED)


def test_nager_collector_upserts_and_skips_unsupported(
    tmp_path: Path, store: MarketsStore, fixture_bytes: Callable[[str], bytes]
) -> None:
    tr_payload = fixture_bytes("nager_TR_2026.json")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url) == NAGER_TR:
            return httpx.Response(200, content=tr_payload)
        return httpx.Response(404, text="not found")

    settings = _settings(tmp_path, holiday_countries=["TR", "ZZ"])

    async def run() -> Any:
        http = Fetcher(
            settings, store, transport=httpx.MockTransport(handler),
            sleep=_no_sleep, host_intervals={"date.nager.at": 0.0},
        )
        ctx = CollectorContext(
            settings=settings, store=store, http=http, registry=PublisherRegistry([]), now=lambda: FETCHED
        )
        try:
            return await NagerHolidayCollector().run(ctx)
        finally:
            await http.aclose()

    result = asyncio.run(run())
    assert len(seen) == 4  # TR 2026/2027, ZZ 2026/2027
    assert result.items == 14
    assert result.notes == []
    stored = store.query_holidays(date(2026, 1, 1), date(2026, 12, 31), countries=["TR"])
    assert len(stored) == 14
    assert all(h.kind == "public" for h in stored)


def test_nager_collector_all_404_is_not_an_error(tmp_path: Path, store: MarketsStore) -> None:
    settings = _settings(tmp_path, holiday_countries=["ZZ"])

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    async def run() -> Any:
        http = Fetcher(
            settings, store, transport=httpx.MockTransport(handler),
            sleep=_no_sleep, host_intervals={"date.nager.at": 0.0},
        )
        ctx = CollectorContext(
            settings=settings, store=store, http=http, registry=PublisherRegistry([]), now=lambda: FETCHED
        )
        try:
            return await NagerHolidayCollector().run(ctx)
        finally:
            await http.aclose()

    assert asyncio.run(run()).items == 0


def test_nager_collector_raises_when_all_fail(tmp_path: Path, store: MarketsStore) -> None:
    settings = _settings(tmp_path, holiday_countries=["TR"])

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async def run() -> None:
        http = Fetcher(
            settings, store, transport=httpx.MockTransport(handler),
            sleep=_no_sleep, host_intervals={"date.nager.at": 0.0},
        )
        ctx = CollectorContext(
            settings=settings, store=store, http=http, registry=PublisherRegistry([]), now=lambda: FETCHED
        )
        try:
            await NagerHolidayCollector().run(ctx)
        finally:
            await http.aclose()

    with pytest.raises(RuntimeError):
        asyncio.run(run())


def test_exchange_collector_skips_unknown_mic(tmp_path: Path, store: MarketsStore) -> None:
    settings = _settings(tmp_path, exchanges=["XNYS", "XNOPE"])
    ctx = CollectorContext(
        settings=settings,
        store=store,
        http=Fetcher(settings, store, transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        registry=PublisherRegistry([]),
        now=lambda: FETCHED,
    )

    async def run() -> Any:
        try:
            return await ExchangeHolidayCollector().run(ctx)
        finally:
            await ctx.http.aclose()

    result = asyncio.run(run())
    assert result.items > 0
    assert result.notes == ["skipped 1 exchange(s)"]
    stored = store.query_holidays(date(2026, 11, 1), date(2026, 12, 31), exchanges=["XNYS"])
    names = {h.name for h in stored}
    assert "Thanksgiving" in names and "Early close" in names
    assert store.query_holidays(date(2026, 11, 1), date(2026, 12, 31), exchanges=["XNOPE"]) == []

    again = asyncio.run(run())
    assert again.items == result.items
    assert len(store.query_holidays(date(2026, 11, 1), date(2026, 12, 31), exchanges=["XNYS"])) == len(stored)


def test_exchange_collector_raises_when_no_exchange_builds(tmp_path: Path, store: MarketsStore) -> None:
    settings = _settings(tmp_path, exchanges=["XNOPE"])
    ctx = CollectorContext(
        settings=settings,
        store=store,
        http=Fetcher(settings, store, transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        registry=PublisherRegistry([]),
        now=lambda: FETCHED,
    )

    async def run() -> None:
        try:
            await ExchangeHolidayCollector().run(ctx)
        finally:
            await ctx.http.aclose()

    with pytest.raises(RuntimeError):
        asyncio.run(run())
