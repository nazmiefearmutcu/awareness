"""Alert evaluation engine over the DuckDbIndex ``captures`` view.

:class:`AlertEngine` wraps an index and an :class:`AlertStore` and turns
active rules into :class:`~awareness.alerts.models.AlertFiring` rows:

* ``term_count`` — fires when the number of captures mentioning the term
  (word-boundary, case-insensitive, on ``title``/``text``) inside the rolling
  ``window_hours`` reaches the rule threshold.
* ``term_spike`` — fires when the window count clears the threshold *and* is
  at least 3x the mean of the previous 7 days of term volume. When the
  baseline is zero, the count must instead clear ``max(threshold, 3)``.

Every rule is subject to a per-rule cooldown: a rule that fired recently is
skipped until ``cooldown_minutes`` have passed.

All SQL is parameterized through :meth:`DuckDbIndex.execute` with bound
parameters (``$name``); terms are escaped with :func:`re.escape` and bound as
parameters, so they can never reach SQL text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from awareness.alerts.models import AlertFiring, AlertRule
from awareness.alerts.store import AlertStore
from awareness.obs.logging import get_logger
from awareness.storage.duckdb_index import DuckDbIndex

logger = get_logger("alerts.engine")

# Word-boundary, case-insensitive match on title OR text (RE2 inline flags).
_COUNT_SQL = """\
SELECT count(*) AS n
FROM captures
WHERE fetch_ts >= $start AND fetch_ts < $end
  AND (COALESCE(regexp_matches(title, $pat), false)
       OR COALESCE(regexp_matches(text, $pat), false))
