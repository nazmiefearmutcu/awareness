"""Provider health bookkeeping (freshness, failures) persisted in the store."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from awareness.markets.models import ProviderHealth
from awareness.markets.store import MarketsStore


class HealthTracker:
    def __init__(self, store: MarketsStore, now: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self._now = now or (lambda: datetime.now(UTC))

    def _get(self, provider: str, expected_interval_s: int | None = None) -> ProviderHealth:
        health = self.store.get_health(provider) or ProviderHealth(provider=provider)
        if expected_interval_s is not None:
            health.expected_interval_s = int(expected_interval_s)
        return health

    def attempt(self, provider: str, expected_interval_s: int) -> None:
        health = self._get(provider, expected_interval_s)
        health.last_attempt_at = self._now()
        health.status = health.derive_status(self._now())
        self.store.put_health(health)

    def ok(self, provider: str, items: int, expected_interval_s: int | None = None) -> None:
        health = self._get(provider, expected_interval_s)
        now = self._now()
        health.last_ok_at = now
        health.last_attempt_at = health.last_attempt_at or now
        health.consecutive_failures = 0
        health.items_last_ok = int(items)
        health.status = health.derive_status(now)
        self.store.put_health(health)

    def error(self, provider: str, message: str, expected_interval_s: int | None = None) -> None:
        health = self._get(provider, expected_interval_s)
        now = self._now()
        health.last_error_at = now
        health.last_attempt_at = health.last_attempt_at or now
        health.last_error = message[:500]
        health.consecutive_failures += 1
        health.status = health.derive_status(now)
        self.store.put_health(health)

    def snapshot(self, providers: list[str] | None = None) -> list[ProviderHealth]:
        now = self._now()
        rows = self.store.all_health()
        if providers is not None:
            wanted = set(providers)
            rows = [r for r in rows if r.provider in wanted or any(r.provider.startswith(p + ".") for p in wanted)]
        out = []
        for row in rows:
            row.status = row.derive_status(now)
            out.append(row)
        return out
