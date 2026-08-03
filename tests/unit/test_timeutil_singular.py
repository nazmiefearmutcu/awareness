"""M-32 + M-03: relative time parsing — singular "day ago", empty end = now."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from awareness.util.timeutil import coerce_relative_end, inclusive_end, to_utc, utcnow


def test_to_utc_singular_day_ago() -> None:
    dt = to_utc("1 day ago")
    assert dt is not None
    assert isinstance(dt, datetime)
    assert abs((utcnow() - dt).total_seconds() - 86400) < 60


def test_to_utc_plural_days_ago_still_works() -> None:
    dt = to_utc("3 days ago")
    assert dt is not None
    assert abs((utcnow() - dt).total_seconds() - 3 * 86400) < 60


def test_to_utc_case_and_space_tolerant() -> None:
    assert to_utc("1 DAY ago") is not None
    assert to_utc("1  day   ago") is not None


def test_coerce_relative_end_empty_means_now() -> None:
    now = utcnow()
    dt = coerce_relative_end("")
    assert isinstance(dt, datetime)
    assert dt.tzinfo is not None
    assert abs((dt - now).total_seconds()) < 60


def test_coerce_relative_end_whitespace_means_now() -> None:
    dt = coerce_relative_end("   ")
    assert isinstance(dt, datetime)


def test_coerce_relative_end_still_rejects_garbage() -> None:
    import pytest

    with pytest.raises(ValueError):
        coerce_relative_end("definitely not a date")


def test_inclusive_end_with_relative_end() -> None:
    end = inclusive_end(to_utc("1 day ago"))
    assert end is not None
    assert isinstance(end, datetime)
    assert end.tzinfo is not None
    # "1 day ago" resolves to a non-midnight timestamp → unchanged shape.
    assert (utcnow() - end).total_seconds() < 2 * 86400
