"""Economic calendar: Nasdaq, ForexFactory and BEA parsers, provenance-preserving merge.

Honesty rules enforced here:
* A value is never multiplied by its K/M/B/T suffix; the number stays in that unit.
* Unparseable display values keep their text but their numeric fields stay ``None``.
* Surprise is computed only when both values carry the same unit.
* Provider failures raise (collectors) instead of producing placeholder rows.
"""

from __future__ import annotations

import html
import logging
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from typing import Any

import yaml
from dateutil import parser as dateparser

from awareness.markets.collectors.base import CollectorContext, CollectorResult
from awareness.markets.config import PACKAGE_DATA_DIR
from awareness.markets.http import FetchError
from awareness.markets.models import EconEvent, ProviderRef, stable_id

log = logging.getLogger(__name__)

NASDAQ_URL = "https://api.nasdaq.com/api/calendar/economicevents"
FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
BEA_URL = "https://apps.bea.gov/API/signup/release_dates.json"

# Lower rank = more trusted for values (actual / forecast / previous / unit).
VALUE_RANK: dict[str, int] = {"nasdaq": 0, "forexfactory": 1, "bea": 2}
# Lower rank = more official for scheduled time.
SCHEDULE_RANK: dict[str, int] = {
    "fed": 0, "ecb": 0, "boe": 0, "boj": 0, "tcmb": 0, "bea": 0,
    "nasdaq": 1, "forexfactory": 2,
}
FUZZY_JACCARD = 0.6
FUZZY_WINDOW = timedelta(minutes=90)
_UNKNOWN_RANK = 9

NASDAQ_COUNTRY_ISO: dict[str, str] = {
    "United States": "US", "Euro Zone": "EU", "Eurozone": "EU", "Japan": "JP",
    "United Kingdom": "GB", "Germany": "DE", "France": "FR", "Italy": "IT", "Spain": "ES",
    "Netherlands": "NL", "Belgium": "BE", "Austria": "AT", "Ireland": "IE", "Portugal": "PT",
    "Greece": "GR", "Finland": "FI", "Canada": "CA", "Australia": "AU", "New Zealand": "NZ",
    "Switzerland": "CH", "China": "CN", "Hong Kong": "HK", "Singapore": "SG",
    "South Korea": "KR", "India": "IN", "Brazil": "BR", "Mexico": "MX", "South Africa": "ZA",
    "Turkey": "TR", "Sweden": "SE", "Norway": "NO", "Denmark": "DK", "Poland": "PL",
    "Israel": "IL", "Russia": "RU", "Taiwan": "TW", "Indonesia": "ID", "Thailand": "TH",
    "Malaysia": "MY", "Philippines": "PH", "Chile": "CL", "Colombia": "CO", "Peru": "PE",
    "Argentina": "AR", "Saudi Arabia": "SA", "Czech Republic": "CZ", "Hungary": "HU",
    "Romania": "RO", "Vietnam": "VN",
}
FF_CURRENCY_ISO: dict[str, str] = {
    "USD": "US", "EUR": "EU", "GBP": "GB", "JPY": "JP", "CAD": "CA", "AUD": "AU",
    "NZD": "NZ", "CHF": "CH", "CNY": "CN", "SEK": "SE", "NOK": "NO", "BRL": "BR",
    "MXN": "MX", "ZAR": "ZA", "INR": "IN", "KRW": "KR", "TRY": "TR", "HKD": "HK",
    "SGD": "SG",
}
CURRENCY_BY_ISO: dict[str, str] = {
    "US": "USD", "EU": "EUR", "GB": "GBP", "JP": "JPY", "CA": "CAD", "AU": "AUD",
    "NZ": "NZD", "CH": "CHF", "CN": "CNY", "DE": "EUR", "FR": "EUR", "IT": "EUR",
    "ES": "EUR", "NL": "EUR", "BE": "EUR", "AT": "EUR", "IE": "EUR", "PT": "EUR",
    "GR": "EUR", "FI": "EUR", "BR": "BRL", "MX": "MXN", "ZA": "ZAR", "IN": "INR",
    "KR": "KRW", "TR": "TRY", "HK": "HKD", "SG": "SGD", "SE": "SEK", "NO": "NOK",
    "DK": "DKK", "PL": "PLN", "IL": "ILS", "RU": "RUB", "TW": "TWD", "ID": "IDR",
    "TH": "THB", "MY": "MYR", "PH": "PHP", "CL": "CLP", "CO": "COP", "PE": "PEN",
    "AR": "ARS", "SA": "SAR", "CZ": "CZK", "HU": "HUF", "RO": "RON", "VN": "VND",
}
_FF_IMPACT: dict[str, int] = {"high": 3, "medium": 2, "low": 1, "non-economic": 1}
_BLANK_VALUES = {"", "-", "--", "N/A", "NA", "\u2014", "\u2013"}
_CURRENCY_SYMBOLS = "$€£¥"
_NUMBER_RE = re.compile(r"([+-])?\s*(\d+(?:\.\d+)?|\.\d+)\s*(%|[KMBTkmbt])?")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_TAG_RE = re.compile(r"<[^>]+>")
_PAREN_PREL_FINAL_RE = re.compile(r"\((prel|final)\)", re.IGNORECASE)
_CHANGE_RES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bm/m\b"), "mom"),
    (re.compile(r"\by/y\b"), "yoy"),
    (re.compile(r"\bq/q\b"), "qoq"),
)


