"""Shared economic-calendar helpers: value parsing, titles, ids, importance rules, country maps.

Honesty rules enforced here:
* A value is never multiplied by its K/M/B/T suffix; the number stays in that unit.
* Unparseable display values keep their text but their numeric fields stay ``None``.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

import yaml

from awareness.markets.config import PACKAGE_DATA_DIR
from awareness.markets.models import stable_id

log = logging.getLogger(__name__)

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
# ForexFactory labels every euro-area release EUR. The title prefix names the member state.
_EURO_MEMBER_PREFIX: dict[str, str] = {
    "german": "DE", "french": "FR", "italian": "IT", "spanish": "ES", "dutch": "NL",
    "belgian": "BE", "austrian": "AT", "irish": "IE", "portuguese": "PT", "greek": "GR",
    "finnish": "FI",
}
_EURO_PREFIX_RE = re.compile(r"^(" + "|".join(_EURO_MEMBER_PREFIX) + r")\b", re.IGNORECASE)
_EURO_AREA_RE = re.compile(r"^(eurozone|euro zone|euro area|eu|ecb)\b", re.IGNORECASE)
# Normalized policy-rate titles (see normalize_title) that ForexFactory uses for decisions.
RATE_DECISION_TITLES: frozenset[str] = frozenset(
    {
        "federal funds rate", "official bank rate", "main refinancing rate", "boj policy rate",
        "cash rate", "official cash rate", "deposit facility rate",
    }
)

_BLANK_VALUES = {"", "-", "--", "N/A", "NA", "\u2014", "\u2013"}
_CURRENCY_SYMBOLS = "$€£¥"
_NUMBER_RE = re.compile(r"([+-])?\s*(\d+(?:\.\d+)?|\.\d+)\s*(%|[KMBTkmbt])?")
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
    # Every comma is treated as a US thousands separator. Nasdaq and ForexFactory use US
    # formatting, so a European decimal comma ("1,2%") would read as 12%; do not feed
    # European-formatted values into this parser.
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


def display(raw: Any) -> str | None:
    """Provider display text: unescape, drop NBSP, collapse whitespace, empty -> None."""
    if raw is None:
        return None
    text = html.unescape(str(raw)).replace("\xa0", " ")
    text = " ".join(text.split())
    return text or None


def clean_text(raw: Any, limit: int = 600) -> str | None:
    """Strip HTML tags and whitespace from a provider description; cap its length."""
    if raw is None:
        return None
    text = _TAG_RE.sub(" ", html.unescape(str(raw)))
    text = " ".join(text.split())
    return text[:limit] or None


def unit_of(shown: str | None, fallback: str | None) -> str | None:
    """Unit of a display string; ``fallback`` only when there is no display string."""
    if shown is None:
        return fallback
    return parse_value(shown)[1]


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


def utc_of(value: datetime) -> datetime:
    """Timezone-aware UTC datetime; naive values are taken as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def event_id(country: str, title: str, scheduled_at: datetime) -> str:
    """Deterministic id from country, normalized title and UTC date."""
    return stable_id("econ", country, normalize_title(title), utc_of(scheduled_at).date().isoformat())


def euro_member_iso(title: str) -> str:
    """ISO code for a EUR-denominated release: member state from the title prefix, else EU."""
    prefix = _EURO_PREFIX_RE.match(title.strip())
    if prefix:
        return _EURO_MEMBER_PREFIX[prefix.group(1).lower()]
    return "EU"


def is_rate_decision_title(title: str) -> bool:
    """True for ForexFactory policy-rate rows such as "Federal Funds Rate"."""
    return normalize_title(title) in RATE_DECISION_TITLES


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
    raw: dict[str, Any] = yaml.safe_load((PACKAGE_DATA_DIR / "econ_importance.yaml").read_text(encoding="utf-8"))
    rules = tuple(
        _Rule(
            pattern=re.compile(str(item["pattern"]), re.IGNORECASE),
            importance=int(item["importance"]),
            category=str(item["category"]),
            chair_importance=int(item["chair_importance"]) if "chair_importance" in item else None,
        )
        for item in raw["rules"]
    )
    chair_names = [str(name) for name in raw.get("chair_names", [])]
    chairs = (
        re.compile(r"\b(?:" + "|".join(map(re.escape, chair_names)) + r")\b", re.IGNORECASE)
        if chair_names
        else None
    )
    default = raw["default"]
    return _Table(rules, chairs, int(default["importance"]), str(default["category"]))


def classify(title: str) -> tuple[int, str]:
    """Importance (1-3) and category for a title from the ordered rule table."""
    table = _table()
    for rule in table.rules:
        if rule.pattern.search(title):
            importance = rule.importance
            if rule.chair_importance is not None and table.chairs is not None and table.chairs.search(title):
                importance = rule.chair_importance
            return importance, rule.category
    return table.default_importance, table.default_category
