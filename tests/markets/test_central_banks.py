"""Central-bank meeting schedules: Fed, ECB, BoE, BoJ parsers, curated loader, collector."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time
from pathlib import Path

import httpx
import pytest

from awareness.markets.calendar.central_banks import (
    BOE_URL,
    BOJ_URL,
    ECB_URL,
    FED_URL,
    CentralBankCollector,
    load_curated_schedules,
    parse_boe_mpc_dates,
    parse_boj_schedule,
    parse_ecb_calendar,
    parse_fomc_calendar,
)
from awareness.markets.collectors.base import CollectorContext
from awareness.markets.config import MarketsSettings
from awareness.markets.http import Fetcher, FetchError
from awareness.markets.models import EconEvent
from awareness.markets.store import MarketsStore

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def _by_title(events: list[EconEvent], title: str) -> list[EconEvent]:
    return [e for e in events if e.title == title]


def _at(events: list[EconEvent], when: datetime) -> list[EconEvent]:
    return [e for e in events if e.scheduled_at == when]


# ── FOMC ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def fomc_events(fixture_path: Callable[[str], Path]) -> list[EconEvent]:
    html = fixture_path("fomc_calendars.html").read_text(encoding="utf-8-sig")
    return parse_fomc_calendar(html, fetched_at=NOW)


def test_fomc_future_meetings(fomc_events: list[EconEvent]) -> None:
    decisions = _by_title(fomc_events, "FOMC Interest Rate Decision")
    future = [e for e in decisions if e.scheduled_at > NOW]
    assert len(future) >= 4


def test_fomc_dst_and_non_dst_utc_conversion(fomc_events: list[EconEvent]) -> None:
    # December: EST (UTC-5), 14:00 New York = 19:00 UTC.
    assert _at(fomc_events, datetime(2026, 12, 9, 19, 0, tzinfo=UTC))
    # March 17, 2027: EDT (UTC-4) already in effect, 14:00 New York = 18:00 UTC.
    assert _at(fomc_events, datetime(2027, 3, 17, 18, 0, tzinfo=UTC))
    # Apr/May span "30-1": decision is the last day, May 1, 2024 in EDT.
    assert _at(fomc_events, datetime(2024, 5, 1, 18, 0, tzinfo=UTC))
    # Jan/Feb span "31-1": decision Feb 1, 2023 in EST.
    assert _at(fomc_events, datetime(2023, 2, 1, 19, 0, tzinfo=UTC))


def test_fomc_sep_marked_and_metadata(fomc_events: list[EconEvent]) -> None:
    sep = next(e for e in fomc_events if e.scheduled_at == datetime(2026, 12, 9, 19, 0, tzinfo=UTC))
    assert sep.title == "FOMC Interest Rate Decision"
    assert sep.description and "SEP" in sep.description
    jan = next(e for e in fomc_events if e.scheduled_at == datetime(2026, 1, 28, 19, 0, tzinfo=UTC))
    assert jan.description is None
    assert jan.country == "US" and jan.currency == "USD"
    assert jan.importance == 3 and jan.category == "central_bank"
    assert jan.sources[0].provider == "fed"
    assert jan.sources[0].basis == "native"
    assert jan.sources[0].url == FED_URL


def test_fomc_skips_notation_votes(fomc_events: list[EconEvent]) -> None:
    # 2025 "August 22 (notation vote)" must not become a meeting.
    assert not [e for e in fomc_events if e.scheduled_at.date() == date(2025, 8, 22)]


def test_fomc_minutes_only_from_listed_release_dates(fomc_events: list[EconEvent]) -> None:
    minutes = _by_title(fomc_events, "FOMC Minutes")
    assert minutes
    feb = next(e for e in minutes if e.scheduled_at.date() == date(2026, 2, 18))
    assert feb.all_day is True
    assert feb.scheduled_at == datetime(2026, 2, 18, 0, 0, tzinfo=UTC)
    # Future minutes are not listed on the page, so none are invented.
    assert not [e for e in minutes if e.scheduled_at > NOW]


# ── ECB ───────────────────────────────────────────────────────────────────


@pytest.fixture()
def ecb_events(fixture_path: Callable[[str], Path]) -> list[EconEvent]:
    html = fixture_path("ecb_mp_calendar.html").read_text(encoding="utf-8-sig")
    return parse_ecb_calendar(html, fetched_at=NOW)


def test_ecb_future_decisions_and_dst(ecb_events: list[EconEvent]) -> None:
    future = [e for e in _by_title(ecb_events, "ECB Interest Rate Decision") if e.scheduled_at > NOW]
    assert len(future) >= 4
    # 17 Dec 2026, CET (UTC+1): 14:15 Frankfurt = 13:15 UTC.
    assert _at(ecb_events, datetime(2026, 12, 17, 13, 15, tzinfo=UTC))
    # 29 Apr 2027, CEST (UTC+2): 14:15 Frankfurt = 12:15 UTC.
    assert _at(ecb_events, datetime(2027, 4, 29, 12, 15, tzinfo=UTC))


def test_ecb_only_monetary_press_conference_days(ecb_events: list[EconEvent]) -> None:
    # 25 Nov 2026 is a non-monetary meeting; General Council meetings are not decisions.
    assert not [e for e in ecb_events if e.scheduled_at.date() == date(2026, 11, 25)]
    assert not [e for e in ecb_events if e.scheduled_at.date() == date(2026, 11, 26)]
    # Day 1 of each monetary meeting is not the decision day.
    assert not [e for e in ecb_events if e.scheduled_at.date() == date(2026, 12, 16)]
    assert len(ecb_events) == 17
    first = next(e for e in ecb_events if e.scheduled_at.date() == date(2026, 12, 17))
    assert first.country == "EU" and first.currency == "EUR"
    assert first.importance == 3 and first.category == "central_bank"
    assert first.sources[0].provider == "ecb" and first.sources[0].url == ECB_URL


# ── BoE ───────────────────────────────────────────────────────────────────


@pytest.fixture()
def boe_events(fixture_path: Callable[[str], Path]) -> list[EconEvent]:
    html = fixture_path("boe_mpc_dates.html").read_text(encoding="utf-8-sig")
    return parse_boe_mpc_dates(html, fetched_at=NOW)


def test_boe_dates_and_dst(boe_events: list[EconEvent]) -> None:
    assert len(boe_events) == 16
    future = [e for e in boe_events if e.scheduled_at > NOW]
    assert len(future) >= 4
    # 17 Dec 2026 (GMT): 12:00 London = 12:00 UTC.
    assert _at(boe_events, datetime(2026, 12, 17, 12, 0, tzinfo=UTC))
    # 29 Apr 2027 (BST, UTC+1): 12:00 London = 11:00 UTC.
    assert _at(boe_events, datetime(2027, 4, 29, 11, 0, tzinfo=UTC))
    event = boe_events[0]
    assert event.title == "BoE Interest Rate Decision"
    assert event.country == "GB" and event.currency == "GBP"
    assert event.sources[0].provider == "boe" and event.sources[0].url == BOE_URL


# ── BoJ ───────────────────────────────────────────────────────────────────


@pytest.fixture()
def boj_events(fixture_path: Callable[[str], Path]) -> list[EconEvent]:
    html = fixture_path("boj_mpm_schedule.html").read_text(encoding="utf-8-sig")
    return parse_boj_schedule(html, fetched_at=NOW)


def test_boj_last_day_all_day(boj_events: list[EconEvent]) -> None:
    assert len(boj_events) == 16
    assert all(e.all_day for e in boj_events)
    assert len([e for e in boj_events if e.scheduled_at > NOW]) >= 4
    assert _at(boj_events, datetime(2026, 1, 23, 0, 0, tzinfo=UTC))  # "Jan. 22, 23"
    assert _at(boj_events, datetime(2026, 12, 18, 0, 0, tzinfo=UTC))  # "Dec. 17, 18"
    assert _at(boj_events, datetime(2027, 1, 22, 0, 0, tzinfo=UTC))  # "Jan. 21, 22"
    assert _at(boj_events, datetime(2027, 3, 18, 0, 0, tzinfo=UTC))  # "Mar. 17 (Wed.), 18"
    event = boj_events[0]
    assert event.title == "BoJ Interest Rate Decision"
    assert event.country == "JP" and event.currency == "JPY"
    assert event.sources[0].provider == "boj" and event.sources[0].url == BOJ_URL


# ── curated ───────────────────────────────────────────────────────────────


def test_packaged_tcmb_schedule_is_verified_and_complete(markets_settings: MarketsSettings) -> None:
    events = load_curated_schedules(markets_settings.config_file("central_banks.yaml"), fetched_at=NOW)
    decisions = [e for e in events if e.title == "TCMB Interest Rate Decision"]
    summaries = [e for e in events if e.title == "TCMB MPC Summary"]
    assert len(decisions) == 12
    assert len(summaries) == 12
    # Turkiye is UTC+3 all year: 14:00 Istanbul = 11:00 UTC.
    assert all(e.scheduled_at.timetz().replace(tzinfo=None) == time(11, 0) for e in decisions)
    expected_dates = [
        (2026, 1, 22), (2026, 3, 12), (2026, 4, 22), (2026, 6, 11), (2026, 7, 23), (2026, 9, 10),
        (2026, 10, 22), (2026, 12, 10), (2027, 1, 21), (2027, 3, 18), (2027, 4, 22), (2027, 6, 10),
    ]
    assert [e.scheduled_at for e in decisions] == [datetime(y, m, d, 11, 0, tzinfo=UTC) for y, m, d in expected_dates]
    for event in decisions:
        assert event.country == "TR" and event.currency == "TRY"
        assert event.importance == 3 and event.category == "central_bank"
        assert event.sources[0].provider == "tcmb" and event.sources[0].basis == "curated"
        assert event.sources[0].url.endswith("2026_Para_Politikas%C4%B1_Metni.pdf?MOD=AJPERES")
        assert event.description and "2026-11-08" in event.description
    assert all(e.all_day and e.importance == 2 for e in summaries)


def test_curated_entry_needs_source_and_verification(tmp_path: Path) -> None:
    path = tmp_path / "curated.yaml"
    path.write_text(
        """
