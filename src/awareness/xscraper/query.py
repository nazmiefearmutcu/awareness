"""Query and timing helpers for the X scraper."""

from __future__ import annotations

import re
from datetime import timedelta
from urllib.parse import urlparse

_LOOKBACK_TOKEN = re.compile(r"(?P<value>\d+(?:\.\d+)?)(?P<unit>[smhdw])", re.IGNORECASE)
_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
    "w": 7 * 24 * 60 * 60,
}


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def normalize_handle(handle: str) -> str:
    """Return a canonical X handle without @ or URL decorations."""
    raw = handle.strip()
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urlparse(raw)
        pieces = [piece for piece in parsed.path.split("/") if piece]
        if not pieces:
            return ""
        raw = pieces[0]
    raw = raw.lstrip("@").strip()
    raw = raw.split("?", 1)[0].split("#", 1)[0].strip()
    return raw


def _format_keyword(term: str) -> str:
    term = term.strip()
    if not term:
        return ""
    if term.startswith('"') and term.endswith('"') and len(term) >= 2:
        return term
    if any(ch.isspace() for ch in term):
        return f'"{term}"'
    return term


def parse_lookback(value: str | timedelta | None) -> timedelta:
    """Parse a compact relative lookback such as ``10m`` or ``2h30m``.

    Supported suffixes:
    - ``s`` seconds
    - ``m`` minutes
    - ``h`` hours
    - ``d`` days
    - ``w`` weeks

    Composite forms are allowed, e.g. ``1d12h`` or ``2h30m``.
    """
    if value is None:
        raise ValueError("lookback is required")
    if isinstance(value, timedelta):
        if value.total_seconds() < 0:
            raise ValueError("lookback cannot be negative")
        return value
    text = value.strip().lower()
    if not text:
        raise ValueError("lookback cannot be empty")
    if text in {"now", "0", "0s", "0m", "0h"}:
        return timedelta(0)
    compact = text.replace(" ", "")
    pos = 0
    total_seconds = 0.0
    for match in _LOOKBACK_TOKEN.finditer(compact):
        if match.start() != pos:
            raise ValueError(f"Invalid lookback window: {value!r}")
        amount = float(match.group("value"))
        unit = match.group("unit").lower()
        total_seconds += amount * _UNIT_SECONDS[unit]
        pos = match.end()
    if pos != len(compact):
        raise ValueError(f"Invalid lookback window: {value!r}")
    if total_seconds < 0:
        raise ValueError(f"Invalid lookback window: {value!r}")
    return timedelta(seconds=total_seconds)


def build_search_query(
    *,
    keywords: list[str] | None = None,
    accounts: list[str] | None = None,
    raw_query: str | None = None,
    language: str | None = None,
    include_retweets: bool = False,
    include_replies: bool = False,
) -> str:
    """Build a boolean X query from structured UI fields.

    The query format is compatible with X recent search and filtered stream rules.
    """
    parts: list[str] = []

    kw_terms = _dedupe([_format_keyword(term) for term in (keywords or []) if term and term.strip()])
    if kw_terms:
        parts.append(f"({ ' OR '.join(kw_terms) })")

    handles = _dedupe([normalize_handle(handle) for handle in (accounts or []) if normalize_handle(handle)])
    if handles:
        parts.append(f"({ ' OR '.join(f'from:{handle}' for handle in handles) })")

    if raw_query and raw_query.strip():
        parts.append(f"({raw_query.strip()})")

    if language and language.strip():
        parts.append(f"lang:{language.strip()}")

    if not include_retweets:
        parts.append("-is:retweet")
    if not include_replies:
        parts.append("-is:reply")

    query = " ".join(parts).strip()
    if not query:
        raise ValueError("At least one keyword, account, or raw query term is required")
    return query
