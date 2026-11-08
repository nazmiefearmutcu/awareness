"""Economic-calendar merge: one release from several providers becomes one record.

Rules
-----
* Matching: same id; or a persisted alias (an earlier fuzzy match); or a fuzzy match
  against an event from a DIFFERENT provider (same country, title-token Jaccard >= 0.6,
  within 90 minutes or the same UTC day when either side is all-day); or a central-bank
  decision matched to a ForexFactory policy-rate row on the same country and UTC day.
* Values (actual / forecast / previous): a newer snapshot from the provider that supplied
  the stored values replaces all of them; a higher-ranked provider replaces them; a
  lower-ranked provider only fills fields that are still None.
* Schedule: a timed value is never replaced by an all-day one; otherwise the more official
  source wins. The title comes from the more official source (central bank > exchange/
  calendar feeds > ForexFactory).
* Idempotent: merging the same inputs again returns ``[]`` (the alias map carries the
  fuzzy matches across runs, so the second run does not insert a duplicate).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from awareness.markets.calendar.econ_common import (
    is_rate_decision_title,
    normalize_title,
    parse_value,
    unit_of,
    utc_of,
)
from awareness.markets.models import EconEvent, ProviderRef

FUZZY_JACCARD = 0.6
FUZZY_WINDOW = timedelta(minutes=90)
# Lower rank = more trusted for values (actual / forecast / previous / unit).
VALUE_RANK: dict[str, int] = {"nasdaq": 0, "forexfactory": 1, "bea": 2}
# Lower rank = more official for scheduled time and title.
SCHEDULE_RANK: dict[str, int] = {
    "fed": 0, "ecb": 0, "boe": 0, "boj": 0, "tcmb": 0, "bea": 0,
    "nasdaq": 1, "forexfactory": 2,
}
VALUE_FIELDS: tuple[str, ...] = ("actual", "actual_value", "forecast", "forecast_value", "previous", "previous_value")
_UNKNOWN_RANK = 9


@dataclass
class MergeState:
    """Side state that must survive between runs.

    ``aliases`` maps an incoming event id to the stored event it was fuzzy-merged into.
    ``value_provider`` maps a stored event id to the provider that supplied its values.
    """

    aliases: dict[str, str] = field(default_factory=dict)
    value_provider: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_json(cls, raw: Any) -> MergeState:
        if not isinstance(raw, dict):
            return cls()
        return cls(
            aliases=dict(raw.get("aliases") or {}),
            value_provider=dict(raw.get("value_provider") or {}),
        )

    def to_json(self) -> dict[str, Any]:
        return {"aliases": dict(self.aliases), "value_provider": dict(self.value_provider)}


# ── finalize ──────────────────────────────────────────────────────────────


def finalize_event(event: EconEvent, *, now: Any) -> EconEvent:
    """Derive surprise, direction and status from the stored values.

    Surprise needs both numeric values in the same unit; otherwise it stays ``None``.
    ``cancelled`` and ``revised`` are kept as set; otherwise ``released`` when an actual
    is present, else ``scheduled``. ``now`` is accepted for API symmetry and not stored here.
    """
    surprise: float | None = None
    direction: str | None = None
    if event.actual_value is not None and event.forecast_value is not None:
        if unit_of(event.actual, event.unit) == unit_of(event.forecast, event.unit):
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


# ── helpers ───────────────────────────────────────────────────────────────


def _providers(event: EconEvent) -> set[str]:
    return {s.provider for s in event.sources}


def _best_provider(event: EconEvent, table: Mapping[str, int]) -> str | None:
    best: str | None = None
    for source in event.sources:
        if best is None or table.get(source.provider, _UNKNOWN_RANK) < table.get(best, _UNKNOWN_RANK):
            best = source.provider
    return best


def _best_rank(event: EconEvent, table: Mapping[str, int]) -> int:
    return min((table.get(s.provider, _UNKNOWN_RANK) for s in event.sources), default=_UNKNOWN_RANK)


def _has_values(event: EconEvent) -> bool:
    return any(getattr(event, name) is not None for name in VALUE_FIELDS)


def _incoming_provider(event: EconEvent) -> str | None:
    return event.sources[0].provider if event.sources else None


def _supplier(event: EconEvent, state: MergeState) -> str | None:
    return state.value_provider.get(event.id) or _best_provider(event, VALUE_RANK)


def _merge_sources(old: Iterable[ProviderRef], new: Iterable[ProviderRef]) -> list[ProviderRef]:
    by_provider: dict[str, ProviderRef] = {}
    for ref in old:
        by_provider[ref.provider] = ref
    for ref in new:
        current = by_provider.get(ref.provider)
        if current is None or ref.fetched_at >= current.fetched_at:
            by_provider[ref.provider] = ref
    return list(by_provider.values())


def _is_rate_pair(a: EconEvent, b: EconEvent) -> bool:
    """A central-bank decision and a ForexFactory policy-rate row on the same country and UTC day."""
    for central, ff in ((a, b), (b, a)):
        if (
            central.category == "central_bank"
            and "forexfactory" not in _providers(central)
            and "forexfactory" in _providers(ff)
            and is_rate_decision_title(ff.title)
            and central.country == ff.country
            and central.scheduled_at.date() == ff.scheduled_at.date()
        ):
            return True
    return False


def _tokens(title: str) -> set[str]:
    return set(normalize_title(title).split())


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _find_fuzzy(working: Mapping[str, EconEvent], incoming: EconEvent) -> str | None:
    """Stored event from a DIFFERENT provider that is the same release (see module rules)."""
    incoming_providers = _providers(incoming)
    incoming_tokens = _tokens(incoming.title)
    best_id: str | None = None
    best_key: tuple[float, float] | None = None
    for key, candidate in working.items():
        if candidate.country != incoming.country:
            continue
        # Same-provider near-duplicates must never collapse into each other.
        if incoming_providers & _providers(candidate):
            continue
        if _is_rate_pair(candidate, incoming):
            rank_key: tuple[float, float] = (-2.0, 0.0)
        else:
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
            rank_key = (-similarity, delta_s)
        if best_key is None or rank_key < best_key:
            best_id, best_key = key, rank_key
    return best_id


def _resolve(event: EconEvent, working: Mapping[str, EconEvent], state: MergeState) -> str | None:
    if event.id in working:
        return event.id
    alias = state.aliases.get(event.id)
    if alias is not None and alias in working:
        return alias  # authoritative: an earlier run fuzzy-merged this id here
    return _find_fuzzy(working, event)


# ── pair merge ────────────────────────────────────────────────────────────


def _merge_values(old: EconEvent, new: EconEvent, state: MergeState) -> dict[str, Any]:
    new_provider = _incoming_provider(new)
    old_supplier = _supplier(old, state)
    old_has, new_has = _has_values(old), _has_values(new)
    if old_supplier is not None and new_provider == old_supplier:
        replace = True  # newer snapshot from the same provider always replaces its values
    elif not old_has and new_has:
        replace = True
    elif new_has and VALUE_RANK.get(new_provider or "", _UNKNOWN_RANK) < VALUE_RANK.get(old_supplier or "", _UNKNOWN_RANK):
        replace = True  # higher-ranked provider
    else:
        replace = False  # lower-ranked provider only fills fields still None
    if replace:
        if new_provider is not None:
            state.value_provider[old.id] = new_provider
        return {name: getattr(new, name) for name in VALUE_FIELDS}
    return {name: getattr(old, name) if getattr(old, name) is not None else getattr(new, name) for name in VALUE_FIELDS}


def _unit_for(values: Mapping[str, Any], old: EconEvent, new: EconEvent) -> str | None:
    shown = [values["actual"], values["forecast"], values["previous"]]
    if all(s is None for s in shown):
        return old.unit or new.unit
    for text in shown:
        unit = parse_value(text)[1] if text is not None else None
        if unit is not None:
            return unit
    return None


def _merge_pair(old: EconEvent, new: EconEvent, state: MergeState) -> EconEvent:
    values = _merge_values(old, new, state)
    if old.all_day != new.all_day:
        take_new_time = old.all_day and not new.all_day  # never a timed value replaced by all-day
    else:
        take_new_time = _best_rank(new, SCHEDULE_RANK) < _best_rank(old, SCHEDULE_RANK)
    time_source = new if take_new_time else old
    title_source = new if _best_rank(new, SCHEDULE_RANK) < _best_rank(old, SCHEDULE_RANK) else old

    statuses = {old.status, new.status}
    if "cancelled" in statuses:
        status = "cancelled"
    elif "revised" in statuses:
        status = "revised"
    else:
        status = "scheduled"

    return old.model_copy(
        update={
            **values,
            "unit": _unit_for(values, old, new),
            "revised": old.revised if old.revised is not None else new.revised,
            "title": title_source.title,
            "scheduled_at": time_source.scheduled_at,
            "all_day": time_source.all_day,
            "importance": max(old.importance, new.importance),
            "category": old.category if old.category is not None else new.category,
            "description": old.description if old.description is not None else new.description,
            "currency": old.currency if old.currency is not None else new.currency,
            "status": status,
            "sources": _merge_sources(old.sources, new.sources),
        }
    )


def _content(event: EconEvent) -> dict[str, Any]:
    return event.model_dump(mode="json", exclude={"updated_at"})


def merge_econ_events(
    existing: list[EconEvent],
    incoming: list[EconEvent],
    *,
    now: Any,
    state: MergeState | None = None,
) -> list[EconEvent]:
    """Merge incoming provider events into the existing set.

    Returns only events that are new or whose stored content changed (``updated_at``
    excluded from the comparison), each stamped with ``updated_at = now``. Pass the same
    ``state`` on every run (see ``store_merge``) so fuzzy matches stay idempotent.
    """
    if state is None:
        state = MergeState()
    stamp = utc_of(now)
    original = {event.id: event for event in existing}
    working: dict[str, EconEvent] = dict(original)
    touched: dict[str, None] = {}
    for event in incoming:
        target = _resolve(event, working, state)
        if target is None:
            working[event.id] = event
            touched[event.id] = None
            provider = _incoming_provider(event)
            if _has_values(event) and provider is not None:
                state.value_provider[event.id] = provider
        else:
            if target != event.id:
                state.aliases[event.id] = target
            working[target] = _merge_pair(working[target], event, state)
            touched[target] = None

    changed: list[EconEvent] = []
    for key in touched:
        final = finalize_event(working[key], now=stamp)
        prior = original.get(key)
        if prior is None or _content(prior) != _content(final):
            changed.append(final.model_copy(update={"updated_at": stamp}))
    return changed
