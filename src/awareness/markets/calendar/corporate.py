"""Corporate calendar from Nasdaq: earnings, dividends, splits and IPOs.

Every ``parse_nasdaq_*`` function is pure: it takes a decoded payload and
returns typed :class:`CorpEvent` records carrying ``ProviderRef`` provenance.
Unknown or unparseable fields stay ``None``; rows missing the fields that
identify the event (symbol, company, date) are skipped rather than guessed.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import Any, Literal

from awareness.markets.collectors.base import CollectorContext, CollectorResult
from awareness.markets.http import FetchError, FetchResult
from awareness.markets.models import CorpEvent, ProviderRef, stable_id

log = logging.getLogger(__name__)

NASDAQ_PROVIDER = "nasdaq"
NASDAQ_CALENDAR_URL = "https://api.nasdaq.com/api/calendar"
NASDAQ_IPO_URL = "https://api.nasdaq.com/api/ipo/calendar"

_NULL_TEXT = {"", "N/A", "NA", "--", "-"}
_NUMBER_RE = re.compile(r"[+-]?\d+(?:\.\d+)?")
_RANGE_RE = re.compile(r"\d\s*-\s*\$?\d")
_RATIO_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?::|-for-|\s+for\s+|/)\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y")
_TIME_OF_DAY = {"time-pre-market": "bmo", "time-after-hours": "amc"}

IpoStatus = Literal["priced", "scheduled", "filed", "withdrawn"]
ParseFn = Callable[[FetchResult], list[CorpEvent]]


def parse_money(raw: str | None) -> float | None:
    """Parse a provider money/number string. ``($0.45)`` is negative; blanks and junk give ``None``."""
    if raw is None:
        return None
    text = str(raw).strip()
    if text.upper() in _NULL_TEXT:
        return None
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1].strip()
    cleaned = text.replace("$", "").replace(",", "").replace(" ", "")
    if not _NUMBER_RE.fullmatch(cleaned):
        return None
    return float(cleaned)


def parse_us_date(raw: str | None) -> date | None:
    """Parse ``MM/DD/YYYY``, ISO, ``Nov 9, 2026`` style dates. Anything else gives ``None``."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text.upper() in _NULL_TEXT:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def corp_event_id(type_: str, symbol: str | None, day: date, extra: str = "") -> str:
    """Deterministic id; ``extra`` separates events that share type, symbol and day."""
    return stable_id("corp", type_, symbol, day, extra)


def parse_split_ratio(raw: str | None) -> tuple[float, float] | None:
    """``"8:1"``, ``"1 : 2"``, ``"2-for-1"`` -> ``(8.0, 1.0)``, ``(1.0, 2.0)``, ``(2.0, 1.0)``.

    The two numbers are kept in the order the provider wrote them (``ratio_first``, ``ratio_second``).
    Whether that order is new:old or old:new is not established by the fixture, so no direction is implied.
    """
    text = _clean(raw)
    if text is None:
        return None
    match = _RATIO_RE.search(text)
    if match is None:
        return None
    return float(match.group(1)), float(match.group(2))


