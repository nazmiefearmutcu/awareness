"""Time utilities. All public timestamps are UTC tz-aware datetimes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from dateutil import parser as _dateutil_parser


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


def to_utc(dt: datetime | str | None) -> datetime | None:
    """Convert a value to a UTC tz-aware datetime."""
    if dt is None:
        return None
    if isinstance(dt, str):
        s = dt.strip().lower()
        if s in ("now", "today"):
            return utcnow()
        if s.endswith("days ago"):
            try:
                days = int(s.split()[0])
                from datetime import timedelta
                return utcnow() - timedelta(days=days)
            except (ValueError, IndexError):
                pass
        try:
            dt = _dateutil_parser.parse(dt)
        except (ValueError, TypeError):
            return None
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_http_date(value: str | None) -> datetime | None:
    """Parse an RFC 7231 HTTP date header. Returns UTC dt or None."""
    if not value:
        return None
    try:
        dt = _dateutil_parser.parse(value)
        if isinstance(dt, datetime):
            return dt.astimezone(UTC)
        return None
    except (ValueError, TypeError):
        return None


def iso(dt: datetime | None) -> str | None:
    """ISO-8601 representation in UTC, ``None`` passthrough."""
    if dt is None:
        return None
    return to_utc(dt).isoformat() if dt else None  # type: ignore[union-attr]


def floor_to_day(dt: datetime) -> datetime:
    dt = to_utc(dt)  # type: ignore[assignment]
    assert dt is not None
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def inclusive_end(dt: datetime | str | None) -> datetime | None:
    """Expand date-only / midnight UTC ends to inclusive end-of-day.

    API/CLI users often pass bare ``YYYY-MM-DD`` (or midnight from
    ``<input type=date>`` / Pydantic). With ``fetch_ts <= end``, midnight
    excludes every same-day capture after 00:00. When the bound is exactly
    00:00:00.000000 UTC, expand to 23:59:59.999999 UTC so the full calendar
    day is included. Non-midnight timestamps (and ``None``) are unchanged.
    """
    utc = to_utc(dt)
    if utc is None:
        return None
    if (utc.hour, utc.minute, utc.second, utc.microsecond) == (0, 0, 0, 0):
        return utc.replace(hour=23, minute=59, second=59, microsecond=999999)
    return utc


def coerce_relative_end(end: Any) -> datetime:
    """Allow ``now``/``today`` literal end markers in CLI/API payloads."""
    if isinstance(end, datetime):
        return to_utc(end)  # type: ignore[return-value]
    if isinstance(end, str):
        s = end.strip().lower()
        if s in ("now", "today"):
            return utcnow()
        parsed = _dateutil_parser.parse(end)
        return to_utc(parsed)  # type: ignore[return-value]
    raise ValueError(f"Cannot coerce {end!r} to a UTC datetime")
