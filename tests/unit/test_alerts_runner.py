"""Unit tests for the periodic alert runner (alerts/runner.py).

Exercises the loop with stub engines so no DuckDB/SQLite state is needed.
The loop interval is clamped to ``MIN_INTERVAL_SECONDS``; multi-tick tests
monkeypatch that floor down so they run fast.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from awareness.alerts import runner as runner_module
from awareness.alerts.engine import AlertEngine
from awareness.alerts.models import AlertFiring
from awareness.alerts.runner import MIN_INTERVAL_SECONDS, AlertRunner, create_default_runner
from awareness.alerts.store import AlertStore


def _firing(rule_id: str = "r1", *, name: str = "bitcoin watch", count: int = 3) -> AlertFiring:
    return AlertFiring(
        id=1,
        rule_id=rule_id,
        rule_name=name,
        kind="term_count",
        term="bitcoin",
        count=count,
        threshold=3.0,
        fired_at=datetime.now(UTC),
        detail="3 docs matched 'bitcoin' in the last 24h",
    )


class _StubEngine:
    """evaluate_rules returns canned firings; optional initial failure script."""

    def __init__(self, firings: list[AlertFiring], *, fail_first: int = 0) -> None:
        self.firings = firings
        self.fail_first = fail_first
        self.calls = 0

    def evaluate_rules(self) -> list[AlertFiring]:
        self.calls += 1
        if self.calls <= self.fail_first:
            raise RuntimeError("index not ready")
        return self.firings


async def _wait_until(predicate: object, seconds: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if predicate():  # type: ignore[operator]
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached in time")


# ── lifecycle ──────────────────────────────────────────────────────────────


async def test_runner_evaluates_immediately_on_start() -> None:
    engine = _StubEngine([_firing()])
    seen: list[list[AlertFiring]] = []

    async def on_firing(firings: list[AlertFiring]) -> None:
        seen.append(firings)

    runner = AlertRunner(lambda: engine, interval_seconds=300.0, on_firing=on_firing)
    await runner.start()
    try:
        await _wait_until(lambda: bool(seen))
        assert seen == [[engine.firings[0]]]
        assert engine.calls == 1  # first tick runs before the first sleep
        assert runner.running
    finally:
        await runner.stop()


async def test_start_is_idempotent_no_double_loop() -> None:
    engine = _StubEngine([])
    runner = AlertRunner(lambda: engine, interval_seconds=300.0)
    await runner.start()
    await runner.start()  # no-op: must not spawn a second loop
    try:
        await _wait_until(lambda: engine.calls >= 1)
        # A second loop would have ticked immediately too → 2 calls.
        assert engine.calls == 1
    finally:
        await runner.stop()


async def test_stop_cancels_loop_and_stops_evaluations() -> None:
    engine = _StubEngine([_firing()])
    runner = AlertRunner(lambda: engine, interval_seconds=300.0)
    await runner.start()
    await _wait_until(lambda: engine.calls >= 1)
    await runner.stop()
    assert not runner.running
    calls_after_stop = engine.calls
    await asyncio.sleep(0.05)
    assert engine.calls == calls_after_stop


async def test_stop_without_start_is_a_noop() -> None:
    runner = AlertRunner(lambda: _StubEngine([]))
    await runner.stop()
    assert not runner.running


# ── resilience ─────────────────────────────────────────────────────────────


async def test_exception_in_evaluate_does_not_kill_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner_module, "MIN_INTERVAL_SECONDS", 0.02)
    engine = _StubEngine([], fail_first=1)
    ticks: list[list[AlertFiring]] = []

    async def on_firing(firings: list[AlertFiring]) -> None:
        ticks.append(firings)

    runner = AlertRunner(lambda: engine, interval_seconds=0.05, on_firing=on_firing)
    await runner.start()
    try:
        await _wait_until(lambda: engine.calls >= 2)
        assert engine.calls >= 2  # the broken first tick did not stop the loop
        assert runner.running
        assert ticks[0] == []  # second tick reported an (empty) pass
    finally:
        await runner.stop()


# ── interval floor ─────────────────────────────────────────────────────────


def test_interval_floor_clamps_to_minimum() -> None:
    engine = _StubEngine([])
    assert AlertRunner(lambda: engine, interval_seconds=5.0).interval_seconds == MIN_INTERVAL_SECONDS
    assert (
        AlertRunner(lambda: engine, interval_seconds=300.0).interval_seconds == 300.0
    )


# ── default wiring ─────────────────────────────────────────────────────────


class _DummyIndex:
    """DuckDbIndex stand-in (the engine never queries it in this test)."""


def test_create_default_runner_wires_default_store(tmp_project: Path) -> None:
    runner = create_default_runner(_DummyIndex)  # type: ignore[arg-type]
    engine = runner._engine_factory()
    assert isinstance(engine, AlertEngine)
    assert isinstance(engine._store, AlertStore)
    assert engine._store._path == tmp_project / "data" / "alerts.db"