def _clean(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return None if text.upper() in _NULL_TEXT else text


def _money(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return parse_money(_clean(value))


def _normalize_symbol(raw: Any) -> str | None:
    text = _clean(raw)
    return text.upper().replace(".", "-") if text else None


def _dig(payload: Any, *keys: str) -> Any:
    node = payload
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _rows(node: Any) -> list[dict[str, Any]]:
    """The ``rows`` list of a Nasdaq table node; empty when the bucket is missing or null."""
    rows = node.get("rows") if isinstance(node, dict) else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _iso(day: date | None) -> str | None:
    return day.isoformat() if day else None


def _provider(url: str, fetched_at: datetime) -> ProviderRef:
    return ProviderRef(provider=NASDAQ_PROVIDER, url=url, fetched_at=fetched_at)


def parse_nasdaq_earnings(payload: dict[str, Any], *, day: date, fetched_at: datetime, url: str) -> list[CorpEvent]:
    """Earnings announcements for ``day`` (the endpoint is queried per date)."""
    provider = _provider(url, fetched_at)
    events: list[CorpEvent] = []
    for row in _rows(_dig(payload, "data")):
        symbol = _normalize_symbol(row.get("symbol"))
        company = _clean(row.get("name"))
        if symbol is None or company is None:
            continue
        time_raw = _clean(row.get("time")) or ""
        num_estimates = _money(row.get("noOfEsts"))
        events.append(
            CorpEvent(
                id=corp_event_id("earnings", symbol, day),
                type="earnings",
                symbol=symbol,
                company=company,
                date=day,
                time_of_day=_TIME_OF_DAY.get(time_raw, "unknown"),
                status="scheduled",
                fields={
                    "eps_estimate": _money(row.get("epsForecast")),
                    "num_estimates": int(num_estimates) if num_estimates is not None else None,
                    "fiscal_quarter": _clean(row.get("fiscalQuarterEnding")),
                    "market_cap": _money(row.get("marketCap")),
                    "last_year_eps": _money(row.get("lastYearEPS")),
                    "last_year_report_date": _iso(parse_us_date(_clean(row.get("lastYearRptDt")))),
                },
                sources=[provider],
            )
        )
    return events


def parse_nasdaq_dividends(
    payload: dict[str, Any], *, fetched_at: datetime, url: str, skipped: list[str] | None = None
) -> list[CorpEvent]:
    """Dividends keyed by ex-dividend date. The amount is part of the id (special dividends).

    Rows without an amount are not stored; their symbols are appended to ``skipped`` when given.
    """
    provider = _provider(url, fetched_at)
    events: list[CorpEvent] = []
    for row in _rows(_dig(payload, "data", "calendar")):
        ex_date = parse_us_date(_clean(row.get("dividend_Ex_Date")))
        symbol = _normalize_symbol(row.get("symbol"))
        company = _clean(row.get("companyName"))
        if ex_date is None or symbol is None or company is None:
            continue
        amount = _money(row.get("dividend_Rate"))
        if amount is None:  # identity includes the amount, so a row without one is not stored
            if skipped is not None:
                skipped.append(symbol)
            continue
        events.append(
            CorpEvent(
                id=corp_event_id("dividend", symbol, ex_date, repr(amount)),
                type="dividend",
                symbol=symbol,
                company=company,
                date=ex_date,
                status="confirmed",
                fields={
                    "amount": amount,
                    "annual_amount": _money(row.get("indicated_Annual_Dividend")),
                    "record_date": _iso(parse_us_date(_clean(row.get("record_Date")))),
                    "pay_date": _iso(parse_us_date(_clean(row.get("payment_Date")))),
                    "announcement_date": _iso(parse_us_date(_clean(row.get("announcement_Date")))),
                },
                sources=[provider],
            )
        )
    return events


def parse_nasdaq_splits(payload: dict[str, Any], *, fetched_at: datetime, url: str) -> list[CorpEvent]:
    """Stock splits effective on ``executionDate``; ``completed`` once that date has passed."""
    provider = _provider(url, fetched_at)
    fetched_day = fetched_at.astimezone(UTC).date()
    events: list[CorpEvent] = []
    for row in _rows(_dig(payload, "data")):
        symbol = _normalize_symbol(row.get("symbol"))
        company = _clean(row.get("name"))
        effective = parse_us_date(_clean(row.get("executionDate")))
        if symbol is None or company is None or effective is None:
            continue
        ratio_raw = _clean(row.get("ratio"))
        parsed = parse_split_ratio(ratio_raw)
        events.append(
            CorpEvent(
                id=corp_event_id("split", symbol, effective, ratio_raw or ""),
                type="split",
                symbol=symbol,
                company=company,
                date=effective,
                status="scheduled" if effective >= fetched_day else "completed",
                fields={
                    "ratio": ratio_raw,
                    "ratio_first": parsed[0] if parsed else None,
                    "ratio_second": parsed[1] if parsed else None,
                    "payable_date": _iso(parse_us_date(_clean(row.get("payableDate")))),
                },
                sources=[provider],
            )
        )
    return events


def _ipo_id(deal_id: str | None, company: str, symbol: str | None) -> str:
    """One IPO deal, one id: the Nasdaq deal id when present, else company and proposed symbol.

    Date and status are deliberately excluded, so a deal moving through the pipeline
    (filed, scheduled, priced, withdrawn) updates its existing row in place.
    """
    if deal_id:
        return stable_id("ipo", deal_id)
    return stable_id("ipo", company.upper(), symbol)


def parse_nasdaq_ipos(payload: dict[str, Any], *, fetched_at: datetime, url: str) -> list[CorpEvent]:
    """IPO pipeline across the priced, upcoming, filed and withdrawn buckets of one month."""
    provider = _provider(url, fetched_at)
    buckets: list[tuple[list[dict[str, Any]], str, IpoStatus]] = [
        (_rows(_dig(payload, "data", "priced")), "pricedDate", "priced"),
        (_rows(_dig(payload, "data", "upcoming", "upcomingTable")), "expectedPriceDate", "scheduled"),
        (_rows(_dig(payload, "data", "filed")), "filedDate", "filed"),
        (_rows(_dig(payload, "data", "withdrawn")), "withdrawDate", "withdrawn"),
    ]
    events: list[CorpEvent] = []
    for rows, date_key, status in buckets:
        for row in rows:
            day = parse_us_date(_clean(row.get(date_key)))
            company = _clean(row.get("companyName"))
            if day is None or company is None:
                continue
            raw_price = _clean(row.get("proposedSharePrice"))
            is_range = raw_price is not None and _RANGE_RE.search(raw_price) is not None
            deal_id = _clean(row.get("dealID"))
            symbol = _normalize_symbol(row.get("proposedTickerSymbol"))
            events.append(
                CorpEvent(
                    id=_ipo_id(deal_id, company, symbol),
                    type="ipo",
                    symbol=symbol,
                    company=company,
                    exchange=_clean(row.get("proposedExchange")),
                    date=day,
                    status=status,
                    fields={
                        "price": None if is_range else _money(raw_price),
                        "price_range": raw_price if is_range else None,
                        "shares_offered": _money(row.get("sharesOffered")),
                        "deal_size_usd": _money(row.get("dollarValueOfSharesOffered")),
                        "filed_date": _iso(parse_us_date(_clean(row.get("filedDate")))),
                        "deal_id": deal_id,
                    },
                    sources=[provider],
                )
            )
    return events


async def _run_jobs(ctx: CollectorContext, jobs: Sequence[tuple[str, ParseFn]], notes: list[str]) -> list[CorpEvent]:
    """Fetch and parse every job. Single failures are tolerated; all failing raises."""
    events: list[CorpEvent] = []
    errors: list[str] = []
    for url, parse in jobs:
        try:
            result = await ctx.http.get(url, profile="browser")
            events.extend(parse(result))
        except (FetchError, ValueError) as exc:  # ValueError also covers JSON decode errors
            errors.append(f"{url}: {exc}")
            log.warning("corp.nasdaq request failed: %s", exc)
    if jobs and len(errors) == len(jobs):
        raise RuntimeError(f"all {len(jobs)} Nasdaq calendar requests failed; last: {errors[-1]}")
    if errors:
        notes.append(f"{len(errors)} of {len(jobs)} Nasdaq requests failed")
    return events


def _month_after(day: date) -> date:
    first = day.replace(day=1)
    return (first + timedelta(days=32)).replace(day=1)


def _parse_earnings_result(result: FetchResult, *, day: date, url: str) -> list[CorpEvent]:
    return parse_nasdaq_earnings(result.json(), day=day, fetched_at=result.fetched_at, url=url)


def _parse_dividends_result(result: FetchResult, *, url: str, skipped: list[str]) -> list[CorpEvent]:
    return parse_nasdaq_dividends(result.json(), fetched_at=result.fetched_at, url=url, skipped=skipped)


def _parse_splits_result(result: FetchResult, *, url: str) -> list[CorpEvent]:
    return parse_nasdaq_splits(result.json(), fetched_at=result.fetched_at, url=url)


def _parse_ipos_result(result: FetchResult, *, url: str) -> list[CorpEvent]:
    return parse_nasdaq_ipos(result.json(), fetched_at=result.fetched_at, url=url)


class NasdaqCorporateCollector:
    """Nasdaq earnings, dividends, splits and IPO calendars for the configured window."""

    name = "corp.nasdaq"
    interval_s = 3600
    lock_group: str | None = "corp"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        today = ctx.now().astimezone(UTC).date()
        settings = ctx.settings
        notes: list[str] = []
        jobs: list[tuple[str, ParseFn]] = []
        dividend_skips: list[str] = []
        for offset in range(-settings.corp_days_back, settings.corp_days_ahead + 1):
            day = today + timedelta(days=offset)
            earnings_url = f"{NASDAQ_CALENDAR_URL}/earnings?date={day.isoformat()}"
            dividends_url = f"{NASDAQ_CALENDAR_URL}/dividends?date={day.isoformat()}"
            jobs.append((earnings_url, partial(_parse_earnings_result, day=day, url=earnings_url)))
            jobs.append((dividends_url, partial(_parse_dividends_result, url=dividends_url, skipped=dividend_skips)))
        splits_url = f"{NASDAQ_CALENDAR_URL}/splits?date={today.isoformat()}"
        jobs.append((splits_url, partial(_parse_splits_result, url=splits_url)))
        for month in (today.replace(day=1), _month_after(today)):
            month_url = f"{NASDAQ_IPO_URL}?date={month.year:04d}-{month.month:02d}"
            jobs.append((month_url, partial(_parse_ipos_result, url=month_url)))

        events = await _run_jobs(ctx, jobs, notes)
        if dividend_skips:
            notes.append(f"{len(dividend_skips)} dividend rows without an amount were not stored")
        upserted = ctx.store.upsert_corp_events(events)
        return CollectorResult(items=upserted, notes=notes)
