"""MarketsRunner — runs every collector on its cadence inside the API process.

* One asyncio task per collector; collectors sharing a ``lock_group`` never run
  concurrently (e.g. all economic-calendar collectors merge into the same rows).
* Restart-friendly: the first run waits until ``last_ok_at + interval`` from the
  stored health row, so restarting the service does not hammer providers.
* Failures are recorded in provider health and retried with exponential backoff
  (capped at the collector interval and 30 minutes); one failing collector never
  affects the others.
* A daily maintenance job prunes news older than ``news_retention_days``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from awareness.markets.collectors.base import Collector, CollectorContext, CollectorResult
from awareness.markets.health import HealthTracker

log = logging.getLogger(__name__)

MAX_BACKOFF_S = 1800


@dataclass
class CollectorState:
    name: str
    interval_s: int
    lock_group: str | None
    running: bool = False
    next_run_at: datetime | None = None
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    last_items: int | None = None
    last_error: str | None = None
    last_notes: list[str] = field(default_factory=list)
    failures: int = 0
    runs: int = 0

    def to_dict(self) -> dict[str, Any]:
        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        return {
            "name": self.name,
            "interval_s": self.interval_s,
            "lock_group": self.lock_group,
            "running": self.running,
            "next_run_at": iso(self.next_run_at),
            "last_started_at": iso(self.last_started_at),
            "last_finished_at": iso(self.last_finished_at),
            "last_items": self.last_items,
            "last_error": self.last_error,
            "last_notes": self.last_notes,
            "failures": self.failures,
            "runs": self.runs,
        }


class MarketsRunner:
    def __init__(
        self,
        ctx: CollectorContext,
        collectors: Sequence[Collector],
        *,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        timeout_s: Callable[[Collector], float] | None = None,
        maintenance: bool = True,
    ) -> None:
        names = [c.name for c in collectors]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate collector names: {names}")
        self.ctx = ctx
        self.collectors = {c.name: c for c in collectors}
        self.health = HealthTracker(ctx.store, now=now)
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._timeout_s = timeout_s or (
            lambda c: float(getattr(c, "timeout_s", None) or max(90, min(1200, c.interval_s)))
        )
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._wakeups: dict[str, asyncio.Event] = {}
        self._maintenance = maintenance
        self.state = {
            c.name: CollectorState(name=c.name, interval_s=c.interval_s, lock_group=c.lock_group)
            for c in collectors
        }
        self.started_at: datetime | None = None

    # ── lifecycle ─────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return any(not t.done() for t in self._tasks)

    async def start(self) -> None:
        if self.running:
            return
        self.started_at = self._now()
        for index, name in enumerate(sorted(self.collectors)):
            self._wakeups[name] = asyncio.Event()
            self._tasks.append(asyncio.create_task(self._loop(name, stagger=index * 1.5), name=f"markets:{name}"))
        if self._maintenance:
            self._tasks.append(asyncio.create_task(self._maintenance_loop(), name="markets:maintenance"))
        log.info("markets runner started with %d collectors", len(self.collectors))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        log.info("markets runner stopped")

    def trigger(self, name: str) -> bool:
        """Ask a collector to run now (returns False for an unknown name)."""
        event = self._wakeups.get(name)
        if name not in self.collectors:
            return False
        self.state[name].next_run_at = self._now()
        if event is not None:
            event.set()
        return True

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "collectors": [self.state[name].to_dict() for name in sorted(self.state)],
        }

    # ── execution ─────────────────────────────────────────────────────────

    def _initial_due(self, collector: Collector) -> datetime:
        now = self._now()
        health = self.ctx.store.get_health(collector.name)
        if health is not None and health.last_ok_at is not None and health.consecutive_failures == 0:
            due = health.last_ok_at + timedelta(seconds=collector.interval_s)
            return max(now, due)
        return now

    async def run_once(self, name: str) -> CollectorResult:
        """Run a collector immediately (honours its lock group); records health."""
        collector = self.collectors[name]
        state = self.state[name]
        lock = self._locks.setdefault(collector.lock_group, asyncio.Lock()) if collector.lock_group else None
        if lock is not None:
            await lock.acquire()
        state.running = True
        state.last_started_at = self._now()
        self.health.attempt(name, collector.interval_s)
        try:
            result = await asyncio.wait_for(collector.run(self.ctx), timeout=self._timeout_s(collector))
        except Exception as exc:
            state.failures += 1
            state.last_error = f"{type(exc).__name__}: {exc}"[:500]
            self.health.error(name, state.last_error, collector.interval_s)
            log.warning("collector %s failed: %s", name, state.last_error)
            raise
        else:
            state.failures = 0
            state.last_error = None
            state.last_items = result.items
            state.last_notes = list(result.notes)[:10]
            self.health.ok(name, result.items, collector.interval_s)
            return result
        finally:
            state.running = False
            state.runs += 1
            state.last_finished_at = self._now()
            if lock is not None:
                lock.release()

    def _next_delay(self, collector: Collector, failed: bool) -> float:
        if not failed:
            jitter = random.uniform(0, min(30.0, collector.interval_s * 0.05))  # noqa: S311
            return collector.interval_s + jitter
        failures = self.state[collector.name].failures
        backoff = min(float(MAX_BACKOFF_S), float(collector.interval_s), 30.0 * (2 ** min(failures, 8)))
        return max(15.0, backoff)

    async def _wait_until(self, name: str, when: datetime) -> None:
        event = self._wakeups[name]
        while True:
            remaining = (when - self._now()).total_seconds()
            if remaining <= 0 or event.is_set():
                event.clear()
                return
            try:
                await asyncio.wait_for(event.wait(), timeout=min(remaining, 60.0))
            except TimeoutError:
                continue

    async def _loop(self, name: str, *, stagger: float) -> None:
        collector = self.collectors[name]
        state = self.state[name]
        state.next_run_at = self._initial_due(collector)
        if stagger:
            await self._sleep(stagger)
        while True:
            assert state.next_run_at is not None
            await self._wait_until(name, state.next_run_at)
            failed = False
            try:
                await self.run_once(name)
            except asyncio.CancelledError:
                raise
            except Exception:
                failed = True
            state.next_run_at = self._now() + timedelta(seconds=self._next_delay(collector, failed))

    async def _maintenance_loop(self) -> None:
        await self._sleep(120)
        while True:
            try:
                cutoff = self._now() - timedelta(days=self.ctx.settings.news_retention_days)
                removed = await asyncio.to_thread(self.ctx.store.prune_news, cutoff)
                if removed:
                    log.info("pruned %d news items older than %s", removed, cutoff.date())
                now = self._now()
                social = await asyncio.to_thread(
                    self.ctx.store.prune_indicators, "social.", now - timedelta(days=7), keep_daily=True
                )
                tone = await asyncio.to_thread(self.ctx.store.prune_indicators, "gdelt.tone:", now - timedelta(days=90))
                if social or tone:
                    log.info("pruned indicators: social=%d gdelt=%d", social, tone)
            except Exception as exc:
                log.warning("maintenance failed: %s", exc)
            await self._sleep(24 * 3600)


def default_collectors() -> list[Collector]:
    """Every production collector. Imports are local so a broken module is reported, not fatal."""
    factories: list[tuple[str, str]] = [
        ("awareness.markets.ref.sec_tickers", "SecTickersCollector"),
        ("awareness.markets.news.feeds", "FeedCollector"),
        ("awareness.markets.news.feeds", "GoogleNewsCollector"),
        ("awareness.markets.calendar.economic", "NasdaqEconCollector"),
        ("awareness.markets.calendar.economic", "ForexFactoryCollector"),
        ("awareness.markets.calendar.economic", "BeaCollector"),
        ("awareness.markets.calendar.central_banks", "CentralBankCollector"),
        ("awareness.markets.calendar.corporate", "NasdaqCorporateCollector"),
        ("awareness.markets.calendar.filings", "SecFilingsCollector"),
        ("awareness.markets.calendar.holidays", "NagerHolidayCollector"),
        ("awareness.markets.calendar.holidays", "ExchangeHolidayCollector"),
        ("awareness.markets.sentiment.indicators", "CnnFearGreedCollector"),
        ("awareness.markets.sentiment.indicators", "CryptoFearGreedCollector"),
        ("awareness.markets.sentiment.indicators", "CboeVixCollector"),
        ("awareness.markets.sentiment.indicators", "ApeWisdomCollector"),
        ("awareness.markets.sentiment.indicators", "GdeltToneCollector"),
    ]
    import importlib

    out: list[Collector] = []
    for module_name, class_name in factories:
        try:
            module = importlib.import_module(module_name)
            out.append(getattr(module, class_name)())
        except Exception as exc:
            log.error("collector %s.%s unavailable: %s", module_name, class_name, exc)
    return out