# ── values ────────────────────────────────────────────────────────────────


def parse_value(raw: str | None) -> tuple[float | None, str | None]:
    """Parse a provider display value into ``(number, unit)``.

    ``"250K"`` -> ``(250.0, "K")`` (never multiplied). ``"0.3%"`` -> ``(0.3, "%")``.
    ``"$1.2B"`` -> ``(1.2, "B")``. ``"$50"`` -> ``(50.0, "$")``. Anything that is not
    a plain number with an optional suffix -> ``(None, None)``.
    """
    if raw is None:
        return None, None
    text = html.unescape(str(raw)).replace("\xa0", " ").replace("\u2212", "-").strip()
    text = text.replace(",", "")
    if text.upper() in _BLANK_VALUES or not text:
        return None, None
    text = text.lstrip("<>≤≥ ").strip()
    currency: str | None = None
    if text and text[0] in _CURRENCY_SYMBOLS:
        currency = text[0]
        text = text[1:].strip()
    negative = False
    if text.startswith("(") and ")" in text:
        close = text.index(")")
        text = (text[1:close] + text[close + 1:]).strip()
        negative = True
    match = _NUMBER_RE.fullmatch(text)
    if match is None:
        return None, None
    value = float(match.group(2))
    if match.group(1) == "-" or negative:
        value = -value
    suffix = match.group(3)
    if suffix == "%":
        unit: str | None = "%"
    elif suffix:
        unit = suffix.upper()
    else:
        unit = currency
    return value, unit


def _display(raw: Any) -> str | None:
    """Provider display text: unescape, drop NBSP, collapse whitespace, empty -> None."""
    if raw is None:
        return None
    text = html.unescape(str(raw)).replace("\xa0", " ")
    text = " ".join(text.split())
    return text or None


def _clean_text(raw: Any, limit: int = 600) -> str | None:
    if raw is None:
        return None
    text = _TAG_RE.sub(" ", html.unescape(str(raw)))
    text = " ".join(text.split())
    return text[:limit] or None


def _unit_of(display: str | None, fallback: str | None) -> str | None:
    if display is None:
        return fallback
    return parse_value(display)[1]


# ── titles / ids ──────────────────────────────────────────────────────────


def normalize_title(title: str) -> str:
    """Lower-case matching key: ``m/m``->``mom``, ``y/y``->``yoy``, ``q/q``->``qoq``,
    punctuation and ``(prel)``/``(final)`` removed, whitespace collapsed."""
    text = html.unescape(title).lower()
    text = _PAREN_PREL_FINAL_RE.sub(" ", text)
    for pattern, replacement in _CHANGE_RES:
        text = pattern.sub(replacement, text)
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def event_id(country: str, title: str, scheduled_at: datetime) -> str:
    """Deterministic id from country, normalized title and UTC date."""
    return stable_id("econ", country, normalize_title(title), _utc(scheduled_at).date().isoformat())


