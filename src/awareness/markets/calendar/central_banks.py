"""Central-bank rate-decision calendars: Fed, ECB, BoE, BoJ pages and curated TCMB-style schedules.

Only dates that appear on a fetched page (or in a verified curated entry) become events.
Nothing is extrapolated from a meeting pattern.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from functools import cache
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from lxml import etree
from lxml import html as lxml_html

from awareness.markets.calendar.econ_common import event_id, utc_of
from awareness.markets.calendar.economic import padded_bounds, store_merge
from awareness.markets.collectors.base import CollectorContext, CollectorResult
from awareness.markets.http import FetchError
from awareness.markets.models import EconEvent, ProviderRef

log = logging.getLogger(__name__)

FED_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
ECB_URL = "https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html"
BOE_URL = "https://www.bankofengland.co.uk/monetary-policy/upcoming-mpc-dates"
BOJ_URL = "https://www.boj.or.jp/en/mopo/mpmsche_minu/index.htm"

# Europe/Frankfurt is not present in the tzdata bundle used here; Europe/Berlin has the
# identical CET/CEST rules (the ECB is in Frankfurt).

FOMC_DECISION_TITLE = "FOMC Interest Rate Decision"
FOMC_MINUTES_TITLE = "FOMC Minutes"
ECB_TITLE = "ECB Interest Rate Decision"
BOE_TITLE = "BoE Interest Rate Decision"
BOJ_TITLE = "BoJ Interest Rate Decision"

_MONTHS: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_FOMC_DATES_RE = re.compile(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*\*?\s*")
_FOMC_RELEASED_RE = re.compile(r"Released\s+([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})")
_BOE_DATE_RE = re.compile(r"^[A-Za-z]+\s+(\d{1,2})\s+([A-Za-z]+)$")
_BOJ_DAY_RE = re.compile(r"(?:([A-Za-z]+)\.?\s+)?(\d{1,2})\s*\(")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def _month(text: str) -> int | None:
    return _MONTHS.get(text.strip().lower()[:3])


@cache
def _zone(name: str) -> ZoneInfo:
    """IANA zone looked up on first use (not at import), so a missing tz database
    fails the collector run instead of importing this module."""
    return ZoneInfo(name)


def _doc(html_text: str) -> Any:
    """Parse a page body. An empty or garbled body raises ValueError (tolerated per page)."""
    if not html_text.strip():
        raise ValueError("empty page body")
    try:
        return lxml_html.fromstring(html_text.lstrip("﻿"))
    except etree.LxmlError as exc:
        raise ValueError(f"unparseable page body: {exc}") from exc


def _text(node: Any) -> str:
    return " ".join(node.text_content().split())


def _ref(provider: str, url: str, fetched_at: datetime) -> ProviderRef:
    return ProviderRef(provider=provider, url=url, fetched_at=utc_of(fetched_at), basis="native")


def _event(
    *, country: str, currency: str, title: str, when: datetime, all_day: bool,
    provider: str, url: str, fetched_at: datetime, description: str | None = None,
) -> EconEvent:
    ref = _ref(provider, url, fetched_at)
    return EconEvent(
        id=event_id(country, title, when),
        country=country,
        currency=currency,
        title=title,
        category="central_bank",
        scheduled_at=when,
        all_day=all_day,
        importance=3,
        description=description,
        sources=[ref],
    )


def _dedupe(events: list[EconEvent]) -> list[EconEvent]:
    out: dict[str, EconEvent] = {}
    for event in events:
        out.setdefault(event.id, event)
    return list(out.values())


# ── Federal Reserve ───────────────────────────────────────────────────────


def parse_fomc_calendar(html: str, *, fetched_at: datetime) -> list[EconEvent]:
    """FOMC meetings per year panel. Decision = last meeting day, 14:00 New York.

    Rows marked "(notation vote)" or "unscheduled" are skipped. An asterisk marks a meeting
    with a Summary of Economic Projections. "Released <date>" minutes dates are emitted as
    all-day ``FOMC Minutes`` events (the page does not state a release time).
    """
    doc = _doc(html)
    events: list[EconEvent] = []
    for panel in doc.xpath('//div[contains(@class,"panel")][.//h4]'):
        heading = _text(panel.xpath(".//h4")[0])
        year_match = re.match(r"(\d{4})\s+FOMC Meetings", heading)
        if year_match is None:
            continue
        year = int(year_match.group(1))
        for row in panel.xpath('.//div[contains(@class,"fomc-meeting") and contains(@class,"row")]'):
            month_text = _text_of(row, "fomc-meeting__month")
            date_text = _text_of(row, "fomc-meeting__date")
            minutes_text = _text_of(row, "fomc-meeting__minutes")
            if "notation" in date_text.lower() or "unscheduled" in date_text.lower():
                continue
            day_match = _FOMC_DATES_RE.fullmatch(date_text)
            if day_match is None or not month_text:
                log.debug("fomc: unparsed meeting row %r %r", month_text, date_text)
                continue
            end_month_name = month_text.split("/")[-1]
            end_month = _month(end_month_name)
            if end_month is None:
                log.debug("fomc: unknown month %r", month_text)
                continue
            start_month = _month(month_text.split("/")[0])
            end_year = year + 1 if start_month is not None and end_month < start_month else year
            end_day = int(day_match.group(2))
            decision_day = date(end_year, end_month, end_day)
            decision = datetime(decision_day.year, decision_day.month, decision_day.day, 14, 0, tzinfo=_zone("America/New_York"))
            is_sep = "*" in date_text
            events.append(
                _event(
                    country="US", currency="USD", title=FOMC_DECISION_TITLE,
                    when=decision.astimezone(UTC), all_day=False, provider="fed", url=FED_URL,
                    fetched_at=fetched_at,
                    description=("Meeting with a Summary of Economic Projections (SEP)." if is_sep else None),
                )
            )
            released = _FOMC_RELEASED_RE.search(minutes_text)
            if released is not None:
                minutes_month = _month(released.group(1))
                if minutes_month is not None:
                    minutes_day = date(int(released.group(3)), minutes_month, int(released.group(2)))
                    events.append(
                        _event(
                            country="US", currency="USD", title=FOMC_MINUTES_TITLE,
                            when=datetime.combine(minutes_day, time(0, 0), UTC), all_day=True,
                            provider="fed", url=FED_URL, fetched_at=fetched_at,
                            description="Release date from the Fed calendar; release time not stated.",
                        )
                    )
    if not events:
        log.warning("fomc: no meetings found on %s", FED_URL)
    return _dedupe(events)


def _text_of(row: Any, class_fragment: str) -> str:
    nodes = row.xpath(f'.//div[contains(@class,"{class_fragment}")]')
    return _text(nodes[0]) if nodes else ""


# ── ECB ───────────────────────────────────────────────────────────────────


def parse_ecb_calendar(html: str, *, fetched_at: datetime) -> list[EconEvent]:
    """ECB monetary-policy meetings that are flagged with a press conference.

    Each ``dt`` holds a ``DD/MM/YYYY`` date and the following ``dd`` describes the meeting.
    Only "monetary policy meeting ... followed by press conference" days become decisions
    (14:15 Frankfurt). Non-monetary meetings and General Council meetings are skipped.
    """
    doc = _doc(html)
    events: list[EconEvent] = []
    for dl in doc.xpath("//dl"):
        current: str | None = None
        for child in dl.xpath("./dt | ./dd"):
            if child.tag == "dt":
                current = _text(child)
                continue
            description = _text(child).lower()
            if current is None:
                continue
            if "monetary policy meeting" not in description or "non-monetary" in description:
                continue
            if "press conference" not in description:
                continue
            try:
                day = datetime.strptime(current, "%d/%m/%Y").date()
            except ValueError:
                log.debug("ecb: unparsed date %r", current)
                continue
            decision = datetime.combine(day, time(14, 15), tzinfo=_zone("Europe/Berlin")).astimezone(UTC)
            events.append(
                _event(
                    country="EU", currency="EUR", title=ECB_TITLE, when=decision, all_day=False,
                    provider="ecb", url=ECB_URL, fetched_at=fetched_at,
                )
            )
    if not events:
        log.warning("ecb: no monetary meetings found on %s", ECB_URL)
    return _dedupe(events)


# ── Bank of England ───────────────────────────────────────────────────────


def parse_boe_mpc_dates(html: str, *, fetched_at: datetime) -> list[EconEvent]:
    """MPC announcement dates under "<year> confirmed dates" headings (12:00 London)."""
    doc = _doc(html)
    events: list[EconEvent] = []
    for heading in doc.xpath("//h2"):
        year_match = re.match(r"^(\d{4})\s+confirmed dates$", _text(heading))
        if year_match is None:
            continue
        year = int(year_match.group(1))
        tables = heading.xpath("following::table[1]")
        if not tables:
            continue
        for row in tables[0].xpath(".//tr"):
            cells = row.xpath("./td")
            if not cells:
                continue
            match = _BOE_DATE_RE.match(_text(cells[0]))
            if match is None:
                continue
            month = _month(match.group(2))
            if month is None:
                continue
            day = date(year, month, int(match.group(1)))
            announce = datetime.combine(day, time(12, 0), tzinfo=_zone("Europe/London")).astimezone(UTC)
            events.append(
                _event(
                    country="GB", currency="GBP", title=BOE_TITLE, when=announce, all_day=False,
                    provider="boe", url=BOE_URL, fetched_at=fetched_at,
                )
            )
    if not events:
        log.warning("boe: no MPC dates found on %s", BOE_URL)
    return _dedupe(events)


# ── Bank of Japan ─────────────────────────────────────────────────────────


def _boj_last_day(cell_text: str) -> tuple[int, int] | None:
    """(month, day) of the last day in a "Jan. 22 (Thurs.), 23 (Fri.)" style cell."""
    month: int | None = None
    day: int | None = None
    for match in _BOJ_DAY_RE.finditer(cell_text):
        if match.group(1):
            month = _month(match.group(1)) or month
        day = int(match.group(2))
    if month is None or day is None:
        return None
    return month, day


def parse_boj_schedule(html: str, *, fetched_at: datetime) -> list[EconEvent]:
    """BoJ monetary policy meetings: last day of each MPM, all-day (no fixed release time)."""
    doc = _doc(html)
    events: list[EconEvent] = []
    for heading in doc.xpath('//h2[starts-with(@id,"p")]'):
        year_match = re.match(r"^\s*(\d{4})\s*$", _text(heading))
        if year_match is None:
            continue
        year = int(year_match.group(1))
        tables = heading.xpath("following::table[1]")
        if not tables:
            continue
        for row in tables[0].xpath(".//tbody/tr"):
            cells = row.xpath("./td")
            if not cells:
                continue
            last = _boj_last_day(_text(cells[0]))
            if last is None:
                log.debug("boj: unparsed MPM cell %r", _text(cells[0]))
                continue
            month, day = last
            meeting_day = date(year, month, day)
            events.append(
                _event(
                    country="JP", currency="JPY", title=BOJ_TITLE,
                    when=datetime.combine(meeting_day, time(0, 0), UTC), all_day=True,
                    provider="boj", url=BOJ_URL, fetched_at=fetched_at,
                )
            )
    if not events:
        log.warning("boj: no MPM dates found on %s", BOJ_URL)
    return _dedupe(events)


# ── curated (hand-maintained, verified) ───────────────────────────────────


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def load_curated_schedules(path: Path, *, fetched_at: datetime) -> list[EconEvent]:
    """Events from ``central_banks.yaml``. Entries need a source_url and verified_on.

    ``time`` is "HH:MM" in the entry's IANA ``tz``, or null for an all-day date.
    Entries that lack provenance or are malformed are skipped with a warning.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = raw.get("entries") or []
    events: list[EconEvent] = []
    for entry in entries:
        required = ("provider", "country", "title", "date", "tz", "source_url", "verified_on")
        missing = [key for key in required if not entry.get(key)]
        if missing:
            log.warning("curated central-bank entry skipped, missing %s: %r", missing, entry.get("title"))
            continue
        day = _as_date(entry["date"])
        verified = _as_date(entry["verified_on"])
        if day is None or verified is None:
            log.warning("curated central-bank entry skipped, bad date: %r", entry.get("title"))
            continue
        try:
            zone = ZoneInfo(str(entry["tz"]))
        except ZoneInfoNotFoundError:
            log.warning("curated central-bank entry skipped, unknown tz %r", entry["tz"])
            continue
        clock = str(entry.get("time") or "").strip()
        if clock:
            match = _TIME_RE.match(clock)
            if match is None:
                log.warning("curated central-bank entry skipped, bad time %r (quote it in YAML)", clock)
                continue
            local = datetime.combine(day, time(int(match.group(1)), int(match.group(2))), tzinfo=zone)
            when, all_day = local.astimezone(UTC), False
        else:
            when, all_day = datetime.combine(day, time(0, 0), UTC), True
        provider = str(entry["provider"])
        ref = ProviderRef(
            provider=provider, url=str(entry["source_url"]), fetched_at=utc_of(fetched_at), basis="curated"
        )
        events.append(
            EconEvent(
                id=event_id(str(entry["country"]), str(entry["title"]), when),
                country=str(entry["country"]).upper(),
                currency=entry.get("currency"),
                title=str(entry["title"]),
                category="central_bank",
                scheduled_at=when,
                all_day=all_day,
                importance=int(entry.get("importance", 3)),
                description=f"Curated date verified on {verified.isoformat()} against the official page.",
                sources=[ref],
            )
        )
    return events


