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
# A value group is a display string plus its parsed number; each group has its own supplier.
VALUE_GROUPS: tuple[str, ...] = ("actual", "forecast", "previous")
CENTRAL_PROVIDERS: frozenset[str] = frozenset({"fed", "ecb", "boe", "boj", "tcmb"})
_UNKNOWN_RANK = 9


@dataclass
class MergeState:
    """Side state that must survive between runs.

    ``aliases`` maps an incoming event id to the stored event it was fuzzy-merged into.
    ``value_provider`` maps a stored event id to the provider that supplied its values.
    """

    aliases: dict[str, str] = field(default_factory=dict)
    # event id -> {value group -> provider that supplied it}; groups: actual, forecast, previous
    value_provider: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def from_json(cls, raw: Any) -> MergeState:
        if not isinstance(raw, dict):
            return cls()
        value_provider: dict[str, dict[str, str]] = {}
        for event_key, supplied in (raw.get("value_provider") or {}).items():
            if isinstance(supplied, str):
                # legacy shape (one supplier per event): applies to every value group
                value_provider[event_key] = {group: supplied for group in VALUE_GROUPS}
            elif isinstance(supplied, dict):
                value_provider[event_key] = {str(g): str(p) for g, p in supplied.items()}
        return cls(aliases=dict(raw.get("aliases") or {}), value_provider=value_provider)

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


def _supplier(event: EconEvent, state: MergeState, group: str) -> str | None:
    """Provider that supplied this value group; falls back to the best-ranked source if unknown."""
    recorded = state.value_provider.get(event.id, {}).get(group)
    return recorded or _best_provider(event, VALUE_RANK)


def _record_supplier(state: MergeState, event_key: str, event: EconEvent, provider: str | None) -> None:
    """Record ``provider`` as the supplier of every value group ``event`` actually carries."""
    if provider is None:
        return
    for group in VALUE_GROUPS:
        if getattr(event, group) is not None or getattr(event, f"{group}_value") is not None:
            state.value_provider.setdefault(event_key, {})[group] = provider


def _is_official(event: EconEvent) -> bool:
    return event.category == "central_bank" and bool(_providers(event) & CENTRAL_PROVIDERS)


def _is_unmerged_rate_row(event: EconEvent) -> bool:
    """A ForexFactory policy-rate row that has not been merged with a central-bank source yet."""
    return (
        event.category == "central_bank"
        and _providers(event) == {"forexfactory"}
        and is_rate_decision_title(event.title)
    )


def _official_side(old: EconEvent, new: EconEvent) -> EconEvent | None:
    """The central-bank side of a rate-decision pair, or None.

    Only policy-rate rows qualify (the ruling): a Fed all-day FOMC Minutes row is not a
    rate decision, so a timed ForexFactory minutes row keeps its time. An already-merged
    central-bank row still counts as official.
    """
    if _is_official(old) and _is_unmerged_rate_row(new):
        return old
    if _is_official(new) and _is_unmerged_rate_row(old):
        return new
    return None


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
    """Per value group (actual / forecast / previous), decide which side's value is kept.

    * A snapshot that carries no value for a group never clears the stored one.
    * An empty stored group takes the incoming value.
    * The same provider that supplied the group replaces it (a newer revision).
    * A higher-ranked provider replaces a lower-ranked supplier's value.
    * Otherwise the stored value stays (a lower-ranked provider only fills empty groups).
    """
    provider = _incoming_provider(new)
    out: dict[str, Any] = {}
    for group in VALUE_GROUPS:
        value_name = f"{group}_value"
        old_pair = (getattr(old, group), getattr(old, value_name))
        new_pair = (getattr(new, group), getattr(new, value_name))
        if new_pair == (None, None):
            take = False
        elif old_pair == (None, None):
            take = True
        else:
            supplier = _supplier(old, state, group)
            outranks = VALUE_RANK.get(provider or "", _UNKNOWN_RANK) < VALUE_RANK.get(supplier or "", _UNKNOWN_RANK)
            take = provider is not None and (provider == supplier or outranks)
        if take:
            out[group], out[value_name] = new_pair
            if provider is not None:
                state.value_provider.setdefault(old.id, {})[group] = provider
        else:
            out[group], out[value_name] = old_pair
    return out


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
    official = _official_side(old, new)
    if official is not None:
        time_source = official  # central bank sets the date, time and all-day flag of its decision
    else:
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
            _record_supplier(state, event.id, event, _incoming_provider(event))
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
