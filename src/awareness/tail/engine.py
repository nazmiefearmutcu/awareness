"""Tail engine: live capture of public text.

Lifecycle:
1. ``start(seeds)`` creates a tail job, persists state ``running=True``,
   and seeds initial discovery partitions.
2. A background asyncio task polls the seed feeds/sitemaps every
   ``tail_poll_seconds`` and re-emits discovery partitions when due.
3. The worker engine processes all enqueued partitions (discovery yields
   sub-partitions for newly-seen URLs; tail_recrawl fetches text).
4. ``stop()`` flips ``running=False`` and drains in-flight work.

Resume: on restart, ``start()`` is called again with the same seeds and the
feeds adapter restores its ``seen_urls`` cursor from each task's checkpoint.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import yaml

from awareness.config import get_settings
from awareness.obs.logging import get_logger
from awareness.planner.planner import Planner
from awareness.schemas.doc import SourceKind
from awareness.schemas.jobs import JobStatus, TaskState
from awareness.storage.state import StateDB
from awareness.util.timeutil import utcnow
from awareness.workers.engine import WorkerEngine

logger = get_logger("tail.engine")


def _load_seeds(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"feeds": [], "sitemaps": [], "atom": []}
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        return {"feeds": [], "sitemaps": [], "atom": []}
    return data


class TailEngine:
    """Async coordinator that runs the worker pool against a tail job."""

    def __init__(self, state: StateDB, planner: Planner) -> None:
        self._state = state
        self._planner = planner
        self._engine: WorkerEngine | None = None
        self._stop_event = asyncio.Event()
        # Set when the reseed loop observes a terminal job status (C-01): the
        # worker loop watches it and requests an engine stop too.
        self._stopped_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._reseed_task: asyncio.Task[None] | None = None
        self._job_id: str | None = None
        # Visibility hooks for the UI: next poll time, last poll time/count.
        self._next_reseed_at: float | None = None
        self._last_reseed_at: str | None = None
        self._last_reseed_count: int = 0

    def info(self) -> dict[str, object]:
        """Snapshot of in-process reseed bookkeeping for the API."""
        return {
            "next_reseed_at": self._next_reseed_at,
            "last_reseed_at": self._last_reseed_at,
            "last_reseed_count": self._last_reseed_count,
            "in_process_running": self._task is not None and not self._task.done(),
        }

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _gdelt_task(self, job_id: str, slot: str, max_urls: int) -> TaskState:
        return TaskState(
            task_id=f"t-{uuid.uuid4().hex[:16]}",
            job_id=job_id,
            source_type=SourceKind.GDELT,
            partition_key=f"gdelt:gkg:{slot}",  # matches the backfill planner key
            payload={"slot": slot, "max_urls": max_urls},
        )

    async def start(
        self,
        seeds_path: Path | None = None,
        *,
        match_config: dict[str, Any] | None = None,
        gdelt: bool = False,
        gdelt_max_urls: int = 500,
        mute_duplicates: bool | None = None,
        job_id: str | None = None,
    ) -> str:
        settings = get_settings()
        seeds = _load_seeds(seeds_path or settings.tail_seed_file)
        # Merge the ingest-time topic filter into the job request so the worker
        # can read it back from job.request (same shape as a BackfillRequest).
        if match_config:
            seeds = {**seeds, **match_config}
        self._gdelt = bool(gdelt)
        self._gdelt_max_urls = int(gdelt_max_urls)
        self._last_gdelt_slot: str | None = None

        if job_id is None:
            job_id = self._planner.submit_tail(seeds)
        else:
            self._state.set_job_status(job_id, JobStatus.RUNNING)
            # set_tail defaults pid=os.getpid() so get_tail can detect orphans.
            self._state.set_tail(running=True, job_id=job_id, note="tail-active")
        self._job_id = job_id
        self._engine = WorkerEngine(self._state, self._planner, mute_duplicates=mute_duplicates)
        self._stop_event.clear()
        self._stopped_event.clear()

        # Seed an initial GDELT slot immediately (don't wait a full poll).
        if self._gdelt:
            from awareness.sources.gdelt import latest_gkg_slot

            slot = latest_gkg_slot()
            self._state.add_tasks([self._gdelt_task(job_id, slot, self._gdelt_max_urls)])
            self._last_gdelt_slot = slot
            logger.info("tail_gdelt_seeded", job_id=job_id, slot=slot, max_urls=self._gdelt_max_urls)

        async def reseed_loop() -> None:
            """Re-arm seed discovery tasks every tail_poll_seconds.

            We are pedantic about exception handling: ANY error in the loop
            body is logged, swallowed, and the loop keeps running. Earlier
            this loop would crash silently on the first reseed (UNIQUE
            constraint on (job_id, partition_key)) and the user would
            wonder why nothing new ever happened.
            """
            iteration = 0
            try:
                while not self._stop_event.is_set():
                    self._next_reseed_at = utcnow().timestamp() + settings.tail_poll_seconds
                    await asyncio.sleep(settings.tail_poll_seconds)
                    if self._stop_event.is_set():
                        break
                    # C-01: planner.stop_tail marks the job COMPLETED (and flips
                    # tail_state.running=False). A stopped tail must stop re-arming
                    # discovery tasks — otherwise reseed keeps feeding the pool.
                    job = self._state.get_job(job_id)
                    if job is not None and job.status in (
                        JobStatus.CANCELLED,
                        JobStatus.COMPLETED,
                        JobStatus.FAILED,
                    ):
                        logger.info("tail_reseed_job_terminal", job_id=job_id, status=job.status.value)
                        self._stopped_event.set()
                        break
                    iteration += 1
                    try:
                        tasks: list[TaskState] = []
                        for entry in seeds.get("feeds", []):
                            if not entry.get("url"):
                                continue
                            tasks.append(
                                TaskState(
                                    task_id=f"t-{uuid.uuid4().hex[:16]}",
                                    job_id=job_id,
                                    source_type=SourceKind.RSS,
                                    partition_key=f"rss:{entry['url']}",
                                    payload={"kind": "rss", "url": entry["url"]},
                                )
                            )
                        for entry in seeds.get("atom", []):
                            if not entry.get("url"):
                                continue
                            tasks.append(
                                TaskState(
                                    task_id=f"t-{uuid.uuid4().hex[:16]}",
                                    job_id=job_id,
                                    source_type=SourceKind.RSS,
                                    partition_key=f"atom:{entry['url']}",
                                    payload={"kind": "atom", "url": entry["url"]},
                                )
                            )
                        for entry in seeds.get("sitemaps", []):
                            if not entry.get("url"):
                                continue
                            tasks.append(
                                TaskState(
                                    task_id=f"t-{uuid.uuid4().hex[:16]}",
                                    job_id=job_id,
                                    source_type=SourceKind.RSS,
                                    partition_key=f"sitemap:{entry['url']}",
                                    payload={"kind": "sitemap", "url": entry["url"]},
                                )
                            )
                        # GDELT firehose: follow the latest 15-min slot. Only
                        # emit when the slot advances, so we don't re-process
                        # the same global-news batch every poll.
                        if getattr(self, "_gdelt", False):
                            from awareness.sources.gdelt import latest_gkg_slot

                            slot = latest_gkg_slot()
                            if slot != self._last_gdelt_slot:
                                tasks.append(self._gdelt_task(job_id, slot, self._gdelt_max_urls))
                                self._last_gdelt_slot = slot
                        if tasks:
                            self._state.add_tasks(tasks)
                            logger.info("tail_reseeded", count=len(tasks), iteration=iteration)
                            self._last_reseed_at = utcnow().isoformat()
                            self._last_reseed_count = len(tasks)
                    except Exception as exc:
                        logger.exception("tail_reseed_iteration_failed", iteration=iteration, err=str(exc))
            except asyncio.CancelledError:
                pass

        self._reseed_task = asyncio.create_task(reseed_loop())

        async def worker_loop() -> None:
            assert self._engine is not None
            try:
                run_task = asyncio.create_task(
                    self._engine.run_tail(job_id, poll_seconds=settings.tail_poll_seconds)
                )
                # C-01: if the reseed loop observed a terminal job, stop the worker
                # loop as well (belt-and-suspenders; run_tail also checks status).
                stop_watch = asyncio.create_task(self._stopped_event.wait())
                await asyncio.wait({run_task, stop_watch}, return_when=asyncio.FIRST_COMPLETED)
                if not stop_watch.done():
                    stop_watch.cancel()
                if self._stopped_event.is_set() and not run_task.done():
                    self._engine.request_stop()
                # Always await the worker task so its outcome is observed and its
                # finally-block (aclose) runs even on cancellation/exception.
                await asyncio.gather(run_task, return_exceptions=True)
            finally:
                await self._engine.aclose()

        self._task = asyncio.create_task(worker_loop())
        logger.info("tail_started", job_id=job_id)
        return job_id

    async def stop(self, *, drain_seconds: float = 30.0) -> None:
        if self._engine is None or self._task is None:
            return
        self._stop_event.set()
        self._engine.request_stop()
        if self._reseed_task is not None:
            self._reseed_task.cancel()
            try:
                await self._reseed_task
            except asyncio.CancelledError:
                pass
        try:
            await asyncio.wait_for(self._task, timeout=drain_seconds)
        except TimeoutError:
            logger.warning("tail_drain_timeout", drain_seconds=drain_seconds)
            # H-04: never abandon a still-running worker task — cancel it and
            # await it so its finally-block (aclose) actually runs.
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._job_id:
            self._planner.stop_tail(self._job_id, note="user-requested-stop")
        self._task = None
        self._engine = None
        logger.info("tail_stopped", job_id=self._job_id)
