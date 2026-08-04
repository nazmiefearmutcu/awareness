"""Periodic alert evaluation loop.

:class:`AlertRunner` owns the background loop that turns active alert rules
into firings and webhook deliveries:

* every ``interval_seconds`` it builds a fresh
  :class:`~awareness.alerts.engine.AlertEngine` through the engine factory and
  evaluates all active rules (the engine records firings + respects per-rule
  cooldowns — the runner never re-implements cooldown);
* firings whose rule carries a ``webhook_url`` are POSTed through
  :func:`~awareness.alerts.notify.deliver_webhook`;
* all firings of the tick are handed to the optional ``on_firing`` callback
  (logging / UI hook) and returned by :meth:`AlertRunner.evaluate_once` for
  tests and one-shot CLI use.

A tick that raises (broken rule, transient index failure) is logged and the
loop continues — one bad evaluation must not kill the process.

:func:`create_default_runner` wires the runner to the standard store
(``<data_dir>/alerts.db``) used by the API and the ``alerts`` CLI.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from awareness.alerts.engine import AlertEngine
from awareness.alerts.models import AlertFiring
from awareness.alerts.notify import deliver_webhook
from awareness.alerts.store import AlertStore
from awareness.config import get_settings
from awareness.obs.logging import get_logger
from awareness.storage.duckdb_index import DuckDbIndex

logger = get_logger("alerts.runner")

# Minimum loop interval (seconds): guards against tight-loop accidents from
# misconfigured intervals. Sub-minute cadence adds nothing for keyword alerts
# over a rolling capture window.
MIN_INTERVAL_SECONDS = 30.0


class AlertRunner:
    """Periodically evaluate alert rules and deliver webhook firings."""

    def __init__(
        self,
        engine_factory: Callable[[], AlertEngine],
        interval_seconds: float = 300.0,
        on_firing: Callable[[list[AlertFiring]], Awaitable[None]] | None = None,
    ) -> None:
        self._engine_factory = engine_factory
        # Interval floor: clamp, never crash, on pathological configs.
        self._interval = max(MIN_INTERVAL_SECONDS, float(interval_seconds))
        self._on_firing = on_firing
        self._task: asyncio.Task[None] | None = None

    # ── lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """Begin the periodic loop; the first evaluation runs immediately.

        Idempotent: calling again while already running is a no-op, so the
        API lifespan and a manual start can race safely.
        """
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="alerts-runner")
        # Yield once so the fresh task is scheduled before start() returns.
        await asyncio.sleep(0)

    async def stop(self) -> None:
        """Cancel the loop task and await its completion (idempotent)."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @property
    def running(self) -> bool:
        """True while the periodic loop task is alive."""
        return self._task is not None and not self._task.done()

    @property
    def interval_seconds(self) -> float:
        """Effective loop interval (clamped to :data:`MIN_INTERVAL_SECONDS`)."""
        return self._interval

    # ── evaluation ──────────────────────────────────────────────────────

    async def evaluate_once(self) -> list[AlertFiring]:
        """Run one full evaluation pass; return the resulting firings.

        Synchronous engine work (DuckDB/SQLite) runs in the default executor
        so the event loop stays responsive. Firings whose rule has a
        ``webhook_url`` are delivered here; the returned list is the complete
        pass output (for CLI / tests / UI).
        """
        engine = self._engine_factory()
        firings = await asyncio.to_thread(engine.evaluate_rules)
        store = getattr(engine, "_store", None)
        for firing in firings:
            if store is None:
                continue
            rule = store.get_rule(firing.rule_id)
            if rule is None:
                continue
            # Deliver to EVERY configured webhook (the legacy single
            # webhook_url is only a fallback when the list is empty).
            webhooks = list(rule.webhooks)
            if not webhooks and rule.webhook_url:
                webhooks = [rule.webhook_url]
            for url in webhooks:
                await deliver_webhook(url, firing)
        return firings

    async def _loop(self) -> None:
        """Evaluate immediately, then every interval; deliver + report.

        Per-tick exceptions (broken rule, transient index failure) are logged
        and swallowed so the loop survives — the next tick retries. Cooldown
        suppression is respected inside the engine and never duplicated here.
        """
        while True:
            started = asyncio.get_running_loop().time()
            try:
                firings = await self.evaluate_once()
                if self._on_firing is not None:
                    await self._on_firing(firings)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("alert_runner_tick_failed", error=str(exc))
            # Fixed cadence: the next tick fires interval_seconds after this
            # one started (evaluation time does not pile up).
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(0.0, self._interval - elapsed))


def create_default_runner(index_getter: Callable[[], DuckDbIndex]) -> AlertRunner:
    """Build an :class:`AlertRunner` over the default store.

    The store lives at ``<data_dir>/alerts.db`` — the same layout the API and
    the ``alerts`` CLI use. A fresh :class:`AlertEngine` (and with it a fresh
    :class:`AlertStore` connection) is created per tick through the factory,
    matching the per-request store pattern of the alerts router, so rule and
    cooldown changes are always picked up without extra lifecycle state.
    """

    def _engine() -> AlertEngine:
        settings = get_settings()
        assert settings.data_dir is not None
        store = AlertStore(settings.data_dir / "alerts.db")
        return AlertEngine(index_getter(), store)

    return AlertRunner(engine_factory=_engine)