# ── importance / category rules ───────────────────────────────────────────


@dataclass(frozen=True)
class _Rule:
    pattern: re.Pattern[str]
    importance: int
    category: str
    chair_importance: int | None


@dataclass(frozen=True)
class _Table:
    rules: tuple[_Rule, ...]
    chairs: re.Pattern[str] | None
    default_importance: int
    default_category: str


@lru_cache(maxsize=1)
def _table() -> _Table:
    raw: Mapping[str, Any] = yaml.safe_load((PACKAGE_DATA_DIR / "econ_importance.yaml").read_text(encoding="utf-8"))
    rules = tuple(
        _Rule(
            pattern=re.compile(item["pattern"], re.IGNORECASE),
            importance=int(item["importance"]),
            category=str(item["category"]),
            chair_importance=int(item["chair_importance"]) if "chair_importance" in item else None,
        )
        for item in raw["rules"]
    )
    chair_names = [str(name) for name in raw.get("chair_names", [])]
    chairs = re.compile(r"\b(?:" + "|".join(map(re.escape, chair_names)) + r")\b", re.IGNORECASE) if chair_names else None
    default = raw["default"]
    return _Table(rules, chairs, int(default["importance"]), str(default["category"]))


def _classify(title: str) -> tuple[int, str]:
    """Importance (1-3) and category for a title from the ordered rule table."""
    table = _table()
    for rule in table.rules:
        if rule.pattern.search(title):
            importance = rule.importance
            if rule.chair_importance is not None and table.chairs is not None and table.chairs.search(title):
                importance = rule.chair_importance
            return importance, rule.category
    return table.default_importance, table.default_category


# ── Nasdaq ────────────────────────────────────────────────────────────────


def nasdaq_url(day: date) -> str:
    return f"{NASDAQ_URL}?date={day.isoformat()}"


def _nasdaq_time(gmt: Any, day: date) -> tuple[datetime, bool]:
    match = _TIME_RE.match(str(gmt or "").strip())
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour < 24 and 0 <= minute < 60:
            return datetime.combine(day, time(hour, minute), UTC), False
    return datetime.combine(day, time(0, 0), UTC), True


def parse_nasdaq_econ(payload: dict[str, Any], *, day: date, fetched_at: datetime) -> list[EconEvent]:
    """Parse one day of the Nasdaq economic-events API payload."""
    data = (payload or {}).get("data") or {}
    rows = data.get("rows") or []
    ref = ProviderRef(provider="nasdaq", url=nasdaq_url(day), fetched_at=_utc(fetched_at))

    prepared: list[tuple[dict[str, Any], str, str, datetime, bool]] = []
    for row in rows:
        country_name = _display(row.get("country"))
        iso = NASDAQ_COUNTRY_ISO.get(country_name or "")
        title = _display(row.get("eventName"))
        if iso is None or title is None:
            log.debug("nasdaq econ: skipping row country=%r title=%r", country_name, title)
            continue
        scheduled, all_day = _nasdaq_time(row.get("gmt"), day)
        prepared.append((row, iso, title, scheduled, all_day))

    group_sizes = Counter((iso, normalize_title(title), at.date()) for _, iso, title, at, _ in prepared)
    seen: Counter[tuple[str, str, date]] = Counter()
    events: list[EconEvent] = []
    for row, iso, title, scheduled, all_day in prepared:
        key = (iso, normalize_title(title), scheduled.date())
        id_title = title
        if group_sizes[key] > 1:
            seen[key] += 1
            id_title = f"{title} [#{seen[key]}]"  # same-day duplicates stay distinct
        actual = _display(row.get("actual"))
        consensus = _display(row.get("consensus"))
        previous = _display(row.get("previous"))
        actual_value, actual_unit = parse_value(actual)
        forecast_value, forecast_unit = parse_value(consensus)
        previous_value, previous_unit = parse_value(previous)
        unit = next((u for u in (actual_unit, forecast_unit, previous_unit) if u), None)
        importance, category = _classify(title)
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
                unit=unit,
                description=_clean_text(row.get("description")),
                sources=[ref],
            )
        )
    return events


