"""Per-domain async rate limiter and concurrency cap.

Async semaphores per (registered) domain, with a minimum inter-fetch delay
derived from either robots.txt crawl-delay or a global default.

Spacing is reserved under a lock via ``next_allowed_at`` so concurrent holders
of the per-domain semaphore cannot race and fire back-to-back (which would
ignore crawl-delay when concurrency > 1).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass
class _DomainSlot:
    sem: asyncio.Semaphore
    # Monotonic time at which the *next* request for this domain may start.
    next_allowed_at: float


class PerDomainLimiter:
    """Provide per-domain concurrency and inter-request spacing."""

    # Soft bound on tracked domains (M-11): adversarial/unbounded fan-out
    # (e.g. per-URL host variants) must not leak memory forever.
    MAX_SLOTS = 4096

    def __init__(self, concurrency: int = 2, min_delay_sec: float = 1.0) -> None:
        self._concurrency = max(1, concurrency)
        self._min_delay = max(0.0, min_delay_sec)
        self._slots: dict[str, _DomainSlot] = {}
        self._lock = asyncio.Lock()

    def _slot(self, domain: str) -> _DomainSlot:
        slot = self._slots.get(domain)
        if slot is None:
            slot = _DomainSlot(sem=asyncio.Semaphore(self._concurrency), next_allowed_at=0.0)
            self._slots[domain] = slot
            self._maybe_evict_slots()
        return slot

    def _maybe_evict_slots(self) -> None:
        """Evict idle domain slots when over the cap (M-11).

        Only slots with no in-flight holder and no pending spacing are
        dropped, so eviction never breaks an active acquire/release pair;
        the next acquire for a dropped domain just creates a fresh slot.
        """
        if len(self._slots) <= PerDomainLimiter.MAX_SLOTS:
            return
        now = time.monotonic()
        idle = [d for d, s in self._slots.items() if not s.sem.locked() and s.next_allowed_at <= now]
        # Drop oldest first (dict is insertion-ordered).
        for d in idle:
            if len(self._slots) <= PerDomainLimiter.MAX_SLOTS:
                break
            self._slots.pop(d, None)
        # All slots busy — drop the oldest one anyway so the dict stays bounded;
        # its holder still owns the semaphore object and releases into the void.
        while len(self._slots) > PerDomainLimiter.MAX_SLOTS:
            oldest = next(iter(self._slots), None)
            if oldest is None:
                break
            self._slots.pop(oldest, None)

    def _effective_delay(self, override_delay: float | None) -> float:
        """Resolve inter-request delay for one acquire.

        Robots crawl-delay is a floor that may raise the configured minimum;
        it never lowers politeness below ``min_delay_sec``.
        """
        if override_delay is None:
            return self._min_delay
        try:
            crawl_delay = float(override_delay)
        except (TypeError, ValueError):
            return self._min_delay
        crawl_delay = max(crawl_delay, 0.0)
        return max(self._min_delay, crawl_delay)

    async def acquire(self, domain: str, override_delay: float | None = None) -> None:
        delay = self._effective_delay(override_delay)
        # Reserve a start time under the lock so concurrent acquirers cannot
        # both read a stale clock and fire back-to-back.
        async with self._lock:
            slot = self._slot(domain)
            now = time.monotonic()
            start_at = max(now, slot.next_allowed_at)
            slot.next_allowed_at = start_at + delay
            wait = start_at - now
        if wait > 0:
            await asyncio.sleep(wait)
        await slot.sem.acquire()

    def release(self, domain: str) -> None:
        slot = self._slots.get(domain)
        if not slot:
            return
        slot.sem.release()

    class _DomainCtx:
        def __init__(self, parent: PerDomainLimiter, domain: str, override_delay: float | None) -> None:
            self.parent = parent
            self.domain = domain
            self.override_delay = override_delay
            # H-26: release only when we actually acquired — cancellation while
            # waiting on the semaphore must not over-release and let the
            # per-domain concurrency cap decay.
            self._acquired = False

        async def __aenter__(self) -> None:
            await self.parent.acquire(self.domain, self.override_delay)
            self._acquired = True

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            if self._acquired:
                self.parent.release(self.domain)
                self._acquired = False

    def domain(self, domain: str, override_delay: float | None = None) -> PerDomainLimiter._DomainCtx:
        """Async context manager for an acquire/release pair."""
        return PerDomainLimiter._DomainCtx(self, domain, override_delay)
