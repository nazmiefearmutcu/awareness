"""Display-title variant labels for same-title Nasdaq rows.

Nasdaq sometimes lists two releases under one name on the same day (for example CPI m/m
and CPI y/y). Their ids already differ (``[#n]``); their display titles must too.

Only a duplicate group (same country, normalized title and UTC date) is labelled. Preference:

1. the period named in each row's description (m/m, y/y, q/q, annualized), when every row
   names a different one;
2. the unit pattern of the row's values (``%`` versus a plain level), when every row carries
   a number and the units differ;
3. an ordinal ``n of N``, only when neither source distinguishes the rows.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from awareness.markets.calendar.econ_common import parse_value

_PERIOD_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bmonth[- ]over[- ]month\b|\bm/m\b", re.IGNORECASE), "m/m"),
    (re.compile(r"\byear[- ]over[- ]year\b|\by/y\b", re.IGNORECASE), "y/y"),
    (re.compile(r"\bquarter[- ]over[- ]quarter\b|\bq/q\b", re.IGNORECASE), "q/q"),
    (re.compile(r"\bannuali[sz]ed\b", re.IGNORECASE), "annualized"),
)


@dataclass(frozen=True)
class VariantRow:
    """What a same-title row says about itself: its description and its actual/forecast/previous display values."""

    description: str | None
    values: tuple[str | None, ...]


def period_qualifier(description: str | None) -> str | None:
    """The single period named in a description, or None when none or several are named."""
    if not description:
        return None
    found = {label for pattern, label in _PERIOD_PATTERNS if pattern.search(description)}
    return found.pop() if len(found) == 1 else None


def unit_label(values: Sequence[str | None]) -> str | None:
    """``%`` or ``level`` from the row's numbers; None when no value carries a number."""
    parsed = [parse_value(v) for v in values if v is not None]
    numbers = [unit for number, unit in parsed if number is not None]
    if not numbers:
        return None
    return next((unit for unit in numbers if unit), "level")


def _distinct(labels: Sequence[str | None]) -> list[str] | None:
    """The labels when every one is present and they are pairwise distinct, else None."""
    present = [label for label in labels if label]
    if len(present) != len(labels) or len(set(present)) != len(present):
        return None
    return present


def variant_suffixes(rows: Sequence[VariantRow]) -> list[str]:
    """One label per row of a duplicate group, in row order (see the module rules)."""
    by_period = _distinct([period_qualifier(r.description) for r in rows])
    if by_period is not None:
        return by_period
    by_unit = _distinct([unit_label(r.values) for r in rows])
    if by_unit is not None:
        return by_unit
    total = len(rows)
    return [f"{index} of {total}" for index in range(1, total + 1)]
