"""Economic calendar: value parsing, provider parsers, merge, finalize, collectors."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from awareness.markets.calendar.econ_common import classify
from awareness.markets.calendar.econ_merge import MergeState
from awareness.markets.calendar.economic import (
    BeaCollector,
    ForexFactoryCollector,
    NasdaqEconCollector,
    event_id,
    finalize_event,
    merge_econ_events,
    normalize_title,
    parse_bea_release_dates,
    parse_forexfactory,
    parse_nasdaq_econ,
    parse_value,
    reclassify_stored,
    store_merge,
)
from awareness.markets.collectors.base import CollectorContext
from awareness.markets.config import MarketsSettings
from awareness.markets.http import Fetcher, FetchError
from awareness.markets.models import EconEvent, ProviderRef
from awareness.markets.store import MarketsStore

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
NASDAQ_DAY = date(2026, 11, 10)


def _ref(provider: str, fetched: datetime = NOW) -> ProviderRef:
    return ProviderRef(provider=provider, url=f"https://{provider}.example/", fetched_at=fetched)


def _event(title: str, country: str, at: datetime, provider: str, **kw: Any) -> EconEvent:
    return EconEvent(
        id=event_id(country, title, at),
        country=country,
        title=title,
        scheduled_at=at,
        sources=[_ref(provider)],
        **kw,
    )


# ── parse_value ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected_value", "expected_unit"),
    [
        ("250K", 250.0, "K"),
        ("250k", 250.0, "K"),
        ("-1.2B", -1.2, "B"),
        ("75.43B", 75.43, "B"),
        ("7.15M", 7.15, "M"),
        ("2.81T", 2.81, "T"),
        ("1.2 M", 1.2, "M"),
        ("0.3%", 0.3, "%"),
        ("+0.4%", 0.4, "%"),
        ("\u22121.2%", -1.2, "%"),
        ("&nbsp;", None, None),
        ("\xa0", None, None),
        ("(0.5)%", -0.5, "%"),
        ("(0.3)%", -0.3, "%"),
        ("1,234.5", 1234.5, None),
        ("52.0", 52.0, None),
        ("<0.1%", 0.1, "%"),
        ("$1.2B", 1.2, "B"),
        ("€3.5", 3.5, "€"),
        ("$50", 50.0, "$"),
        ("-", None, None),
        ("N/A", None, None),
        ("", None, None),
        (None, None, None),
        ("abc", None, None),
        ("5.42|3.6", None, None),
        ("7-0-2", None, None),
    ],
)
def test_parse_value_table(raw: str | None, expected_value: float | None, expected_unit: str | None) -> None:
    value, unit = parse_value(raw)
    assert unit == expected_unit
    if expected_value is None:
        assert value is None
    else:
        assert value == pytest.approx(expected_value)


def test_parse_value_never_multiplies_suffix() -> None:
    assert parse_value("250K") == (250.0, "K")
    assert parse_value("1.5B")[0] == 1.5


# ── normalize_title / event_id ────────────────────────────────────────────


def test_normalize_title_maps_changes_and_strips_noise() -> None:
    assert normalize_title("CPI m/m") == "cpi mom"
    assert normalize_title("Core CPI Flash Estimate y/y") == "core cpi flash estimate yoy"
    assert normalize_title("GDP q/q (prel)") == "gdp qoq"
    assert normalize_title("Final Manufacturing PMI") == "final manufacturing pmi"
    assert normalize_title("Trade   Balance (final)") == "trade balance"


def test_event_id_is_deterministic_on_normalized_title() -> None:
    at = datetime(2026, 11, 10, 13, 30, tzinfo=UTC)
    assert event_id("US", "CPI m/m", at) == event_id("US", "cpi  MoM", at)
    assert event_id("US", "CPI m/m", at) != event_id("EU", "CPI m/m", at)
    assert event_id("US", "CPI m/m", at) != event_id("US", "CPI m/m", at + timedelta(days=1))


# ── Nasdaq ────────────────────────────────────────────────────────────────


@pytest.fixture()
def nasdaq_payload(fixture_path: Callable[[str], Path]) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(fixture_path("nasdaq_econ_2026-11-10.json").read_text(encoding="utf-8"))
    return payload


@pytest.fixture()
def nasdaq_events(nasdaq_payload: dict[str, Any]) -> list[EconEvent]:
    return parse_nasdaq_econ(nasdaq_payload, day=NASDAQ_DAY, fetched_at=NOW)


def test_nasdaq_event_count_and_unique_ids(nasdaq_events: list[EconEvent]) -> None:
    assert len(nasdaq_events) >= 30
    assert len({e.id for e in nasdaq_events}) == len(nasdaq_events)


def test_nasdaq_countries_mapped(nasdaq_events: list[EconEvent]) -> None:
    countries = {e.country for e in nasdaq_events}
    assert {"JP", "US", "DE", "EU", "ZA", "CA", "BR", "FR", "GB", "AU"} <= countries
    assert "Euro Zone" not in countries
    de = next(e for e in nasdaq_events if e.country == "DE")
    assert de.currency == "EUR"
    eu = next(e for e in nasdaq_events if e.country == "EU")
    assert eu.currency == "EUR"


def test_nasdaq_leading_index_duplicates_have_distinct_ids(nasdaq_events: list[EconEvent]) -> None:
    leading = [e for e in nasdaq_events if e.country == "JP" and e.title.startswith("Leading Index")]
    assert len(leading) == 2
    assert leading[0].id != leading[1].id


def test_nasdaq_gmt_is_new_york_wall_clock_converted_to_utc(nasdaq_events: list[EconEvent]) -> None:
    # Nasdaq's "gmt" field is America/New_York local time (EST on 2026-11-10): 18:30 ET = 23:30 UTC.
    spend = [e for e in nasdaq_events if e.title.startswith("Household Spending")]
    assert len(spend) == 2
    assert all(e.scheduled_at == datetime(2026, 11, 10, 23, 30, tzinfo=UTC) for e in spend)
    reserves = next(e for e in nasdaq_events if e.title == "Foreign Reserves (USD)")
    assert reserves.scheduled_at == datetime(2026, 11, 10, 6, 0, tzinfo=UTC)  # 01:00 ET
    assert reserves.all_day is False
    wholesale = next(e for e in nasdaq_events if e.title == "Wholesale Inventories")
    assert wholesale.scheduled_at == datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # 10:00 ET
    coincident = next(e for e in nasdaq_events if e.title == "Coincident Indicator")
    assert coincident.all_day is True
    assert coincident.scheduled_at == datetime(2026, 11, 10, 0, 0, tzinfo=UTC)


def test_nasdaq_values_keep_text_and_parse_numbers(nasdaq_events: list[EconEvent]) -> None:
    de_bal = next(e for e in nasdaq_events if e.title == "German Trade Balance")
    assert de_bal.forecast == "19.2B"
    assert de_bal.forecast_value == pytest.approx(19.2)
    assert de_bal.previous == "19.5B"
    assert de_bal.previous_value == pytest.approx(19.5)
    assert de_bal.actual is None and de_bal.actual_value is None
    assert de_bal.unit == "B"
    de_exp = next(e for e in nasdaq_events if e.title == "German Exports")
    assert de_exp.forecast is None
    assert de_exp.previous_value == pytest.approx(-0.8)
    assert de_exp.unit == "%"


def test_nasdaq_description_is_clean(nasdaq_events: list[EconEvent]) -> None:
    desc = next(e for e in nasdaq_events if e.title == "Coincident Indicator").description
    assert desc is not None
    assert len(desc) <= 600
    for bad in ("<", ">", "&nbsp;", "\r", "\n", "  "):
        assert bad not in desc


def test_nasdaq_provenance_and_importance(nasdaq_events: list[EconEvent]) -> None:
    ev = nasdaq_events[0]
    assert ev.sources[0].provider == "nasdaq"
    assert ev.sources[0].url == "https://api.nasdaq.com/api/calendar/economicevents?date=2026-11-10"
    assert ev.sources[0].fetched_at == NOW
    bailey = next(e for e in nasdaq_events if "Bailey" in e.title)
    assert bailey.importance == 3
    bowman = next(e for e in nasdaq_events if "Bowman" in e.title)
    assert bowman.importance == 2
    assert bowman.category == "speech"
    assert next(e for e in nasdaq_events if e.title == "Atlanta Fed GDPNow").importance == 1


# ── ForexFactory ──────────────────────────────────────────────────────────


@pytest.fixture()
def ff_payload(fixture_path: Callable[[str], Path]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = json.loads(fixture_path("ff_thisweek.json").read_text(encoding="utf-8"))
    return payload


@pytest.fixture()
def ff_events(ff_payload: list[dict[str, Any]]) -> list[EconEvent]:
    return parse_forexfactory(ff_payload, fetched_at=NOW)


def test_ff_skips_holidays(ff_events: list[EconEvent]) -> None:
    titles = {e.title for e in ff_events}
    assert "Daylight Saving Time Shift" not in titles
    assert "Bank Holiday" not in titles
    assert len(ff_events) == 116 - 3


def test_ff_importance_mapping_and_table_max(ff_events: list[EconEvent]) -> None:
    ism = next(e for e in ff_events if e.title == "ISM Manufacturing PMI")
    assert ism.importance == 3  # FF Medium=2, table "ism manufacturing"=3 -> max
    nfp = next(e for e in ff_events if e.title == "Non-Farm Employment Change")
    assert nfp.importance == 3  # FF High
    cash = next(e for e in ff_events if e.title == "Cash Rate")
    assert cash.importance == 3  # FF High; table has no rule
    loan = next(e for e in ff_events if e.title == "Loan Officer Survey")
    assert loan.importance == 1


def test_ff_utc_conversion_from_offset(ff_events: list[EconEvent]) -> None:
    cash = next(e for e in ff_events if e.title == "Cash Rate")
    assert cash.scheduled_at == datetime(2026, 11, 3, 3, 30, tzinfo=UTC)
    assert cash.country == "AU"
    assert cash.currency == "AUD"
    assert cash.sources[0].provider == "forexfactory"
    assert cash.all_day is False


def test_ff_values_and_units(ff_events: list[EconEvent]) -> None:
    cash = next(e for e in ff_events if e.title == "Cash Rate")
    assert cash.forecast == "4.85%"
    assert cash.forecast_value == pytest.approx(4.85)
    assert cash.previous_value == pytest.approx(4.60)
    claims = next(e for e in ff_events if e.title == "Unemployment Claims")
    assert claims.forecast_value == pytest.approx(197.0)
    assert claims.unit == "K"
    votes = next(e for e in ff_events if e.title == "MPC Official Bank Rate Votes")
    assert votes.forecast == "7-0-2"
    assert votes.forecast_value is None


# ── BEA ───────────────────────────────────────────────────────────────────


def test_bea_window_filter_and_importance(fixture_path: Callable[[str], Path]) -> None:
    payload: dict[str, Any] = json.loads(fixture_path("bea_release_dates.json").read_text(encoding="utf-8"))
    start, end = date(2026, 11, 1), date(2026, 12, 31)
    events = parse_bea_release_dates(payload, fetched_at=NOW, start=start, end=end)

    expected: set[tuple[str, str]] = set()
    for name, info in payload.items():
        if not isinstance(info, dict):  # "file_last_updated" metadata
            continue
        for raw in info["release_dates"]:
            d = datetime.fromisoformat(raw).astimezone(UTC).date()
            if start <= d <= end:
                expected.add((name, d.isoformat()))
    got = {(e.title, e.scheduled_at.date().isoformat()) for e in events}
    assert got == expected
    assert len(events) == len(expected)
    assert all(start <= e.scheduled_at.date() <= end for e in events)
    assert all(e.country == "US" and e.currency == "USD" for e in events)
    assert all(e.sources[0].provider == "bea" and e.sources[0].basis == "native" for e in events)
    gdp = [e for e in events if e.title == "Gross Domestic Product"]
    assert gdp and all(e.importance == 3 for e in gdp)


# ── finalize_event ────────────────────────────────────────────────────────


def test_finalize_surprise_direction_and_status() -> None:
    at = datetime(2026, 11, 6, 13, 30, tzinfo=UTC)
    ev = _event("Unemployment Rate", "US", at, "nasdaq", actual="4.3%", actual_value=4.3,
                forecast="4.2%", forecast_value=4.2, unit="%")
    done = finalize_event(ev, now=NOW)
    assert done.surprise == pytest.approx(0.1)
    assert done.surprise_direction == "above"
    assert done.status == "released"
    inline = finalize_event(ev.model_copy(update={"actual": "4.2%", "actual_value": 4.2}), now=NOW)
    assert inline.surprise_direction == "inline"
    cancelled = finalize_event(ev.model_copy(update={"status": "cancelled"}), now=NOW)
    assert cancelled.status == "cancelled"


def test_surprise_only_with_equal_units() -> None:
    at = datetime(2026, 11, 6, 13, 30, tzinfo=UTC)
    mismatch = _event("Nonfarm Payrolls", "US", at, "nasdaq", actual="250K", actual_value=250.0,
                      forecast="0.3M", forecast_value=0.3, unit="K")
    assert finalize_event(mismatch, now=NOW).surprise is None
    same = _event("Nonfarm Payrolls", "US", at, "nasdaq", actual="250K", actual_value=250.0,
                  forecast="250K", forecast_value=250.0, unit="K")
    fin = finalize_event(same, now=NOW)
    assert fin.surprise == pytest.approx(0.0)
    assert fin.surprise_direction == "inline"
    big = _event("Trade Balance", "US", at, "nasdaq", actual="1.2B", actual_value=1.2,
                 forecast="1.1B", forecast_value=1.1, unit="B")
    assert finalize_event(big, now=NOW).surprise == pytest.approx(0.1)


# ── merge ─────────────────────────────────────────────────────────────────


def test_merge_nasdaq_and_ff_same_event_one_record() -> None:
    at = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
    nas = _event("ISM Manufacturing PMI", "US", at, "nasdaq", forecast="54.5", forecast_value=54.5,
                 importance=1, category="pmi")
    ff = _event("ISM Manufacturing PMI", "US", at, "forexfactory", forecast="54.0",
                forecast_value=54.0, previous="54.5", previous_value=54.5, importance=3)
    merged = merge_econ_events([], [nas, ff], now=NOW)
    assert len(merged) == 1
    ev = merged[0]
    assert ev.forecast == "54.5" and ev.forecast_value == pytest.approx(54.5)  # Nasdaq wins
    assert ev.previous_value == pytest.approx(54.5)  # from FF where Nasdaq is None
    assert ev.importance == 3
    assert {s.provider for s in ev.sources} == {"nasdaq", "forexfactory"}
    assert ev.updated_at == NOW


def test_merge_forecast_falls_back_to_ff_when_nasdaq_empty() -> None:
    at = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
    nas = _event("ISM Manufacturing PMI", "US", at, "nasdaq")
    ff = _event("ISM Manufacturing PMI", "US", at, "forexfactory", forecast="54.0", forecast_value=54.0)
    ev = merge_econ_events([], [nas, ff], now=NOW)[0]
    assert ev.forecast == "54.0"
    assert ev.forecast_value == pytest.approx(54.0)


def test_merge_is_idempotent() -> None:
    at = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
    nas = _event("ISM Manufacturing PMI", "US", at, "nasdaq", forecast="54.5", forecast_value=54.5)
    ff = _event("ISM Manufacturing PMI", "US", at, "forexfactory", importance=3)
    first = merge_econ_events([], [nas, ff], now=NOW)
    assert merge_econ_events(first, [nas, ff], now=NOW + timedelta(hours=1)) == []


def test_merge_fuzzy_match_keeps_existing_id() -> None:
    at = datetime(2026, 11, 6, 13, 30, tzinfo=UTC)
    stored = _event("Retail Sales ex Autos m/m", "US", at, "nasdaq")
    incoming = _event("Retail Sales m/m", "US", at + timedelta(minutes=30), "forexfactory", importance=3)
    merged = merge_econ_events([stored], [incoming], now=NOW)
    assert len(merged) == 1
    assert merged[0].id == stored.id
    assert merged[0].importance == 3


def test_merge_does_not_fuzzy_match_far_apart_times() -> None:
    at = datetime(2026, 11, 6, 13, 30, tzinfo=UTC)
    stored = _event("Retail Sales ex Autos m/m", "US", at, "nasdaq")
    far = _event("Retail Sales m/m", "US", at + timedelta(hours=3), "forexfactory")
    merged = merge_econ_events([stored], [far], now=NOW)
    assert len(merged) == 1
    assert merged[0].id == far.id


def test_merge_never_replaces_timed_with_all_day() -> None:
    timed = datetime(2026, 11, 6, 13, 30, tzinfo=UTC)
    stored = _event("Unemployment Rate", "US", timed, "forexfactory")
    all_day = datetime(2026, 11, 6, 0, 0, tzinfo=UTC)
    incoming = _event("Unemployment Rate", "US", all_day, "nasdaq", all_day=True)
    merged = merge_econ_events([stored], [incoming], now=NOW)
    assert merged[0].scheduled_at == timed
    assert merged[0].all_day is False


def test_merge_surprise_never_multiplies_k() -> None:
    at = datetime(2026, 11, 6, 13, 30, tzinfo=UTC)
    nas = _event("Unemployment Claims", "US", at, "nasdaq", actual="197K", actual_value=197.0,
                 forecast="194K", forecast_value=194.0, unit="K")
    merged = merge_econ_events([], [nas], now=NOW)[0]
    assert merged.surprise == pytest.approx(3.0)
    assert merged.actual_value == pytest.approx(197.0)


# ── collectors (MockTransport, no network) ────────────────────────────────


async def _no_sleep(_seconds: float) -> None:
    return None


def _fetcher(settings: MarketsSettings, handler: Callable[[httpx.Request], httpx.Response]) -> Fetcher:
    return Fetcher(
        settings,
        transport=httpx.MockTransport(handler),
        sleep=_no_sleep,
        host_intervals={"api.nasdaq.com": 0.0, "nfs.faireconomy.media": 0.0, "apps.bea.gov": 0.0},
    )


def _ctx(settings: MarketsSettings, store: MarketsStore, http: Fetcher) -> CollectorContext:
    return CollectorContext(
        settings=settings,
        store=store,
        http=http,
        registry=None,  # type: ignore[arg-type]  # collectors here do not use the registry
        now=lambda: NOW,
    )


async def test_nasdaq_collector_tolerates_one_failing_day(
    markets_settings: MarketsSettings, store: MarketsStore, nasdaq_payload: dict[str, Any]
) -> None:
    fail_day = "2026-11-11"

    def handler(request: httpx.Request) -> httpx.Response:
        day = request.url.params["date"]
        if day == fail_day:
            return httpx.Response(500, text="boom")
        if day == NASDAQ_DAY.isoformat():
            return httpx.Response(200, json=nasdaq_payload)
        return httpx.Response(200, json={"data": {"rows": []}})

    http = _fetcher(markets_settings, handler)
    try:
        result = await NasdaqEconCollector().run(_ctx(markets_settings, store, http))
    finally:
        await http.aclose()
    assert result.items >= 30
    stored = store.query_econ(datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 12, 31, tzinfo=UTC))
    assert len(stored) >= 30
    assert all(e.sources and e.sources[0].provider == "nasdaq" for e in stored)


async def test_nasdaq_collector_raises_when_all_days_fail(
    markets_settings: MarketsSettings, store: MarketsStore
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    http = _fetcher(markets_settings, handler)
    try:
        with pytest.raises(FetchError):
            await NasdaqEconCollector().run(_ctx(markets_settings, store, http))
    finally:
        await http.aclose()


async def test_ff_and_bea_collectors_store_events(
    markets_settings: MarketsSettings, store: MarketsStore, fixture_path: Callable[[str], Path],
) -> None:
    ff_raw = fixture_path("ff_thisweek.json").read_bytes()
    bea_raw = fixture_path("bea_release_dates.json").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "nfs.faireconomy.media":
            return httpx.Response(200, content=ff_raw)
        return httpx.Response(200, content=bea_raw)

    http = _fetcher(markets_settings, handler)
    try:
        ff = await ForexFactoryCollector().run(_ctx(markets_settings, store, http))
        bea = await BeaCollector().run(_ctx(markets_settings, store, http))
    finally:
        await http.aclose()
    assert ff.items == 113
    assert bea.items > 0
    assert ForexFactoryCollector.name == "econ.forexfactory"
    assert ForexFactoryCollector.interval_s == 3600
    assert BeaCollector.interval_s == 86400
    assert NasdaqEconCollector.interval_s == 1800
    assert all(c.lock_group == "econ" for c in (NasdaqEconCollector, ForexFactoryCollector, BeaCollector))


# ── fix round 1 ───────────────────────────────────────────────────────────

AT = datetime(2026, 11, 13, 13, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("title", "importance", "category"),
    [
        ("CPI m/m", 3, "inflation"),
        ("CPI Flash Estimate y/y", 3, "inflation"),
        ("Core CPI", 3, "inflation"),
        ("GDP q/q", 3, "growth"),
        ("GDP Growth Rate", 3, "growth"),
        ("Manufacturing PMI", 2, "pmi"),
        ("PPI m/m", 2, "inflation"),
        ("JOLTS Job Openings", 2, "employment"),
        ("ZEW Economic Sentiment", 2, "sentiment"),
        ("Nonfarm Payrolls", 3, "employment"),
        ("Non-Farm Payrolls", 3, "employment"),
        ("Federal Funds Rate", 3, "central_bank"),
        ("MPC Official Bank Rate Votes", 1, "other"),
    ],
)
def test_rule_table_regexes_take_effect(title: str, importance: int, category: str) -> None:
    assert classify(title) == (importance, category)


def _snap(
    provider: str,
    *,
    fetched: datetime = NOW,
    forecast: str | None = None,
    actual: str | None = None,
    previous: str | None = None,
    title: str = "CPI m/m",
    country: str = "US",
) -> EconEvent:
    return EconEvent(
        id=event_id(country, title, AT),
        country=country,
        title=title,
        scheduled_at=AT,
        forecast=forecast,
        forecast_value=parse_value(forecast)[0],
        actual=actual,
        actual_value=parse_value(actual)[0],
        previous=previous,
        previous_value=parse_value(previous)[0],
        sources=[ProviderRef(provider=provider, url=f"https://{provider}.example/", fetched_at=fetched)],
    )


def _stored(events: list[EconEvent]) -> EconEvent:
    assert len(events) == 1
    return events[0]


def test_same_provider_refresh_replaces_forecast() -> None:
    state = MergeState()
    first = _stored(merge_econ_events([], [_snap("forexfactory", forecast="0.3%")], now=NOW, state=state))
    later = NOW + timedelta(hours=1)
    changed = merge_econ_events(
        [first], [_snap("forexfactory", forecast="0.4%", fetched=later)], now=later, state=state
    )
    assert _stored(changed).forecast == "0.4%"
    assert _stored(changed).forecast_value == pytest.approx(0.4)


def test_higher_ranked_provider_replaces_forecast() -> None:
    state = MergeState()
    first = _stored(merge_econ_events([], [_snap("forexfactory", forecast="0.3%")], now=NOW, state=state))
    changed = merge_econ_events([first], [_snap("nasdaq", forecast="0.5%")], now=NOW, state=state)
    ev = _stored(changed)
    assert ev.forecast == "0.5%"
    assert ev.forecast_value == pytest.approx(0.5)
    assert {s.provider for s in ev.sources} == {"forexfactory", "nasdaq"}


def test_same_provider_revision_replaces_actual() -> None:
    state = MergeState()
    first = _stored(merge_econ_events([], [_snap("nasdaq", actual="0.2%")], now=NOW, state=state))
    later = NOW + timedelta(hours=1)
    changed = merge_econ_events([first], [_snap("nasdaq", actual="0.25%", fetched=later)], now=later, state=state)
    assert _stored(changed).actual == "0.25%"
    assert _stored(changed).actual_value == pytest.approx(0.25)


def test_lower_ranked_provider_fills_only_empty_fields() -> None:
    state = MergeState()
    first = _stored(merge_econ_events([], [_snap("nasdaq", forecast="0.5%")], now=NOW, state=state))
    changed = merge_econ_events(
        [first], [_snap("forexfactory", forecast="0.9%", actual="0.4%")], now=NOW, state=state
    )
    ev = _stored(changed)
    assert ev.forecast == "0.5%"  # Nasdaq value kept
    assert ev.actual == "0.4%"  # empty field filled from ForexFactory


def test_scheduled_becomes_released_with_two_nasdaq_fetches() -> None:
    state = MergeState()
    first = _stored(merge_econ_events([], [_snap("nasdaq", forecast="0.3%")], now=NOW, state=state))
    assert first.status == "scheduled"
    assert first.surprise is None
    later = NOW + timedelta(hours=1)
    changed = merge_econ_events(
        [first], [_snap("nasdaq", forecast="0.3%", actual="0.35%", fetched=later)], now=later, state=state
    )
    ev = _stored(changed)
    assert ev.status == "released"
    assert ev.surprise == pytest.approx(0.05)
    assert ev.surprise_direction == "above"


def test_fuzzy_pair_second_run_is_empty_and_store_keeps_one_row(
    markets_settings: MarketsSettings, store: MarketsStore
) -> None:
    at = datetime(2026, 11, 6, 13, 30, tzinfo=UTC)
    nas = _event("Retail Sales ex Autos m/m", "US", at, "nasdaq")
    ff = _event("Retail Sales m/m", "US", at + timedelta(minutes=30), "forexfactory", importance=3)
    ctx = CollectorContext(settings=markets_settings, store=store, http=None, registry=None,  # type: ignore[arg-type]
                           now=lambda: NOW)
    start, end = datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 30, tzinfo=UTC)
    first = store_merge(ctx, [nas, ff], start, end, NOW)
    assert len(first) == 1
    assert len(store.query_econ(start, end)) == 1
    assert store_merge(ctx, [nas, ff], start, end, NOW + timedelta(hours=1)) == []
    assert store_merge(ctx, [nas], start, end, NOW + timedelta(hours=2)) == []
    assert store_merge(ctx, [ff], start, end, NOW + timedelta(hours=3)) == []
    rows = store.query_econ(start, end)
    assert len(rows) == 1
    assert rows[0].importance == 3


def test_ff_euro_rows_take_member_country_from_title() -> None:
    rows = [
        {"title": "German Retail Sales m/m", "country": "EUR", "date": "2026-11-10T03:00:00+01:00",
         "impact": "Low", "forecast": "0.4%", "previous": "1.3%"},
        {"title": "Final Manufacturing PMI", "country": "EUR", "date": "2026-11-10T10:00:00+01:00",
         "impact": "Low", "forecast": "52.4", "previous": "52.4"},
        {"title": "ECB President Lagarde Speaks", "country": "EUR", "date": "2026-11-10T14:00:00+01:00",
         "impact": "Medium", "forecast": "", "previous": ""},
    ]
    events = {e.title: e for e in parse_forexfactory(rows, fetched_at=NOW)}
    de = events["German Retail Sales m/m"]
    assert de.country == "DE" and de.currency == "EUR"
    assert events["Final Manufacturing PMI"].country == "EU"
    assert events["ECB President Lagarde Speaks"].country == "EU"


def test_ff_german_row_merges_with_nasdaq_de_row(fixture_path: Callable[[str], Path]) -> None:
    payload: dict[str, Any] = json.loads(fixture_path("nasdaq_econ_2026-11-10.json").read_text(encoding="utf-8"))
    nasdaq = next(e for e in parse_nasdaq_econ(payload, day=NASDAQ_DAY, fetched_at=NOW)
                  if e.title == "German Trade Balance")
    ff = parse_forexfactory(
        [{"title": "German Trade Balance", "country": "EUR", "date": "2026-11-10T08:00:00+01:00",  # 07:00 UTC
          "impact": "Medium", "forecast": "19.0B", "previous": "19.5B"}],
        fetched_at=NOW,
    )[0]
    assert nasdaq.country == "DE" and ff.country == "DE"
    changed = merge_econ_events([nasdaq], [ff], now=NOW)
    assert len(changed) == 1
    assert {s.provider for s in changed[0].sources} == {"nasdaq", "forexfactory"}
    assert changed[0].forecast == "19.2B"  # Nasdaq consensus kept over ForexFactory


def test_central_bank_decision_merges_with_ff_rate_row_in_either_order() -> None:
    at = datetime(2026, 12, 9, 19, 0, tzinfo=UTC)
    cb = EconEvent(
        id=event_id("US", "FOMC Interest Rate Decision", at),
        country="US", currency="USD", title="FOMC Interest Rate Decision", category="central_bank",
        scheduled_at=at, importance=3,
        sources=[ProviderRef(provider="fed", url="https://www.federalreserve.gov/", fetched_at=NOW, basis="native")],
    )
    ff = parse_forexfactory(
        [{"title": "Federal Funds Rate", "country": "USD", "date": "2026-12-09T14:00:00-05:00",
          "impact": "High", "forecast": "3.75%", "previous": "3.50%"}],
        fetched_at=NOW,
    )[0]
    for incoming in ([cb, ff], [ff, cb]):
        changed = merge_econ_events([], incoming, now=NOW)
        assert len(changed) == 1
        ev = changed[0]
        assert ev.title == "FOMC Interest Rate Decision"
        assert ev.scheduled_at == at
        assert ev.forecast == "3.75%" and ev.forecast_value == pytest.approx(3.75)
        assert {s.provider for s in ev.sources} == {"fed", "forexfactory"}


def test_nasdaq_rejects_non_object_payload() -> None:
    with pytest.raises(ValueError):
        parse_nasdaq_econ([], day=NASDAQ_DAY, fetched_at=NOW)  # type: ignore[arg-type]


# ── fix round 2 ───────────────────────────────────────────────────────────


def _latest(current: EconEvent, changed: list[EconEvent]) -> EconEvent:
    return changed[0] if changed else current


def test_refresh_from_one_provider_never_clears_another_providers_actual() -> None:
    state = MergeState()
    t = NOW
    current = _stored(merge_econ_events([], [_snap("nasdaq", forecast="0.5%")], now=t, state=state))
    assert current.status == "scheduled"

    t += timedelta(minutes=30)  # FF fills the actual
    current = _latest(current, merge_econ_events(
        [current], [_snap("forexfactory", forecast="0.9%", actual="0.4%", fetched=t)], now=t, state=state))
    assert current.actual == "0.4%" and current.forecast == "0.5%" and current.status == "released"

    steps = [
        ("nasdaq", "0.5%"),          # step 3: Nasdaq refresh, no actual
        ("forexfactory", "0.9%"),    # step 4: FF refresh, actual again
        ("nasdaq", "0.5%"),          # step 5: Nasdaq refresh, no actual
    ]
    for provider, forecast in steps:
        t += timedelta(minutes=30)
        changed = merge_econ_events(
            [current], [_snap(provider, forecast=forecast, fetched=t)], now=t, state=state)
        current = _latest(current, changed)
        assert current.actual == "0.4%", provider
        assert current.actual_value == pytest.approx(0.4), provider
        assert current.status == "released", provider
        assert current.forecast == "0.5%", provider


def test_nasdaq_actual_overrides_ff_actual() -> None:
    state = MergeState()
    t = NOW
    current = _stored(merge_econ_events([], [_snap("nasdaq", forecast="0.5%")], now=t, state=state))
    t += timedelta(minutes=30)
    current = _latest(current, merge_econ_events(
        [current], [_snap("forexfactory", actual="0.4%", fetched=t)], now=t, state=state))
    assert current.actual == "0.4%"
    t += timedelta(minutes=30)
    current = _latest(current, merge_econ_events(
        [current], [_snap("nasdaq", forecast="0.5%", actual="0.45%", fetched=t)], now=t, state=state))
    assert current.actual == "0.45%"
    assert current.actual_value == pytest.approx(0.45)
    assert current.status == "released"


@pytest.mark.parametrize("fed_first", [True, False])
def test_timed_ff_minutes_beats_all_day_fed_minutes(fed_first: bool) -> None:
    """Only rate decisions let the central bank set the time; minutes keep the timed value."""
    day = datetime(2026, 12, 1, 0, 0, tzinfo=UTC)
    fed = EconEvent(
        id=event_id("US", "FOMC Minutes", day),
        country="US", currency="USD", title="FOMC Minutes", category="central_bank",
        scheduled_at=day, all_day=True, importance=3,
        sources=[ProviderRef(provider="fed", url="https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
                             fetched_at=NOW, basis="native")],
    )
    ff = parse_forexfactory(
        [{"title": "FOMC Meeting Minutes", "country": "USD", "date": "2026-12-01T14:00:00-05:00",
          "impact": "High", "forecast": "", "previous": ""}],
        fetched_at=NOW,
    )[0]
    assert ff.scheduled_at == datetime(2026, 12, 1, 19, 0, tzinfo=UTC)
    incoming = [fed, ff] if fed_first else [ff, fed]
    changed = merge_econ_events([], incoming, now=NOW)
    assert len(changed) == 1
    ev = changed[0]
    assert ev.all_day is False
    assert ev.scheduled_at == datetime(2026, 12, 1, 19, 0, tzinfo=UTC)
    assert {s.provider for s in ev.sources} == {"fed", "forexfactory"}


@pytest.mark.parametrize("cb_first", [True, False])
def test_all_day_boj_decision_keeps_its_date_when_merged_with_ff_rate(cb_first: bool) -> None:
    at = datetime(2026, 12, 18, 0, 0, tzinfo=UTC)
    boj = EconEvent(
        id=event_id("JP", "BoJ Interest Rate Decision", at),
        country="JP", currency="JPY", title="BoJ Interest Rate Decision", category="central_bank",
        scheduled_at=at, all_day=True, importance=3,
        sources=[ProviderRef(provider="boj", url="https://www.boj.or.jp/en/mopo/mpmsche_minu/index.htm",
                             fetched_at=NOW, basis="native")],
    )
    ff = parse_forexfactory(
        [{"title": "BOJ Policy Rate", "country": "JPY", "date": "2026-12-18T08:00:00+00:00",
          "impact": "High", "forecast": "0.75%", "previous": "0.50%"}],
        fetched_at=NOW,
    )[0]
    incoming = [boj, ff] if cb_first else [ff, boj]
    changed = merge_econ_events([], incoming, now=NOW)
    assert len(changed) == 1
    ev = changed[0]
    assert ev.all_day is True
    assert ev.scheduled_at == at
    assert ev.title == "BoJ Interest Rate Decision"
    assert ev.forecast == "0.75%"
    assert {s.provider for s in ev.sources} == {"boj", "forexfactory"}


# ── final review wave A: New York times, variant labels, category re-derivation ──


def _row(gmt: str, country: str, name: str, *, desc: str | None = None, actual: str | None = None,
         consensus: str | None = None, previous: str | None = None) -> dict[str, Any]:
    return {"gmt": gmt, "country": country, "eventName": name, "actual": actual,
            "consensus": consensus, "previous": previous, "description": desc}


def _payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"data": {"rows": rows}}


def test_nasdaq_cpi_0830_new_york_in_november_is_1330_utc() -> None:
    events = parse_nasdaq_econ(_payload([_row("08:30", "United States", "CPI")]),
                               day=date(2026, 11, 10), fetched_at=NOW)
    assert events[0].scheduled_at == datetime(2026, 11, 10, 13, 30, tzinfo=UTC)
    assert events[0].all_day is False


def test_nasdaq_cpi_0830_new_york_in_july_edt_is_1230_utc() -> None:
    events = parse_nasdaq_econ(_payload([_row("08:30", "United States", "CPI")]),
                               day=date(2026, 7, 14), fetched_at=NOW)
    assert events[0].scheduled_at == datetime(2026, 7, 14, 12, 30, tzinfo=UTC)


def test_nasdaq_all_day_rows_stay_utc_midnight_of_the_day() -> None:
    events = parse_nasdaq_econ(_payload([_row("24H", "United States", "Coincident Indicator")]),
                               day=date(2026, 7, 14), fetched_at=NOW)
    assert events[0].all_day is True
    assert events[0].scheduled_at == datetime(2026, 7, 14, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize("ff_first", [True, False])
def test_nasdaq_and_ff_rows_for_same_release_merge_into_one_event(ff_first: bool) -> None:
    nasdaq = parse_nasdaq_econ(
        _payload([_row("10:00", "United States", "Wholesale Inventories", consensus="0.6%")]),
        day=date(2026, 11, 10), fetched_at=NOW)
    ff = parse_forexfactory(
        [{"title": "Wholesale Inventories", "country": "USD", "date": "2026-11-10T10:00:00-05:00",
          "impact": "Medium", "forecast": "0.6%", "previous": ""}],
        fetched_at=NOW)
    incoming = [*ff, *nasdaq] if ff_first else [*nasdaq, *ff]
    changed = merge_econ_events([], incoming, now=NOW)
    assert len(changed) == 1
    assert changed[0].scheduled_at == datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
    assert {s.provider for s in changed[0].sources} == {"nasdaq", "forexfactory"}


def test_duplicate_cpi_rows_get_period_labels_and_stable_ids() -> None:
    rows = [
        _row("08:30", "United States", "CPI", desc="Consumer prices, month-over-month change."),
        _row("08:30", "United States", "CPI", desc="Consumer prices, year-over-year change."),
    ]
    events = parse_nasdaq_econ(_payload(rows), day=date(2026, 11, 10), fetched_at=NOW)
    assert [e.title for e in events] == ["CPI (m/m)", "CPI (y/y)"]
    at = datetime(2026, 11, 10, 13, 30, tzinfo=UTC)
    assert [e.id for e in events] == [event_id("US", "CPI [#1]", at), event_id("US", "CPI [#2]", at)]


def test_duplicate_rows_with_different_unit_pattern_get_unit_labels() -> None:
    rows = [
        _row("24H", "Japan", "Leading Index", previous="0.4%"),
        _row("24H", "Japan", "Leading Index", consensus="116.7", previous="118.0"),
    ]
    events = parse_nasdaq_econ(_payload(rows), day=date(2026, 11, 10), fetched_at=NOW)
    assert [e.title for e in events] == ["Leading Index (%)", "Leading Index (level)"]


def test_identical_duplicate_rows_fall_back_to_ordinal_labels() -> None:
    rows = [
        _row("18:30", "Japan", "Household Spending", consensus="0.3%", previous="0.1%"),
        _row("18:30", "Japan", "Household Spending", previous="-3.1%"),
    ]
    events = parse_nasdaq_econ(_payload(rows), day=date(2026, 11, 10), fetched_at=NOW)
    assert [e.title for e in events] == ["Household Spending (1 of 2)", "Household Spending (2 of 2)"]


def test_single_row_title_is_not_labelled() -> None:
    events = parse_nasdaq_econ(_payload([_row("08:30", "United States", "CPI", desc="month-over-month")]),
                               day=date(2026, 11, 10), fetched_at=NOW)
    assert events[0].title == "CPI"


def test_merge_rederives_category_and_importance_floor_from_title() -> None:
    at = datetime(2026, 11, 10, 13, 30, tzinfo=UTC)
    stale = EconEvent(id=event_id("US", "CPI m/m", at), country="US", title="CPI m/m", category="other",
                      importance=1, scheduled_at=at, sources=[_ref("forexfactory")])
    incoming = _event("CPI m/m", "US", at, "forexfactory")
    changed = merge_econ_events([stale], [incoming], now=NOW)
    assert len(changed) == 1
    assert changed[0].category == "inflation"
    assert changed[0].importance == 3


def test_same_provider_refresh_takes_the_newer_title() -> None:
    at = datetime(2026, 11, 10, 13, 30, tzinfo=UTC)
    stored = _event("CPI", "US", at, "nasdaq")
    incoming = _event("CPI", "US", at, "nasdaq").model_copy(update={"title": "CPI (m/m)"})
    changed = merge_econ_events([stored], [incoming], now=NOW)
    assert len(changed) == 1
    assert changed[0].title == "CPI (m/m)"
    assert changed[0].id == stored.id


def test_reclassify_stored_fixes_stale_categories_and_is_idempotent(store: MarketsStore) -> None:
    at = datetime(2026, 11, 10, 13, 30, tzinfo=UTC)
    store.upsert_econ_events([EconEvent(id=event_id("US", "CPI", at), country="US", title="CPI",
                                        category="other", importance=1, scheduled_at=at,
                                        sources=[_ref("nasdaq")])])
    assert reclassify_stored(store, NOW) == 1
    stored = store.query_econ(at - timedelta(days=1), at + timedelta(days=1), min_importance=0)
    assert len(stored) == 1
    assert stored[0].category == "inflation"
    assert stored[0].importance == 3
    assert reclassify_stored(store, NOW) == 0