# ── ForexFactory ──────────────────────────────────────────────────────────


def parse_forexfactory(payload: list[dict[str, Any]], *, fetched_at: datetime) -> list[EconEvent]:
    """Parse the ForexFactory 'this week' JSON. Holidays are skipped."""
    if not isinstance(payload, list):
        raise ValueError("unexpected ForexFactory payload shape")
    ref = ProviderRef(provider="forexfactory", url=FF_URL, fetched_at=_utc(fetched_at))
    out: dict[str, EconEvent] = {}
    for item in payload:
        impact = str(item.get("impact") or "").strip()
        if impact.lower() == "holiday":
            continue
        currency = str(item.get("country") or "").strip().upper()
        iso = FF_CURRENCY_ISO.get(currency)
        title = _display(item.get("title"))
        if iso is None or title is None:
            log.debug("forexfactory: skipping currency=%r title=%r", currency, title)
            continue
        try:
            when = dateparser.isoparse(str(item["date"]))
        except (KeyError, ValueError, TypeError):
            log.debug("forexfactory: unparseable date for %r", title)
            continue
        all_day = "all day" in title.lower()
        if all_day:
            scheduled = datetime.combine(when.date(), time(0, 0), UTC)
        else:
            scheduled = _utc(when)
        ff_importance = _FF_IMPACT.get(impact.lower(), 1)
        table_importance, category = _classify(title)
        forecast = _display(item.get("forecast"))
        previous = _display(item.get("previous"))
        actual = _display(item.get("actual"))
        actual_value, actual_unit = parse_value(actual)
        forecast_value, forecast_unit = parse_value(forecast)
        previous_value, previous_unit = parse_value(previous)
        unit = next((u for u in (actual_unit, forecast_unit, previous_unit) if u), None)
        event = EconEvent(
            id=event_id(iso, title, scheduled),
            country=iso,
            currency=currency,
            title=title,
            category=category,
            scheduled_at=scheduled,
            all_day=all_day,
            importance=max(ff_importance, table_importance),
            actual=actual,
            forecast=forecast,
            previous=previous,
            actual_value=actual_value,
            forecast_value=forecast_value,
            previous_value=previous_value,
            unit=unit,
            sources=[ref],
        )
        out.setdefault(event.id, event)
    return list(out.values())


# ── BEA ───────────────────────────────────────────────────────────────────


def parse_bea_release_dates(
    payload: dict[str, Any], *, fetched_at: datetime, start: date, end: date
) -> list[EconEvent]:
    """US release dates from BEA, filtered to ``[start, end]`` (UTC dates, inclusive)."""
    ref = ProviderRef(provider="bea", url=BEA_URL, fetched_at=_utc(fetched_at), basis="native")
    out: dict[str, EconEvent] = {}
    for name, info in (payload or {}).items():
        title = _display(name)
        if title is None or not isinstance(info, dict):
            continue  # top-level metadata such as "file_last_updated" is not a release
        importance, category = _classify(title)
        for raw in info.get("release_dates") or []:
            try:
                when = _utc(dateparser.isoparse(str(raw)))
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


# ── finalize ──────────────────────────────────────────────────────────────


def finalize_event(event: EconEvent, *, now: datetime) -> EconEvent:
    """Derive surprise, direction and status from the stored values.

    Surprise needs both numeric values in the same unit; otherwise it stays ``None``.
    ``cancelled`` and ``revised`` are kept as set; otherwise ``released`` when an
    actual is present, else ``scheduled``.
    """
    surprise: float | None = None
    direction: str | None = None
    if event.actual_value is not None and event.forecast_value is not None:
        actual_unit = _unit_of(event.actual, event.unit)
        forecast_unit = _unit_of(event.forecast, event.unit)
        if actual_unit == forecast_unit:
            surprise = event.actual_value - event.forecast_value
            if abs(surprise) < 1e-9:
                direction = "inline"
            else:
                direction = "above" if surprise > 0 else "below"
    if event.status in ("cancelled", "revised"):
        status = event.status
    elif event.actual is not None or event.actual_value is not None:
        status = "released"
    else:
        status = "scheduled"
    return event.model_copy(update={"surprise": surprise, "surprise_direction": direction, "status": status})