entries:
  - provider: tcmb
    country: TR
    currency: TRY
    title: TCMB Interest Rate Decision
    date: 2026-01-22
    time: "14:00"
    tz: Europe/Istanbul
    source_url: https://www.tcmb.gov.tr/example
    verified_on: 2026-11-08
  - provider: tcmb
    country: TR
    currency: TRY
    title: TCMB Interest Rate Decision
    date: 2026-03-12
    time: "14:00"
    tz: Europe/Istanbul
    source_url: https://www.tcmb.gov.tr/example
""",
        encoding="utf-8",
    )
    events = load_curated_schedules(path, fetched_at=NOW)
    assert len(events) == 1  # the second entry has no verified_on and is skipped
    event = events[0]
    assert event.scheduled_at == datetime(2026, 1, 22, 11, 0, tzinfo=UTC)  # 14:00 UTC+3
    assert event.sources[0].provider == "tcmb"
    assert event.sources[0].basis == "curated"
    assert event.sources[0].url == "https://www.tcmb.gov.tr/example"
    assert event.country == "TR" and event.importance == 3


# ── collector (MockTransport, no network) ─────────────────────────────────


async def _no_sleep(_seconds: float) -> None:
    return None


def _fetcher(settings: MarketsSettings, handler: Callable[[httpx.Request], httpx.Response]) -> Fetcher:
    hosts = {
        "www.federalreserve.gov": 0.0,
        "www.ecb.europa.eu": 0.0,
        "www.bankofengland.co.uk": 0.0,
        "www.boj.or.jp": 0.0,
    }
    return Fetcher(settings, transport=httpx.MockTransport(handler), sleep=_no_sleep, host_intervals=hosts)


def _ctx(settings: MarketsSettings, store: MarketsStore, http: Fetcher) -> CollectorContext:
    return CollectorContext(
        settings=settings,
        store=store,
        http=http,
        registry=None,  # type: ignore[arg-type]  # not used by this collector
        now=lambda: NOW,
    )


def _pages(fixture_path: Callable[[str], Path], *, fail_host: str | None = None) -> Callable[[httpx.Request], httpx.Response]:
    bodies = {
        "www.federalreserve.gov": fixture_path("fomc_calendars.html").read_bytes(),
        "www.ecb.europa.eu": fixture_path("ecb_mp_calendar.html").read_bytes(),
        "www.bankofengland.co.uk": fixture_path("boe_mpc_dates.html").read_bytes(),
        "www.boj.or.jp": fixture_path("boj_mpm_schedule.html").read_bytes(),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == fail_host:
            return httpx.Response(500, text="down")
        return httpx.Response(200, content=bodies[host])

    return handler


async def test_central_bank_collector_stores_all_pages(
    markets_settings: MarketsSettings, store: MarketsStore, fixture_path: Callable[[str], Path]
) -> None:
    http = _fetcher(markets_settings, _pages(fixture_path))
    try:
        result = await CentralBankCollector().run(_ctx(markets_settings, store, http))
    finally:
        await http.aclose()
    assert result.items > 0
    stored = store.query_econ(datetime(2023, 1, 1, tzinfo=UTC), datetime(2028, 12, 31, tzinfo=UTC))
    providers = {s.provider for e in stored for s in e.sources}
    assert providers == {"fed", "ecb", "boe", "boj", "tcmb"}
    assert CentralBankCollector.name == "econ.central_banks"
    assert CentralBankCollector.interval_s == 86400


async def test_central_bank_collector_tolerates_one_failed_page(
    markets_settings: MarketsSettings, store: MarketsStore, fixture_path: Callable[[str], Path]
) -> None:
    http = _fetcher(markets_settings, _pages(fixture_path, fail_host="www.bankofengland.co.uk"))
    try:
        result = await CentralBankCollector().run(_ctx(markets_settings, store, http))
    finally:
        await http.aclose()
    assert result.items > 0
    stored = store.query_econ(datetime(2023, 1, 1, tzinfo=UTC), datetime(2028, 12, 31, tzinfo=UTC))
    providers = {s.sources[0].provider for s in stored}
    assert "boe" not in providers
    assert {"fed", "ecb", "boj"} <= providers


async def test_central_bank_collector_raises_when_all_pages_fail(
    markets_settings: MarketsSettings, store: MarketsStore
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    http = _fetcher(markets_settings, handler)
    try:
        with pytest.raises(FetchError):
            await CentralBankCollector().run(_ctx(markets_settings, store, http))
    finally:
        await http.aclose()


async def test_empty_page_body_is_skipped_and_collector_continues(
    markets_settings: MarketsSettings, store: MarketsStore, fixture_path: Callable[[str], Path]
) -> None:
    pages = _pages(fixture_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.bankofengland.co.uk":
            return httpx.Response(200, content=b"   \n")  # 200 with an empty body
        return pages(request)

    http = _fetcher(markets_settings, handler)
    try:
        result = await CentralBankCollector().run(_ctx(markets_settings, store, http))
    finally:
        await http.aclose()
    assert result.items > 0
    stored = store.query_econ(datetime(2023, 1, 1, tzinfo=UTC), datetime(2028, 12, 31, tzinfo=UTC))
    providers = {s.sources[0].provider for s in stored}
    assert "boe" not in providers
    assert {"fed", "ecb", "boj", "tcmb"} <= providers
