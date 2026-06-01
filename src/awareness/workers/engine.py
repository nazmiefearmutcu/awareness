"""Worker engine.

Pulls pending tasks for a job, runs the adapter's ``run_partition``, captures
sub-partition enqueues, runs dedup, writes JSONL + Iceberg.

Pipeline stages (per task):
    [adapter.run_partition] → [dedup] → [storage]

We use an in-process asyncio bus per worker; bounded queue sizes enforce
backpressure. The whole worker loop coordinates many parallel tasks
(default ``worker_concurrency``).
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid

from rich.console import Console
from rich.markup import escape

from awareness.config import get_settings
from awareness.dedup.engine import DedupDecision, DedupEngine
from awareness.obs.logging import get_logger
from awareness.obs.metrics import get_metrics
from awareness.planner.planner import Planner
from awareness.schemas.doc import DocCapture
from awareness.schemas.jobs import JobStatus, TaskState
from awareness.sources import get_adapter_registry
from awareness.sources.base import AdapterContext, PartitionSpec
from awareness.storage.iceberg import IcebergWriter
from awareness.storage.jsonl import JsonlStagingWriter
from awareness.storage.state import StateDB

logger = get_logger("workers")


def _format_size(size_bytes: int) -> str:
    if size_bytes == 0:
        return "0 B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    import math
    try:
        i = int(math.floor(math.log(size_bytes, 1024)))
        p = math.pow(1024, i)
        s = round(size_bytes / p, 2)
        return f"{s} {size_name[i]}"
    except Exception:
        return f"{size_bytes} B"


class WorkerEngine:
    """Drives task execution for one job at a time (or many concurrently)."""

    def __init__(
        self,
        state: StateDB,
        planner: Planner,
        *,
        jsonl_writer: JsonlStagingWriter | None = None,
        iceberg_writer: IcebergWriter | None = None,
        concurrency: int | None = None,
    ) -> None:
        self._state = state
        self._planner = planner
        self._registry = get_adapter_registry()
        self._dedup = DedupEngine(state)
        settings = get_settings()
        self._concurrency = concurrency or settings.worker_concurrency
        self._jsonl = jsonl_writer or JsonlStagingWriter(
            root=settings.staging_jsonl_dir(),
            flush_seconds=settings.storage_flush_seconds,
            max_records_per_file=settings.storage_flush_records,
        )
        self._iceberg: IcebergWriter | None = None
        if settings.enable_iceberg:
            assert settings.iceberg_catalog_db is not None
            assert settings.iceberg_warehouse is not None
            self._iceberg = iceberg_writer or IcebergWriter(
                catalog_db=settings.iceberg_catalog_db,
                warehouse=settings.iceberg_warehouse,
            )
            try:
                self._iceberg.ensure_table()
            except Exception as exc:
                logger.warning("iceberg_init_failed", err=str(exc))
                self._iceberg = None
        self._stop_event = asyncio.Event()
        self._batch_buffer: list[DocCapture] = []
        self._buffer_lock = asyncio.Lock()
        self._last_flush_at = time.time()
        self._console = Console()
        self._is_tty = sys.stdout.isatty()
        self._total_bytes_processed = 0
        self._total_docs_processed = 0

    # ── lifecycle ────────────────────────────────────────────────────────
    def request_stop(self) -> None:
        self._stop_event.set()

    def is_stopping(self) -> bool:
        return self._stop_event.is_set()

    async def aclose(self) -> None:
        await self._flush(force=True)
        self._jsonl.close()
        if self._iceberg is not None:
            self._iceberg.close()

    # ── public: run a job to completion ──────────────────────────────────
    async def run_job(self, job_id: str, *, poll_seconds: float = 0.5) -> None:
        """Drain all PENDING tasks for ``job_id`` using a worker pool."""
        self._state.set_job_status(job_id, JobStatus.RUNNING)
        sem = asyncio.Semaphore(self._concurrency)

        # Initialize running metrics from DB if available
        job = self._state.get_job(job_id)
        if job:
            self._total_bytes_processed = job.bytes_processed or 0
            self._total_docs_processed = job.docs_emitted or 0

        async def run_one(task: TaskState) -> None:
            async with sem:
                await self._run_task(task)

        try:
            empty_polls = 0
            while not self.is_stopping():
                tasks = self._state.claim_pending_tasks(job_id, limit=self._concurrency * 2)
                if not tasks:
                    empty_polls += 1
                    if empty_polls >= 3:
                        break
                    await asyncio.sleep(poll_seconds)
                    continue
                empty_polls = 0
                await asyncio.gather(*(run_one(t) for t in tasks), return_exceptions=False)
                await self._flush(force=False)
        finally:
            await self._flush(force=True)
            job = self._state.get_job(job_id)
            if job and job.status not in (
                JobStatus.CANCELLED,
                JobStatus.COMPLETED,
                JobStatus.FAILED,
            ):
                self._state.set_job_status(job_id, JobStatus.COMPLETED)

    async def run_tail(self, job_id: str, *, poll_seconds: float) -> None:
        """Like run_job, but never stops until ``request_stop`` is set."""
        sem = asyncio.Semaphore(self._concurrency)

        # Initialize running metrics from DB if available
        job = self._state.get_job(job_id)
        if job:
            self._total_bytes_processed = job.bytes_processed or 0
            self._total_docs_processed = job.docs_emitted or 0

        async def run_one(task: TaskState) -> None:
            async with sem:
                await self._run_task(task)

        try:
            while not self.is_stopping():
                tasks = self._state.claim_pending_tasks(job_id, limit=self._concurrency * 2)
                if not tasks:
                    await asyncio.sleep(min(poll_seconds, 1.0))
                    continue
                await asyncio.gather(*(run_one(t) for t in tasks), return_exceptions=False)
                await self._flush(force=False)
        finally:
            await self._flush(force=True)

    # ── single task ──────────────────────────────────────────────────────
    async def _run_task(self, task: TaskState) -> None:
        adapter = self._registry.get(task.source_type)
        if adapter is None:
            self._state.fail_task(task.task_id, error=f"no_adapter:{task.source_type.value}", dead_letter=True)
            self._state.add_dlq(task.job_id, task.task_id, task.payload, error="no_adapter")
            return

        settings = get_settings()
        batch_id = f"b-{uuid.uuid4().hex[:8]}"
        context = AdapterContext(
            user_agent=settings.user_agent,
            job_id=task.job_id,
            task_id=task.task_id,
            batch_id=batch_id,
            ingest_version=settings.ingest_version,
            checkpoint=dict(task.checkpoint or {}),
            is_stopping=self.is_stopping,
        )
        partition = PartitionSpec(
            source_type=task.source_type,
            partition_key=task.partition_key,
            payload=task.payload,
        )

        docs_emitted = 0
        dedup_dropped = 0
        bytes_processed = 0
        try:
            async for cap in adapter.run_partition(partition, context):
                outcome = self._dedup.evaluate(cap)
                # Persist all captures (provenance), but track stats.
                async with self._buffer_lock:
                    self._batch_buffer.append(cap)
                docs_emitted += 1
                bytes_processed += len(cap.text)
                self._total_bytes_processed += len(cap.text)
                self._total_docs_processed += 1
                if outcome.decision in (DedupDecision.EXACT_DUP, DedupDecision.NEAR_DUP, DedupDecision.REVISION):
                    dedup_dropped += 1
                get_metrics().inc(
                    "dedup.decisions",
                    labels={"decision": outcome.decision.value, "source": task.source_type.value},
                )
                if self._is_tty:
                    title = cap.title or "(No Title)"
                    if len(title) > 50:
                        title = title[:47] + "..."
                    domain = cap.domain or (cap.url.split("/")[2] if cap.url and "//" in cap.url else cap.url) or "unknown"
                    if len(domain) > 30:
                        domain = domain[:27] + "..."
                    chars = len(cap.text)
                    lang = cap.language or "unknown"
                    decision_str = outcome.decision.value.upper()
                    
                    if outcome.decision == DedupDecision.UNIQUE:
                        style = "bold green"
                    elif outcome.decision in (DedupDecision.EXACT_DUP, DedupDecision.NEAR_DUP):
                        style = "bold yellow"
                    elif outcome.decision == DedupDecision.REVISION:
                        style = "bold blue"
                    else:
                        style = "bold white"
                    
                    self._console.print(
                        f"[cyan]📥[/cyan] [[{style}]{decision_str:^9}[/{style}]] "
                        f"[bold white]{escape(title)}[/bold white] | [dim]{escape(domain)}[/dim] "
                        f"({chars} chars, {escape(lang)} | Total: [green]{_format_size(self._total_bytes_processed)}[/green], {self._total_docs_processed} docs)"
                    )
                if len(self._batch_buffer) >= settings.storage_flush_records:
                    await self._flush(force=False)
        except Exception as exc:
            logger.exception("task_failed", task_id=task.task_id, err=str(exc))
            dead = task.attempts >= max(1, settings.max_retries)
            self._state.fail_task(task.task_id, error=str(exc), dead_letter=dead)
            if dead:
                self._state.add_dlq(task.job_id, task.task_id, task.payload, error=str(exc))
                self._state.increment_job_counters(task.job_id, dead_lettered=1)
            return

        # Pick up sub-partitions emitted by adapter (e.g. CC discovery).
        enqueue: list[PartitionSpec] = context.extras.get("enqueue", []) if context.extras else []
        if enqueue:
            added = self._planner.enqueue_subpartitions(task.job_id, enqueue)
            logger.info("subpartitions_enqueued", task_id=task.task_id, added=added)

        # Commit task state.
        self._state.complete_task(
            task.task_id,
            docs_emitted=docs_emitted,
            docs_dedup_dropped=dedup_dropped,
            bytes_processed=bytes_processed,
            checkpoint=context.checkpoint,
        )
        self._state.increment_job_counters(
            task.job_id,
            docs=docs_emitted,
            dedup_dropped=dedup_dropped,
            bytes_=bytes_processed,
            completed=1,
        )
        get_metrics().inc("tasks.completed", labels={"source": task.source_type.value})
        get_metrics().inc("docs.emitted", value=docs_emitted, labels={"source": task.source_type.value})

    # ── flushing ─────────────────────────────────────────────────────────
    async def _flush(self, *, force: bool) -> None:
        """Write the buffered captures to JSONL (and Iceberg). Idempotent."""
        async with self._buffer_lock:
            if not self._batch_buffer:
                return
            settings = get_settings()
            now = time.time()
            if not force and len(self._batch_buffer) < settings.storage_flush_records and (now - self._last_flush_at) < settings.storage_flush_seconds:
                return
            rows = [c.as_iceberg_row() for c in self._batch_buffer]
            n = len(rows)
            self._batch_buffer.clear()
            self._last_flush_at = now

        # JSONL always first if enabled or if we need a temporary chunk for cloud uploading.
        written = 0
        chunk = None
        if settings.enable_jsonl_staging or settings.enable_iceberg:
            try:
                written = await asyncio.get_event_loop().run_in_executor(None, self._jsonl.write, rows)
            except Exception:
                logger.exception("jsonl_write_failed")
                return
            
            chunk = self._jsonl.flush()
            if chunk is not None and chunk.exists():
                try:
                    bytes_ = chunk.stat().st_size
                except OSError:
                    bytes_ = 0
                
                # Record in sqlite manifest only if staging is enabled.
                if settings.enable_jsonl_staging:
                    await asyncio.get_event_loop().run_in_executor(
                        None, self._state.add_manifest, str(chunk), n, bytes_
                    )
                
                from awareness.storage import gdrive
                if gdrive.is_authorized() and settings.enable_iceberg:
                    if self._is_tty:
                        self._console.print(
                            f"[bold blue]☁️  [GDrive][/bold blue] Uploading JSONL chunk [bold]{chunk.name}[/bold]..."
                        )
                    def _upload():
                        try:
                            return gdrive.upload_file(chunk)
                        except Exception as e:
                            logger.exception("gdrive_upload_failed", err=str(e))
                            return None
                    file_id = await asyncio.get_event_loop().run_in_executor(None, _upload)
                    if file_id:
                        if self._is_tty:
                            self._console.print(
                                f"[bold green]☁️  [GDrive] ✔ Uploaded successfully![/bold green] File ID: [dim]{file_id}[/dim]"
                            )
                    else:
                        if self._is_tty:
                            self._console.print(
                                "[bold red]☁️  [GDrive] ✘ Upload failed. Check application logs for details.[/bold red]"
                            )
                
                # Clean up staging file if local staging is disabled.
                if not settings.enable_jsonl_staging:
                    try:
                        chunk.unlink(missing_ok=True)
                    except OSError:
                        pass

        # Iceberg if enabled.
        if self._iceberg is not None and rows:
            try:
                await asyncio.get_event_loop().run_in_executor(None, self._iceberg.append, rows)
            except Exception as exc:
                logger.warning("iceberg_append_failed", err=str(exc))

        get_metrics().add("flushes.records", written or n)