# ── merge ─────────────────────────────────────────────────────────────────


def _best_rank(event: EconEvent | None, table: Mapping[str, int]) -> int:
    if event is None:
        return _UNKNOWN_RANK
    return min((table.get(s.provider, _UNKNOWN_RANK) for s in event.sources), default=_UNKNOWN_RANK)


def _pick(old: Any, old_rank: int, new: Any, new_rank: int) -> Any:
    if old is None:
        return new
    if new is None:
        return old
    return new if new_rank < old_rank else old


def _first(*values: Any) -> Any:
    return next((v for v in values if v is not None), None)


def _merge_sources(old: Iterable[ProviderRef], new: Iterable[ProviderRef]) -> list[ProviderRef]:
    by_provider: dict[str, ProviderRef] = {}
    for ref in old:
        by_provider[ref.provider] = ref
    for ref in new:
        current = by_provider.get(ref.provider)
        if current is None or ref.fetched_at >= current.fetched_at:
            by_provider[ref.provider] = ref
    return list(by_provider.values())


def _merge_pair(old: EconEvent, new: EconEvent) -> EconEvent:
    old_value_rank = _best_rank(old, VALUE_RANK)
    new_value_rank = _best_rank(new, VALUE_RANK)

    def pick(a: Any, b: Any) -> Any:
        return _pick(a, old_value_rank, b, new_value_rank)

    # Schedule: never let an all-day placeholder replace a timed value.
    if old.all_day != new.all_day:
        take_new = old.all_day and not new.all_day
    else:
        take_new = _best_rank(new, SCHEDULE_RANK) < _best_rank(old, SCHEDULE_RANK)
    scheduled_source = new if take_new else old

    statuses = {old.status, new.status}
    if "cancelled" in statuses:
        status = "cancelled"
    elif "revised" in statuses:
        status = "revised"
    else:
        status = "scheduled"

    return old.model_copy(
        update={
            "actual": pick(old.actual, new.actual),
            "actual_value": pick(old.actual_value, new.actual_value),
            "forecast": pick(old.forecast, new.forecast),
            "forecast_value": pick(old.forecast_value, new.forecast_value),
            "previous": pick(old.previous, new.previous),
            "previous_value": pick(old.previous_value, new.previous_value),
            "unit": pick(old.unit, new.unit),
            "revised": _first(old.revised, new.revised),
            "scheduled_at": scheduled_source.scheduled_at,
            "all_day": scheduled_source.all_day,
            "importance": max(old.importance, new.importance),
            "category": _first(old.category, new.category),
            "description": _first(old.description, new.description),
            "currency": _first(old.currency, new.currency),
            "status": status,
            "sources": _merge_sources(old.sources, new.sources),
        }
    )


def _tokens(title: str) -> set[str]:
    return set(normalize_title(title).split())


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _find_fuzzy(working: Mapping[str, EconEvent], incoming: EconEvent) -> str | None:
    """Existing event that is the same release: same country, title tokens Jaccard >= 0.6,
    and within 90 minutes (or the same UTC day when either side is all-day)."""
    incoming_tokens = _tokens(incoming.title)
    incoming_providers = {s.provider for s in incoming.sources}
    best_id: str | None = None
    best_key: tuple[float, float] | None = None
    for event_key, candidate in working.items():
        if candidate.country != incoming.country:
            continue
        # Only cross-provider matches: one provider's own near-duplicates (e.g. ForexFactory
        # maps every euro-area PMI to "EU") must never collapse into each other.
        if incoming_providers & {s.provider for s in candidate.sources}:
            continue
        similarity = _jaccard(_tokens(candidate.title), incoming_tokens)
        if similarity < FUZZY_JACCARD:
            continue
        if candidate.all_day or incoming.all_day:
            if candidate.scheduled_at.date() != incoming.scheduled_at.date():
                continue
            delta_s = 0.0
        else:
            delta = abs((candidate.scheduled_at - incoming.scheduled_at).total_seconds())
            if delta > FUZZY_WINDOW.total_seconds():
                continue
            delta_s = delta
        key = (-similarity, delta_s)
        if best_key is None or key < best_key:
            best_id, best_key = event_key, key
    return best_id