"""

# Rolling baseline length for term_spike rules (calendar-free fixed 24h windows).
_BASELINE_DAYS = 7
# Spike multiplier over the baseline mean.
_SPIKE_FACTOR = 3.0
# Absolute floor when the baseline is empty.
_SPIKE_ABS_FLOOR = 3.0


@dataclass
class RuleCheckReport:
    """Single-rule test-mode evaluation result (:meth:`AlertEngine.check_rule_report`)."""

    fired: bool
    firing: AlertFiring | None
    count: int
    threshold: float
    suppressed_by_cooldown: bool


def _term_pattern(term: str) -> str:
    """Word-boundary, case-insensitive regex pattern for *term*.

    The pattern is bound as a query parameter (never interpolated into SQL)
    and ``re.escape`` protects regex metacharacters in the term.
    """
    return "(?i)\\b" + re.escape(term) + "\\b"


class AlertEngine:
    """Evaluate alert rules against the captured corpus."""

    def __init__(self, index: DuckDbIndex, store: AlertStore) -> None:
        self._index = index
        self._store = store

    def ensure_ready(self) -> None:
        """Raise :class:`RuntimeError` (``"index not ready"``) when the
        DuckDB index cannot answer queries.

        Mirrors the ``/healthz`` ``index_ready`` contract: probes
        ``health_snapshot()`` when present, else a duck-typed ``index_ready``
        attribute.
        """
        probe = getattr(self._index, "health_snapshot", None)
        if callable(probe):
            try:
                if not bool((probe() or {}).get("ready")):
                    raise RuntimeError("index not ready")
                return
            except RuntimeError:
                raise
            except Exception as exc:
                raise RuntimeError("index not ready") from exc
        if not bool(getattr(self._index, "index_ready", False)):
            raise RuntimeError("index not ready")

    # ── public evaluation surface ────────────────────────────────────────

    def evaluate_rules(self) -> list[AlertFiring]:
        """Evaluate every active rule; record + return each firing.

        Rules suppressed by cooldown, or that did not meet their condition,
        are skipped. Raises :class:`RuntimeError` when the index is not ready.
        """
        self.ensure_ready()
        now = datetime.now(UTC)
        firings: list[AlertFiring] = []
        for rule in self._store.list_rules(active_only=True):
            if self._in_cooldown(rule, now):
                continue
            firing = self._evaluate_rule(rule, now)
            if firing is not None:
                firings.append(firing)
        return firings

    def check_rule(
        self, rule_id: str, *, ignore_cooldown: bool = False
    ) -> AlertFiring | None:
        """Evaluate a single rule (cooldown respected), or ``None``.

        With ``ignore_cooldown=True`` (test mode) the per-rule cooldown gate
        is bypassed and the firing is NOT persisted, so the current condition
        is always surfaced without muting or polluting real alert history.
        Inactive or missing rules return ``None``; index-not-ready still
        raises :class:`RuntimeError`.
        """
        self.ensure_ready()
        rule = self._store.get_rule(rule_id)
        if rule is None or not rule.active:
            return None
        now = datetime.now(UTC)
        if self._in_cooldown(rule, now) and not ignore_cooldown:
            return None
        return self._evaluate_rule(rule, now, persist=not ignore_cooldown)

    def check_rule_report(self, rule_id: str) -> RuleCheckReport | None:
        """Test-mode single-rule check returning the full current-status report.

        The cooldown gate is always ignored and firings are never persisted:
        a test surfaces the rule's live condition (fired or not, count vs
        threshold, whether a normal run would have been suppressed) without
        muting or polluting real history. Inactive rules are still evaluated
        — a test is an explicit action. Returns ``None`` only for unknown
        rule ids; index-not-ready raises :class:`RuntimeError`.
        """
        self.ensure_ready()
        rule = self._store.get_rule(rule_id)
        if rule is None:
            return None
        now = datetime.now(UTC)
        firing = self._evaluate_rule(rule, now, persist=False)
        if firing is not None:
            count = firing.count
        else:
            window_start = now - timedelta(hours=rule.window_hours)
            count = self._count_docs(rule.term, window_start, now)
        return RuleCheckReport(
            fired=firing is not None,
            firing=firing,
            count=count,
            threshold=rule.threshold,
            suppressed_by_cooldown=self._in_cooldown(rule, now),
        )

    # ── per-rule evaluation ──────────────────────────────────────────────

    def _in_cooldown(self, rule: AlertRule, now: datetime) -> bool:
        """True when *rule* last fired within its own cooldown window."""
        last = self._store.last_firing_time(rule.id)
        if last is None:
            return False
        return (now - last).total_seconds() < rule.cooldown_minutes * 60.0

    def _evaluate_rule(
        self, rule: AlertRule, now: datetime, *, persist: bool = True
    ) -> AlertFiring | None:
        """Evaluate *rule* at *now*; return a firing when it fires.

        With ``persist=False`` (test mode) the firing is not recorded and
        carries a placeholder id of ``0``.
        """
        window_start = now - timedelta(hours=rule.window_hours)
        count = self._count_docs(rule.term, window_start, now)
        fired: bool
        detail: str
        if rule.kind == "term_count":
            fired = count >= rule.threshold
            detail = (
                f"{count} docs matched '{rule.term}' in the last "
                f"{rule.window_hours:g}h (threshold {rule.threshold:g})"
            )
        elif rule.kind == "term_spike":
            baseline = self._baseline_mean(rule.term, window_start)
            if baseline > 0:
                fired = count >= rule.threshold and count >= _SPIKE_FACTOR * baseline
                detail = (
                    f"{count} docs matched '{rule.term}' in the last "
                    f"{rule.window_hours:g}h (threshold {rule.threshold:g}, "
                    f"7-day baseline {baseline:g})"
                )
            else:
                required = max(rule.threshold, _SPIKE_ABS_FLOOR)
                fired = count >= required
                detail = (
                    f"{count} docs matched '{rule.term}' in the last "
                    f"{rule.window_hours:g}h (no baseline; requires "
                    f"count >= {required:g})"
                )
        else:  # pragma: no cover - kind is a Literal, but stay defensive
            logger.warning("alert_unknown_rule_kind", rule_id=rule.id, kind=rule.kind)
            return None

        if not fired:
            return None
        if persist:
            firing_id = self._store.record_firing(
                rule_id=rule.id,
                rule_name=rule.name,
                kind=rule.kind,
                term=rule.term,
                count=float(count),
                threshold=rule.threshold,
                detail=detail,
            )
        else:
            firing_id = 0  # test mode: not persisted, placeholder id
        return AlertFiring(
            id=firing_id,
            rule_id=rule.id,
            rule_name=rule.name,
            kind=rule.kind,
            term=rule.term,
            count=count,
            threshold=rule.threshold,
            fired_at=now,
            detail=detail,
        )

    # ── counting helpers ─────────────────────────────────────────────────

    def _count_docs(self, term: str, start: datetime, end: datetime) -> int:
        """Docs whose title/text mention *term* with ``fetch_ts`` in [start, end)."""
        rows = self._index.execute(
            _COUNT_SQL,
            {"start": start, "end": end, "pat": _term_pattern(term)},
        )
        return int(rows[0]["n"]) if rows else 0

    def _baseline_mean(self, term: str, window_start: datetime, days: int = _BASELINE_DAYS) -> float:
        """Mean per-day count of *term* over the *days* 24h windows before
        *window_start* (zero when the corpus has no older captures)."""
        counts: list[int] = []
        for i in range(days, 0, -1):
            start = window_start - timedelta(days=i)
            end = window_start - timedelta(days=i - 1)
            counts.append(self._count_docs(term, start, end))
        return float(sum(counts)) / days
