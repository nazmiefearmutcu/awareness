"""Corporate calendar: Nasdaq earnings, dividends, splits, IPOs and the collector."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from awareness.markets.calendar.corporate import (
    NasdaqCorporateCollector,
    corp_event_id,
    parse_money,
    parse_nasdaq_dividends,
    parse_nasdaq_earnings,
    parse_nasdaq_ipos,
    parse_nasdaq_splits,
    parse_split_ratio,
    parse_us_date,
)
from awareness.markets.collectors.base import CollectorContext
from awareness.markets.config import MarketsSettings
from awareness.markets.http import Fetcher
from awareness.markets.models import CorpEvent
from awareness.markets.sources.registry import PublisherRegistry
from awareness.markets.store import MarketsStore

FETCHED = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
URL = "https://api.nasdaq.com/api/calendar/earnings?date=2026-11-09"


def _load(fixture_bytes: Callable[[str], bytes], name: str) -> dict[str, Any]:
    return json.loads(fixture_bytes(name).decode("utf-8"))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$1.23", 1.23),
        ("($0.45)", -0.45),
        ("-$0.10", -0.10),
        ("$2,345,678,901", 2345678901.0),
        ("N/A", None),
        ("", None),
        ("--", None),
        (None, None),
    ],
)
def test_parse_money(raw: str | None, expected: float | None) -> None:
    assert parse_money(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("11/09/2026", date(2026, 11, 9)),
        ("1/20/2026", date(2026, 1, 20)),
        ("2026-11-09", date(2026, 11, 9)),
        ("Nov 9, 2026", date(2026, 11, 9)),
        ("November 9, 2026", date(2026, 11, 9)),
        ("N/A", None),
        ("", None),
        (None, None),
        ("soon", None),
    ],
)
def test_parse_us_date(raw: str | None, expected: date | None) -> None:
    assert parse_us_date(raw) == expected


def test_parse_split_ratio_orders_as_written() -> None:
    assert parse_split_ratio("8:1") == (8.0, 1.0)
    assert parse_split_ratio("1 : 2") == (1.0, 2.0)
    assert parse_split_ratio("2-for-1") == (2.0, 1.0)
    assert parse_split_ratio("n/a") is None


def test_corp_event_id_is_stable_and_discriminates() -> None:
    day = date(2026, 11, 9)
    assert corp_event_id("dividend", "HTO", day, "0.44") == corp_event_id("dividend", "HTO", day, "0.44")
    assert corp_event_id("dividend", "HTO", day, "0.44") != corp_event_id("dividend", "HTO", day, "1.0")


def test_earnings_fixture(fixture_bytes: Callable[[str], bytes]) -> None:
    payload = _load(fixture_bytes, "nasdaq_earnings_2026-11-09.json")
    day = date(2026, 11, 9)
    events = parse_nasdaq_earnings(payload, day=day, fetched_at=FETCHED, url=URL)
    assert len(events) == len(payload["data"]["rows"]) == 15
    by_symbol = {e.symbol: e for e in events}

    kkr = by_symbol["KKR"]
    assert kkr.type == "earnings" and kkr.date == day and kkr.status == "scheduled"
    assert kkr.time_of_day == "bmo"
    assert kkr.company == "KKR & Co. Inc."
    assert kkr.fields["eps_estimate"] == pytest.approx(1.41)
    assert kkr.fields["num_estimates"] == 7
    assert kkr.fields["market_cap"] == pytest.approx(88237580000.0)
    assert kkr.fields["last_year_eps"] == pytest.approx(1.22)
    assert kkr.fields["last_year_report_date"] == "2025-11-07"
    assert kkr.fields["fiscal_quarter"] == "Sep/2026"
    assert kkr.sources[0].provider == "nasdaq" and kkr.sources[0].url == URL
    assert kkr.sources[0].fetched_at == FETCHED

    assert by_symbol["OXY"].time_of_day == "amc"
    assert by_symbol["FERG"].fields["last_year_report_date"] is None
    negative = [e for e in events if e.fields["eps_estimate"] is not None and e.fields["eps_estimate"] < 0]
    assert negative, "fixture contains parenthesised negative EPS estimates"
    assert {e.time_of_day for e in events} <= {"bmo", "amc", "unknown"}


def test_earnings_ids_are_stable_across_parses(fixture_bytes: Callable[[str], bytes]) -> None:
    payload = _load(fixture_bytes, "nasdaq_earnings_2026-11-09.json")
    day = date(2026, 11, 9)
    first = parse_nasdaq_earnings(payload, day=day, fetched_at=FETCHED, url=URL)
    second = parse_nasdaq_earnings(payload, day=day, fetched_at=FETCHED, url=URL)
    assert [e.id for e in first] == [e.id for e in second]
    assert len({e.id for e in first}) == len(first)


def test_dividends_fixture(fixture_bytes: Callable[[str], bytes]) -> None:
    payload = _load(fixture_bytes, "nasdaq_dividends_2026-11-09.json")
    events = parse_nasdaq_dividends(payload, fetched_at=FETCHED, url=URL)
    assert len(events) == len(payload["data"]["calendar"]["rows"]) == 15
    assert all(e.date == date(2026, 11, 9) and e.type == "dividend" for e in events)
    hto = next(e for e in events if e.symbol == "HTO")
    assert hto.status == "confirmed"
    assert hto.fields == {
        "amount": 0.44,
        "annual_amount": 1.76,
        "record_date": "2026-11-09",
        "pay_date": "2026-12-01",
        "announcement_date": "2026-10-23",
    }


def test_special_dividend_on_same_day_is_distinct() -> None:
    row = {
        "companyName": "Example Corp Common Stock",
        "symbol": "EXM",
        "dividend_Ex_Date": "11/09/2026",
        "payment_Date": "11/20/2026",
        "record_Date": "11/09/2026",
        "dividend_Rate": 0.5,
        "indicated_Annual_Dividend": 2.0,
        "announcement_Date": "10/28/2026",
    }
    special = {**row, "dividend_Rate": 3.0}
    payload = {"data": {"calendar": {"rows": [row, special]}}}
    events = parse_nasdaq_dividends(payload, fetched_at=FETCHED, url=URL)
    assert len(events) == 2
    assert events[0].id != events[1].id


def test_dividend_without_amount_is_not_stored() -> None:
    row = {"companyName": "No Amount Corp", "symbol": "NAC", "dividend_Ex_Date": "11/09/2026", "dividend_Rate": "N/A"}
    skipped: list[str] = []
    events = parse_nasdaq_dividends(
        {"data": {"calendar": {"rows": [row]}}}, fetched_at=FETCHED, url=URL, skipped=skipped
    )
    assert events == []
    assert skipped == ["NAC"]


def test_ipo_same_deal_across_buckets_is_one_row() -> None:
    deal = "1407089-119692"
    filed = {"companyName": "Rezera Inc.", "proposedTickerSymbol": None, "dealID": deal, "filedDate": "11/02/2026"}
    upcoming = {
        "companyName": "Rezera Inc.", "proposedTickerSymbol": "RZRA", "dealID": deal,
        "proposedSharePrice": "11.00-13.00", "expectedPriceDate": "11/12/2026",
    }
    priced = {
        "companyName": "Rezera Inc.", "proposedTickerSymbol": "RZRA", "dealID": deal,
        "proposedSharePrice": "12.00", "pricedDate": "11/12/2026",
    }
    def payload(bucket: str, row: dict) -> dict:
        return {"data": {bucket: {"rows": [row]} if bucket != "upcoming" else {"upcomingTable": {"rows": [row]}}}}
    ids = set()
    for bucket, row in (("filed", filed), ("upcoming", upcoming), ("priced", priced)):
        (event,) = parse_nasdaq_ipos(payload(bucket, row), fetched_at=FETCHED, url=URL)
        ids.add(event.id)
        last = event
    assert len(ids) == 1
    assert last.status == "priced" and last.date == date(2026, 11, 12) and last.symbol == "RZRA"


def test_splits_fixture(fixture_bytes: Callable[[str], bytes]) -> None:
    payload = _load(fixture_bytes, "nasdaq_splits.json")
    events = parse_nasdaq_splits(payload, fetched_at=FETCHED, url=URL)
    assert len(events) == len(payload["data"]["rows"]) == 25
    sndk = next(e for e in events if e.symbol == "SNDK")
    assert sndk.type == "split" and sndk.date == date(2026, 11, 20)
    assert sndk.status == "scheduled"
    assert sndk.fields["ratio"] == "8:1"
    assert sndk.fields["ratio_first"] == 8.0 and sndk.fields["ratio_second"] == 1.0
    assert sndk.fields["payable_date"] is None
    bito = next(e for e in events if e.symbol == "BITO")
    assert bito.fields["ratio_first"] == 1.0 and bito.fields["ratio_second"] == 2.0


def test_split_status_completed_when_effective_date_passed() -> None:
    payload = {
        "data": {
            "rows": [
                {"symbol": "OLD", "name": "Old Co", "ratio": "2 : 1", "executionDate": "11/01/2026"},
            ]
        }
    }
    (event,) = parse_nasdaq_splits(payload, fetched_at=FETCHED, url=URL)
    assert event.status == "completed"


def test_ipo_buckets_fixture(fixture_bytes: Callable[[str], bytes]) -> None:
    payload = _load(fixture_bytes, "nasdaq_ipo_2026-11.json")
    events = parse_nasdaq_ipos(payload, fetched_at=FETCHED, url=URL)
    assert len(events) == 1 + 1 + 3 + 2

    priced = next(e for e in events if e.status == "priced")
    assert priced.symbol == "NARA" and priced.date == date(2026, 11, 2)
    assert priced.exchange == "NASDAQ Capital"
    assert priced.fields["price"] == pytest.approx(10.0)
    assert priced.fields["price_range"] is None
    assert priced.fields["shares_offered"] == pytest.approx(9098800.0)
    assert priced.fields["deal_size_usd"] == pytest.approx(90988000.0)
    assert priced.fields["deal_id"] == "1390143-118358"

    upcoming = next(e for e in events if e.status == "scheduled")
    assert upcoming.symbol == "RZRA" and upcoming.date == date(2026, 11, 12)
    assert upcoming.fields["price_range"] == "11.00-13.00"
    assert upcoming.fields["price"] is None

    filed = [e for e in events if e.status == "filed"]
    assert len(filed) == 3
    assert sorted(e.symbol or "" for e in filed) == ["", "CXIVU", "KACQU"]
    assert all(e.date is not None for e in filed)

    withdrawn = [e for e in events if e.status == "withdrawn"]
    assert len(withdrawn) == 2
    dt_house = next(e for e in withdrawn if e.company == "DT House Ltd")
    assert dt_house.date == date(2026, 11, 4)
    assert dt_house.fields["filed_date"] == "2026-01-20"


def test_ipo_skips_rows_without_date_and_handles_null_buckets() -> None:
    payload = {
        "data": {
            "priced": None,
            "upcoming": {"upcomingTable": {"rows": [{"companyName": "No Date Inc", "expectedPriceDate": "N/A"}]}},
            "filed": {"rows": None},
            "withdrawn": {"rows": [{"companyName": "Ok Corp", "withdrawDate": "11/03/2026"}]},
        }
    }
    events = parse_nasdaq_ipos(payload, fetched_at=FETCHED, url=URL)
    assert [e.company for e in events] == ["Ok Corp"]


def _settings(tmp_path: Path, **overrides: Any) -> MarketsSettings:
    return MarketsSettings(
        data_dir=tmp_path / "markets", autostart_runner=False, sentiment_backend="lexicon", **overrides
    )


def test_collector_tolerates_partial_failure(
    tmp_path: Path, store: MarketsStore, fixture_bytes: Callable[[str], bytes]
) -> None:
    """Two days in the window; the first day fails, the second succeeds, IPO next month fails."""
    earnings = fixture_bytes("nasdaq_earnings_2026-11-09.json")
    dividends = fixture_bytes("nasdaq_dividends_2026-11-09.json")
    ipo = fixture_bytes("nasdaq_ipo_2026-11.json")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if "earnings?date=2026-11-09" in url:
            return httpx.Response(200, content=earnings)
        if "dividends?date=2026-11-09" in url:
            return httpx.Response(200, content=dividends)
        if "ipo/calendar?date=2026-11" in url and "2026-12" not in url:
            return httpx.Response(200, content=ipo)
        return httpx.Response(404, text="not found")

    settings = _settings(tmp_path, corp_days_back=0, corp_days_ahead=1)
    now = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)

    async def run() -> Any:
        http = Fetcher(
            settings, store, transport=httpx.MockTransport(handler),
            sleep=_no_sleep, host_intervals={"api.nasdaq.com": 0.0},
        )
        ctx = CollectorContext(
            settings=settings, store=store, http=http, registry=PublisherRegistry([]), now=lambda: now
        )
        try:
            return await NasdaqCorporateCollector().run(ctx)
        finally:
            await http.aclose()

    result = asyncio.run(run())
    assert any("earnings?date=2026-11-08" in u for u in seen)
    assert result.items == 15 + 15 + 7
    assert result.notes and "failed" in result.notes[0]

    window = (date(2026, 11, 1), date(2026, 12, 31))
    assert len(store.query_corp(*window, types=["earnings"])) == 15
    assert len(store.query_corp(*window, types=["dividend"])) == 15
    assert len(store.query_corp(*window, types=["ipo"])) == 7


def test_collector_raises_when_every_request_fails(tmp_path: Path, store: MarketsStore) -> None:
    settings = _settings(tmp_path, corp_days_back=0, corp_days_ahead=0)
    now = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="gone")

    async def run() -> None:
        http = Fetcher(
            settings, store, transport=httpx.MockTransport(handler),
            sleep=_no_sleep, host_intervals={"api.nasdaq.com": 0.0},
        )
        ctx = CollectorContext(
            settings=settings, store=store, http=http, registry=PublisherRegistry([]), now=lambda: now
        )
        try:
            await NasdaqCorporateCollector().run(ctx)
        finally:
            await http.aclose()

    with pytest.raises(RuntimeError):
        asyncio.run(run())


async def _no_sleep(_: float) -> None:
    return None


def test_corp_events_are_valid_models(fixture_bytes: Callable[[str], bytes]) -> None:
    payload = _load(fixture_bytes, "nasdaq_ipo_2026-11.json")
    for event in parse_nasdaq_ipos(payload, fetched_at=FETCHED, url=URL):
        assert isinstance(event, CorpEvent)
        assert event.datetime_utc is None
        assert event.sources[0].basis == "native"
