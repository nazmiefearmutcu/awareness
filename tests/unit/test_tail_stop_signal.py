"""C-01/H-04 tail stop-signal tests.

Covers:
- run_tail breaks when the job becomes terminal (COMPLETED via planner.stop_tail)
  without needing an in-process request_stop.
- The reseed loop stops re-arming once the job is terminal and raises the
  ``_stopped_event`` that stops the worker loop too.
- TailEngine.stop() cancels + awaits a hung worker task on drain timeout (H-04)
  instead of abandoning it.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

from awareness.config import reset_settings
from awareness.planner.planner import Planner
from awareness.schemas.doc import SourceKind
from awareness.schemas.jobs import JobKind, JobState, JobStatus, TaskState
from awareness.sources import get_adapter_registry
from awareness.storage.state import StateDB
from awareness.tail.engine import TailEngine
from awareness.workers.engine import WorkerEngine


def _state(tmp_project) -> StateDB:
    state = StateDB(f"sqlite:///{tmp_project / 'state.db'}")
    state.init()
    return state


async def test_run_tail_breaks_when_job_completed(tmp_project) -> None:
    state = _state(tmp_project)
    planner = Planner(state)
    job_id = "tail-stop-1"
    state.create_job(
        JobState(job_id=job_id, kind=JobKind.TAIL, status=JobStatus.RUNNING, request={})
    )
    engine = WorkerEngine(state, planner, concurrency=1, silent_progress=True)

    async def mark_completed() -> None:
        await asyncio.sleep(0.15)
        state.set_job_status(job_id, JobStatus.COMPLETED, note="stop")

    # C-01: without the fix, run_tail never exits and this times out.
    await asyncio.wait_for(
        asyncio.gather(engine.run_tail(job_id, poll_seconds=0.05), mark_completed()),
        timeout=5.0,
    )
    assert not engine.is_stopping()  # exited via status observation, not stop()
    await engine.aclose()


def _noop_rss_adapter() -> MagicMock:
    adapter = MagicMock()
    adapter.source_type = SourceKind.RSS

    async def _noop_run(partition, context):
        if False:  # pragma: no cover - make it an async generator
            yield

    adapter.run_partition = _noop_run
    return adapter


async def test_tail_engine_reseed_stops_on_terminal_job(tmp_project, monkeypatch) -> None:
    monkeypatch.setenv("AW_TAIL_POLL_SECONDS", "0.1")
    reset_settings()
    state = _state(tmp_project)
    planner = Planner(state)
    job_id = "tail-stop-2"
    state.create_job(JobState(job_id=job_id, kind=JobKind.TAIL, request={}))

    reg = get_adapter_registry()
    orig_rss = reg.get(SourceKind.RSS)
    reg.register(_noop_rss_adapter())
    try:
        seeds = tmp_project / "seeds.yaml"
        seeds.write_text("feeds:\n  - url: https://example.com/feed\n", encoding="utf-8")
        tail = TailEngine(state, planner)
        await tail.start(job_id=job_id, seeds_path=seeds, gdelt=False)
        try:
            await asyncio.sleep(0.35)
            assert tail._last_reseed_count >= 1, "reseed should have re-armed seeds"

            planner.stop_tail(job_id)  # job COMPLETED + tail_state.running=False
            await _wait_loops_done(tail, wait_seconds=5.0)

            assert tail._stopped_event.is_set()
            assert tail.info()["in_process_running"] is False
            assert state.get_job(job_id).status == JobStatus.COMPLETED
        finally:
            await tail.stop(drain_seconds=1.0)
    finally:
        if orig_rss is not None:
            reg.register(orig_rss)


async def test_tail_engine_stop_cancels_hung_worker(tmp_project, monkeypatch) -> None:
    # H-04: stop() with a drain timeout must cancel + await the worker task
    # (its finally-block / aclose runs) instead of abandoning it.
    monkeypatch.setenv("AW_TAIL_POLL_SECONDS", "0.1")
    reset_settings()
    state = _state(tmp_project)
    planner = Planner(state)
    job_id = "tail-hang-1"
    state.create_job(JobState(job_id=job_id, kind=JobKind.TAIL, request={}))

    tail = TailEngine(state, planner)
    await tail.start(job_id=job_id, gdelt=False)

    async def hang(task) -> None:
        await asyncio.sleep(60.0)

    tail._engine._run_task = hang  # type: ignore[method-assign]
    aclosed: list[bool] = []
    orig_aclose = tail._engine.aclose

    async def _spy_aclose() -> None:
        aclosed.append(True)
        await orig_aclose()

    tail._engine.aclose = _spy_aclose  # type: ignore[method-assign]
    state.add_tasks(
        [
            TaskState(
                task_id="t-hang",
                job_id=job_id,
                source_type=SourceKind.LOCAL_FIXTURE,
                partition_key="pk-hang",
                payload={},
            )
        ]
    )
    # Let the worker claim the task and block inside the hung run_partition.
    await asyncio.sleep(0.4)
    assert tail._task is not None and not tail._task.done()  # worker is genuinely stuck

    await tail.stop(drain_seconds=0.2)
    assert tail._task is None
    assert aclosed, "hung worker task must be cancelled and awaited (H-04)"


async def _wait_loops_done(tail: TailEngine, *, wait_seconds: float) -> None:
    deadline = time.monotonic() + wait_seconds
    while not (
        tail._reseed_task is not None
        and tail._reseed_task.done()
        and tail._task is not None
        and tail._task.done()
    ):
        if time.monotonic() > deadline:
            raise AssertionError("tail reseed/worker loops did not terminate")
        await asyncio.sleep(0.05)