# ── collector ─────────────────────────────────────────────────────────────


class CentralBankCollector:
    """Fed, ECB, BoE and BoJ pages plus the curated schedule file, merged into the calendar."""

    name = "econ.central_banks"
    interval_s = 86400
    lock_group: str | None = "econ"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        now = utc_of(ctx.now())
        pages: list[tuple[str, str, Literal["bot", "browser"], Callable[..., list[EconEvent]]]] = [
            ("fed", FED_URL, "bot", parse_fomc_calendar),
            ("ecb", ECB_URL, "bot", parse_ecb_calendar),
            ("boe", BOE_URL, "browser", parse_boe_mpc_dates),
            ("boj", BOJ_URL, "browser", parse_boj_schedule),
        ]
        incoming: list[EconEvent] = []
        failed: list[str] = []
        for name, url, profile, parser in pages:
            try:
                response = await ctx.http.get(url, profile=profile)
                incoming.extend(parser(response.text, fetched_at=response.fetched_at))
            except (FetchError, ValueError) as exc:
                failed.append(name)
                ctx.log.warning("central bank page %s failed: %s", name, exc)
        if len(failed) == len(pages):
            raise FetchError(FED_URL, None, "every central-bank page failed")
        incoming.extend(load_curated_schedules(ctx.settings.config_file("central_banks.yaml"), fetched_at=now))
        bounds = padded_bounds(incoming)
        if bounds is None:
            return CollectorResult(items=0, notes=[f"pages failed: {', '.join(failed)}"] if failed else [])
        changed = store_merge(ctx, incoming, bounds[0], bounds[1], now)
        notes = [f"pages failed: {', '.join(failed)}"] if failed else []
        return CollectorResult(items=len(changed), notes=notes)


__all__ = [
    "BOE_URL",
    "BOJ_URL",
    "ECB_URL",
    "FED_URL",
    "CentralBankCollector",
    "load_curated_schedules",
    "parse_boe_mpc_dates",
    "parse_boj_schedule",
    "parse_ecb_calendar",
    "parse_fomc_calendar",
]