def merge_econ_events(existing: list[EconEvent], incoming: list[EconEvent], *, now: datetime) -> list[EconEvent]:
    """Merge incoming provider events into the existing set.

    Returns only events that are new or whose stored content changed (``updated_at``
    excluded from the comparison), each stamped with ``updated_at = now``.
    """
    stamp = _utc(now)
    original = {event.id: event for event in existing}
    working: dict[str, EconEvent] = dict(original)
    touched: dict[str, None] = {}
    for event in incoming:
        if event.id in working:
            target: str | None = event.id
        else:
            target = _find_fuzzy(working, event)
        if target is None:
            working[event.id] = event
            touched[event.id] = None
        else:
            working[target] = _merge_pair(working[target], event)
            touched[target] = None

    changed: list[EconEvent] = []
    for event_key in touched:
        final = finalize_event(working[event_key], now=stamp)
        prior = original.get(event_key)
        if prior is None or _content(prior) != _content(final):
            changed.append(final.model_copy(update={"updated_at": stamp}))
    return changed


def _content(event: EconEvent) -> dict[str, Any]:
    return event.model_dump(mode="json", exclude={"updated_at"})


# ── collectors ────────────────────────────────────────────────────────────


def _store_merge(
    ctx: CollectorContext, incoming: list[EconEvent], start: datetime, end: datetime, now: datetime
) -> list[EconEvent]:
    existing = ctx.store.query_econ(start, end)
    changed = merge_econ_events(existing, incoming, now=now)
    ctx.store.upsert_econ_events(changed)
    return changed


def _day_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    return datetime.combine(start, time(0, 0), UTC), datetime.combine(end, time(23, 59, 59), UTC)


def _padded_bounds(events: list[EconEvent]) -> tuple[datetime, datetime] | None:
    if not events:
        return None
    lowest = min(e.scheduled_at for e in events) - timedelta(days=1)
    highest = max(e.scheduled_at for e in events) + timedelta(days=1)
    return lowest, highest


class NasdaqEconCollector:
    """Daily Nasdaq economic-events pages for ``today - econ_days_back .. today + econ_days_ahead``."""

    name = "econ.nasdaq"
    interval_s = 1800
    lock_group: str | None = "econ"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        now = _utc(ctx.now())
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
        start, end = _day_bounds(start_day, end_day)
        changed = _store_merge(ctx, incoming, start, end, now)
        notes = [f"{len(failed)} of {len(days)} day(s) failed"] if failed else []
        return CollectorResult(items=len(changed), notes=notes)


class ForexFactoryCollector:
    """ForexFactory 'this week' calendar."""

    name = "econ.forexfactory"
    interval_s = 3600
    lock_group: str | None = "econ"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        now = _utc(ctx.now())
        response = await ctx.http.get(FF_URL, profile="browser")
        incoming = parse_forexfactory(response.json(), fetched_at=response.fetched_at)
        bounds = _padded_bounds(incoming)
        if bounds is None:
            return CollectorResult(items=0, notes=["no events in feed"])
        changed = _store_merge(ctx, incoming, bounds[0], bounds[1], now)
        return CollectorResult(items=len(changed))


class BeaCollector:
    """BEA release dates for ``today - 7 .. today + 120`` days."""

    name = "econ.bea"
    interval_s = 86400
    lock_group: str | None = "econ"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        now = _utc(ctx.now())
        today = now.date()
        start_day, end_day = today - timedelta(days=7), today + timedelta(days=120)
        response = await ctx.http.get(BEA_URL, profile="browser")
        incoming = parse_bea_release_dates(
            response.json(), fetched_at=response.fetched_at, start=start_day, end=end_day
        )
        start, end = _day_bounds(start_day, end_day)
        changed = _store_merge(ctx, incoming, start, end, now)
        return CollectorResult(items=len(changed))
