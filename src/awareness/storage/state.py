"""State DB: jobs, tasks, manifests, dedup index, checkpoints, DLQ.

Implementation is sync SQLAlchemy 2.x over SQLite by default. The hot path
of the pipeline is text fetching/extraction; state ops are small and infrequent
so synchronous calls behind ``asyncio.to_thread`` are simpler and more reliable
than full async SQLAlchemy. The URL is fully overridable so you can point this
at Postgres in production without code changes.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Float,
    Integer,
    BigInteger,
    String,
    UniqueConstraint,
    create_engine,
    event,
    func,
    select,
    update,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from awareness.obs.logging import get_logger
from awareness.schemas.doc import SourceKind
from awareness.schemas.jobs import (
    JobKind,
    JobState,
    JobStatus,
    TaskState,
    TaskStatus,
)
from awareness.util.hashing import sig128_from_hex, sig128_to_hex

logger = get_logger("storage.state")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _verify_dedup_schema(inspector: Any) -> None:
    """Raise if the dedup_near table exists but lacks the sig_hex column.

    The migration in init() adds sig_hex to legacy tables; if that ALTER
    silently failed (locked/partial/read-only DB), surface it loudly here
    instead of deferring to a confusing 'no such column: sig_hex' on every
    later dedup write.
    """
    try:
        cols = [c["name"] for c in inspector.get_columns("dedup_near")]
    except Exception:
        cols = []
    if cols and "sig_hex" not in cols:
        raise RuntimeError(
            "dedup_near.sig_hex is missing after migration — the DB may be "
            "read-only, locked, or partially migrated; near-dup indexing "
            "would fail. Fix or recreate the state DB."
        )




class Base(DeclarativeBase):
    pass


class JobRow(Base):
    __tablename__ = "jobs"
    job_id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, index=True, default=JobStatus.PENDING.value)
    request_json: Mapped[str] = mapped_column(String, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tasks_total: Mapped[int] = mapped_column(Integer, default=0)
    tasks_completed: Mapped[int] = mapped_column(Integer, default=0)
    tasks_failed: Mapped[int] = mapped_column(Integer, default=0)
    tasks_dead_lettered: Mapped[int] = mapped_column(Integer, default=0)
    docs_emitted: Mapped[int] = mapped_column(BigInteger, default=0)
    docs_dedup_dropped: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes_processed: Mapped[int] = mapped_column(BigInteger, default=0)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)


class TaskRow(Base):
    __tablename__ = "tasks"
    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(String, index=True)
    source_type: Mapped[str] = mapped_column(String, index=True)
    partition_key: Mapped[str] = mapped_column(String, index=True)
    payload_json: Mapped[str] = mapped_column(String, default="{}")
    status: Mapped[str] = mapped_column(String, index=True, default=TaskStatus.PENDING.value)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    docs_emitted: Mapped[int] = mapped_column(BigInteger, default=0)
    docs_dedup_dropped: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes_processed: Mapped[int] = mapped_column(BigInteger, default=0)
    checkpoint_json: Mapped[str] = mapped_column(String, default="{}")
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    __table_args__ = (UniqueConstraint("job_id", "partition_key", name="uq_task_part"),)


class DedupRow(Base):
    """Stores the canonical doc_id for a content_hash so re-ingests fold cleanly.

    A new capture with the same content_hash points to the existing dup-group
    via ``parent_doc_or_dup_group = first.doc_id``.
    """

    __tablename__ = "dedup_content"
    content_hash: Mapped[str] = mapped_column(String, primary_key=True)
    first_doc_id: Mapped[str] = mapped_column(String, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    capture_count: Mapped[int] = mapped_column(Integer, default=1)


class DedupNearRow(Base):
    """Coarse simhash-bucket index for near-dup search.

    To search for near-dupes for a 128-bit simhash ``H`` we split H into
    :data:`NEAR_DUP_SEGMENTS` bands of :data:`NEAR_DUP_SEG_BITS` bits and store
    rows keyed by ``(segment_index, segment_value)``. Two near-dupes share at
    least one band exactly when their Hamming distance is ≤ (bands − 1)
    (Manku/Jain pigeonhole; 32 bands → Hamming ≤ 31), and probabilistically
    beyond it. Query is ``WHERE seg = ? AND value = ?``.

    ``sig_hex`` holds the full 128-bit signature (32 hex chars); the legacy
    ``near_dup_hash`` int column is retained, nullable, for backward
    compatibility with indexes written before the 128-bit upgrade.
    """

    __tablename__ = "dedup_near"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[str] = mapped_column(String, index=True)
    sig_hex: Mapped[str | None] = mapped_column(String, nullable=True)
    near_dup_hash: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # legacy 64-bit signed
    seg: Mapped[int] = mapped_column(Integer, index=True)
    seg_value: Mapped[int] = mapped_column(Integer, index=True)
    __table_args__ = (UniqueConstraint("doc_id", "seg", name="uq_dedup_near"),)


# 128 bits split into 32 bands of 4 bits. The Manku/Jain pigeonhole guarantee is
# "a pair within Hamming ≤ (bands-1) shares ≥1 identical band", so 32 bands give
# an EXACT-retrieval guarantee up to Hamming ≤31 — covering DEFAULT_NEAR_THRESHOLD
# (24), which 16×8 banding (guarantee ≤15) did not. Cost: 32 tiny index rows/doc.
# Reindex: existing near_dup rows written under 16×8 are wrong band width after
# upgrade; rebuild the dedup_near index if upgrading mid-corpus.
NEAR_DUP_SEGMENTS = 32
NEAR_DUP_SEG_BITS = 4
_NEAR_DUP_SEG_MASK = (1 << NEAR_DUP_SEG_BITS) - 1
# Per-band candidate cap. Higher than the 64-bit era's 256 so moderate-scale
# corpora don't silently truncate true near-dup candidates out of a hot band.
NEAR_DUP_CANDIDATE_LIMIT = 1024


class ManifestRow(Base):
    """Tracks staging chunks ready for compaction (and what's been compacted)."""

    __tablename__ = "manifests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    path: Mapped[str] = mapped_column(String, unique=True)
    records: Mapped[int] = mapped_column(Integer, default=0)
    bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    compacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class DLQRow(Base):
    __tablename__ = "dlq"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    payload_json: Mapped[str] = mapped_column(String, default="{}")
    error: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


class TailRow(Base):
    """Single-row table tracking tail daemon state (Postgres-friendly)."""

    __tablename__ = "tail_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    running: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    # Owning OS process. Used by get_tail(reconcile=True) to detect orphans
    # after a crash/kill that never called set_tail(False).
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)



class UrlFetchLogRow(Base):
    """Successful HTTP fetches keyed by canonical URL (skip re-fetch gate)."""

    __tablename__ = "url_fetch_log"
    canonical_url: Mapped[str] = mapped_column(String, primary_key=True)
    first_doc_id: Mapped[str | None] = mapped_column(String, nullable=True)
    last_content_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)


class RobotsCacheRow(Base):
    __tablename__ = "robots_cache"
    site: Mapped[str] = mapped_column(String, primary_key=True)
    robots_txt: Mapped[str | None] = mapped_column(String, nullable=True)
    expires_at: Mapped[float] = mapped_column(Float)
    crawl_delay: Mapped[float | None] = mapped_column(Float, nullable=True)


class StateDB:
    """High-level state operations used by the planner/workers/tail."""

    def __init__(self, url: str, redis_url: str | None = None) -> None:
        # Strip ``+aiosqlite`` if present; we use sync.
        if url.startswith("sqlite+aiosqlite:"):
            url = "sqlite:" + url[len("sqlite+aiosqlite:") :]
        
        self._redis_url = redis_url
        if url.startswith(("redis://", "rediss://", "redlock://", "redlocks://")):
            self._redis_url = url
            url = "sqlite:///awareness.sqlite"

        if self._redis_url is None:
            try:
                from awareness.config import get_settings
                self._redis_url = get_settings().redis_url
            except Exception:
                pass

        self._url = url
        
        # SQLite-specific logic: enable WAL mode, timeouts, etc.
        if url.startswith("sqlite:"):
            self._engine = create_engine(
                url,
                future=True,
                connect_args={"timeout": 30, "check_same_thread": False}
            )

            @event.listens_for(self._engine, "connect")
            def set_sqlite_pragma(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                try:
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.execute("PRAGMA synchronous=NORMAL")
                    cursor.execute("PRAGMA foreign_keys=ON")
                except Exception as e:
                    logger.warning("failed_to_set_sqlite_pragmas", error=str(e))
                finally:
                    cursor.close()
        else:
            self._engine = create_engine(url, future=True)

        self._sessionmaker = sessionmaker(self._engine, expire_on_commit=False)
        self._lock = threading.RLock()
        self._initialized = False

    @property
    def url(self) -> str:
        return self._url

    def init(self) -> None:
        with self._lock:
            if self._initialized:
                return
            Base.metadata.create_all(self._engine)

            # Simple automatic schema migration for legacy databases (SQLite only)
            if self._engine.dialect.name == "sqlite":
                from sqlalchemy import inspect, text

                try:
                    inspector = inspect(self._engine)
                    try:
                        near_cols = [c["name"] for c in inspector.get_columns("dedup_near")]
                    except Exception:
                        near_cols = []
                    if near_cols and "sig_hex" not in near_cols:
                        with self._engine.begin() as conn:
                            conn.execute(text("ALTER TABLE dedup_near ADD COLUMN sig_hex VARCHAR"))
                    try:
                        tail_cols = [c["name"] for c in inspector.get_columns("tail_state")]
                    except Exception:
                        tail_cols = []
                    if tail_cols and "pid" not in tail_cols:
                        with self._engine.begin() as conn:
                            conn.execute(text("ALTER TABLE tail_state ADD COLUMN pid INTEGER"))
                except Exception as e:
                    logger.warning("migration_failed", error=str(e))
            else:
                # Non-SQLite: best-effort ADD COLUMN if missing (Postgres etc.).
                from sqlalchemy import inspect, text

                try:
                    inspector = inspect(self._engine)
                    tail_cols = [c["name"] for c in inspector.get_columns("tail_state")]
                    if tail_cols and "pid" not in tail_cols:
                        with self._engine.begin() as conn:
                            conn.execute(text("ALTER TABLE tail_state ADD COLUMN pid INTEGER"))
                except Exception as e:
                    logger.warning("migration_failed", error=str(e))

            from sqlalchemy import inspect as _sa_inspect

            _verify_dedup_schema(_sa_inspect(self._engine))

            self._initialized = True

    def session(self) -> Session:
        if not self._initialized:
            self.init()
        return self._sessionmaker()

    # ── jobs ─────────────────────────────────────────────────────────────
    def create_job(self, job: JobState) -> None:
        with self.session() as s:
            row = JobRow(
                job_id=job.job_id,
                kind=job.kind.value,
                status=job.status.value,
                request_json=json.dumps(job.request),
                created_at=job.created_at,
                started_at=job.started_at,
                completed_at=job.completed_at,
                tasks_total=job.tasks_total,
                notes=job.notes,
            )
            s.add(row)
            s.commit()

    def get_job(self, job_id: str) -> JobState | None:
        with self.session() as s:
            row = s.get(JobRow, job_id)
            if row is None:
                return None
            return self._job_state_from_row(row)

    def delete_job(self, job_id: str) -> None:
        from sqlalchemy import delete
        with self.session() as s:
            s.execute(delete(TaskRow).where(TaskRow.job_id == job_id))
            s.execute(delete(JobRow).where(JobRow.job_id == job_id))
            s.commit()

    def list_jobs(self, kind: JobKind | None = None, limit: int = 50) -> list[JobState]:
        with self.session() as s:
            stmt = select(JobRow).order_by(JobRow.created_at.desc()).limit(limit)
            if kind is not None:
                stmt = stmt.where(JobRow.kind == kind.value)
            return [self._job_state_from_row(r) for r in s.scalars(stmt)]

    def set_job_status(self, job_id: str, status: JobStatus, *, note: str | None = None) -> None:
        with self.session() as s:
            row = s.get(JobRow, job_id)
            if row is None:
                return
            row.status = status.value
            now = _utcnow()
            if status == JobStatus.RUNNING and row.started_at is None:
                row.started_at = now
            if status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
                row.completed_at = now
            if note:
                row.notes = note
            s.commit()

    def increment_job_counters(
        self,
        job_id: str,
        *,
        docs: int = 0,
        dedup_dropped: int = 0,
        bytes_: int = 0,
        completed: int = 0,
        failed: int = 0,
        dead_lettered: int = 0,
    ) -> None:
        with self.session() as s:
            stmt = (
                update(JobRow)
                .where(JobRow.job_id == job_id)
                .values(
                    docs_emitted=JobRow.docs_emitted + docs,
                    docs_dedup_dropped=JobRow.docs_dedup_dropped + dedup_dropped,
                    bytes_processed=JobRow.bytes_processed + bytes_,
                    tasks_completed=JobRow.tasks_completed + completed,
                    tasks_failed=JobRow.tasks_failed + failed,
                    tasks_dead_lettered=JobRow.tasks_dead_lettered + dead_lettered,
                )
            )
            s.execute(stmt)
            s.commit()

    @staticmethod
    def _job_state_from_row(row: JobRow) -> JobState:
        return JobState(
            job_id=row.job_id,
            kind=JobKind(row.kind),
            status=JobStatus(row.status),
            request=json.loads(row.request_json or "{}"),
            created_at=row.created_at,
            started_at=row.started_at,
            completed_at=row.completed_at,
            tasks_total=row.tasks_total,
            tasks_completed=row.tasks_completed,
            tasks_failed=row.tasks_failed,
            tasks_dead_lettered=row.tasks_dead_lettered,
            docs_emitted=row.docs_emitted,
            docs_dedup_dropped=row.docs_dedup_dropped,
            bytes_processed=row.bytes_processed,
            notes=row.notes,
        )

    # ── tasks ────────────────────────────────────────────────────────────
    def add_tasks(self, tasks: Iterable[TaskState]) -> int:
        """Add or RE-ARM tasks.

        Tasks have a UNIQUE(job_id, partition_key) constraint to keep the
        planner idempotent. When a caller (notably the tail reseed loop)
        asks to add a task whose (job_id, partition_key) already exists,
        we silently RESET that existing row to PENDING so the worker pool
        picks it back up — instead of crashing on an IntegrityError. This
        makes reseed safe: re-polling the same RSS seed is just "re-arm
        the discovery task". Returns the number of NEW rows inserted.
        """
        materialized = list(tasks)
        if not materialized:
            return 0

        if self._redis_url:
            from awareness.util.lock import RedisLock
            job_id = materialized[0].job_id
            try:
                with RedisLock(self._redis_url, f"add_tasks:{job_id}", expire_sec=30.0, timeout_sec=15.0):
                    return self._do_add_tasks(materialized)
            except ImportError:
                logger.warning("redis_library_missing_falling_back_to_unlocked_add_tasks")
                return self._do_add_tasks(materialized)
            except Exception as e:
                logger.warning("redis_lock_failed_falling_back_to_unlocked_add_tasks", error=str(e))
                return self._do_add_tasks(materialized)
        else:
            return self._do_add_tasks(materialized)

    def _do_add_tasks(self, materialized: list[TaskState]) -> int:
        added = 0
        rearmed = 0
        # Count re-arms by *previous status* so we can keep the job's status
        # counters consistent (the worker will re-emit completion increments).
        rearmed_from: dict[str, int] = {}
        with self.session() as s:
            for t in materialized:
                # Look up by (job_id, partition_key) — the unique key.
                existing = s.execute(
                    select(TaskRow).where(
                        TaskRow.job_id == t.job_id,
                        TaskRow.partition_key == t.partition_key,
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    # One-shot per-URL fetch rows (tail_recrawl) must NOT be
                    # re-armed once COMPLETED: re-adding the same URL across
                    # overlapping GDELT slots would otherwise force a redundant
                    # network re-fetch before dedup can fold it. Re-arm is only
                    # for constant-key discovery rows (RSS/sitemap/GDELT-slot)
                    # that are *meant* to re-run each poll.
                    if (
                        existing.status == TaskStatus.COMPLETED.value
                        and t.source_type == SourceKind.TAIL_RECRAWL
                    ):
                        continue
                    # Re-arm: reset status, remember the previous one.
                    prev = existing.status
                    rearmed_from[prev] = rearmed_from.get(prev, 0) + 1
                    existing.status = TaskStatus.PENDING.value
                    existing.started_at = None
                    existing.completed_at = None
                    existing.last_error = None
                    # Keep attempts so we still respect max_retries semantics
                    # across reseeds.
                    rearmed += 1
                    continue
                row = TaskRow(
                    task_id=t.task_id,
                    job_id=t.job_id,
                    source_type=t.source_type.value,
                    partition_key=t.partition_key,
                    payload_json=json.dumps(t.payload),
                    status=t.status.value,
                    attempts=t.attempts,
                    last_error=t.last_error,
                    created_at=t.created_at,
                    started_at=t.started_at,
                    completed_at=t.completed_at,
                    docs_emitted=t.docs_emitted,
                    docs_dedup_dropped=t.docs_dedup_dropped,
                    bytes_processed=t.bytes_processed,
                    checkpoint_json=json.dumps(t.checkpoint or {}),
                )
                s.add(row)
                added += 1
            if added:
                s.execute(
                    update(JobRow)
                    .where(JobRow.job_id == materialized[0].job_id)
                    .values(tasks_total=JobRow.tasks_total + added)
                )
            # Roll back job counters for re-armed tasks so the worker's later
            # completion bumps don't push tasks_completed past tasks_total.
            completed_back = rearmed_from.get(TaskStatus.COMPLETED.value, 0)
            failed_back = rearmed_from.get(TaskStatus.FAILED.value, 0)
            dead_back = rearmed_from.get(TaskStatus.DEAD_LETTERED.value, 0)
            if completed_back or failed_back or dead_back:
                s.execute(
                    update(JobRow)
                    .where(JobRow.job_id == materialized[0].job_id)
                    .values(
                        tasks_completed=JobRow.tasks_completed - completed_back,
                        tasks_failed=JobRow.tasks_failed - failed_back,
                        tasks_dead_lettered=JobRow.tasks_dead_lettered - dead_back,
                    )
                )
            s.commit()
        if rearmed:
            logger.info(
                "tasks_rearmed",
                count=rearmed,
                from_=rearmed_from,
                job_id=materialized[0].job_id,
            )
        return added

    def claim_pending_tasks(self, job_id: str, limit: int) -> list[TaskState]:
        """Atomically transition PENDING tasks to RUNNING for processing."""
        if self._redis_url:
            from awareness.util.lock import RedisLock
            try:
                with RedisLock(self._redis_url, f"claim:{job_id}", expire_sec=30.0, timeout_sec=15.0):
                    return self._do_claim_pending_tasks(job_id, limit)
            except ImportError:
                logger.warning("redis_library_missing_falling_back_to_unlocked_claims")
                return self._do_claim_pending_tasks(job_id, limit)
            except Exception as e:
                logger.warning("redis_lock_failed_falling_back_to_unlocked_claims", error=str(e))
                return self._do_claim_pending_tasks(job_id, limit)
        else:
            return self._do_claim_pending_tasks(job_id, limit)

    def _do_claim_pending_tasks(self, job_id: str, limit: int) -> list[TaskState]:
        with self.session() as s:
            stmt = (
                select(TaskRow)
                .where(TaskRow.job_id == job_id, TaskRow.status == TaskStatus.PENDING.value)
                .order_by(TaskRow.created_at)
            )
            if self._engine.dialect.name != "sqlite":
                stmt = stmt.with_for_update(skip_locked=True)
            stmt = stmt.limit(limit)
            rows = list(s.scalars(stmt))
            now = _utcnow()
            candidates = list(
                s.scalars(
                    select(TaskRow.task_id)
                    .where(
                        TaskRow.job_id == job_id,
                        TaskRow.status == TaskStatus.PENDING.value,
                        (TaskRow.next_attempt_at.is_(None)) | (TaskRow.next_attempt_at <= now),
                    )
                    .order_by(TaskRow.created_at)
                    .limit(limit)
                )
            )
            if not candidates:
                return []
            claimed_ids = list(
                s.scalars(
                    update(TaskRow)
                    .where(
                        TaskRow.task_id.in_(candidates),
                        TaskRow.status == TaskStatus.PENDING.value,
                    )
                    .values(
                        status=TaskStatus.RUNNING.value,
                        started_at=now,
                        attempts=TaskRow.attempts + 1,
                    )
                    .returning(TaskRow.task_id)
                    .execution_options(synchronize_session=False)
                )
            )
            s.commit()
            rows = (
                list(s.scalars(select(TaskRow).where(TaskRow.task_id.in_(claimed_ids))))
                if claimed_ids
                else []
            )
            rows.sort(key=lambda r: (r.created_at, r.task_id))
            return [self._task_state_from_row(r) for r in rows]

    def requeue_orphaned_running(
        self, job_id: str, *, older_than_seconds: float, max_retries: int
    ) -> int:
        """Reset RUNNING tasks whose ``started_at`` is older than the lease back
        to PENDING so a worker re-claims them after a crash/stop. Tasks that have
        already exhausted ``max_retries`` are dead-lettered instead. Returns the
        number of tasks requeued (not dead-lettered).
        """
        cutoff = _utcnow() - timedelta(seconds=older_than_seconds)
        with self._lock, self.session() as s:
            rows = list(
                s.scalars(
                    select(TaskRow).where(
                        TaskRow.job_id == job_id,
                        TaskRow.status == TaskStatus.RUNNING.value,
                        TaskRow.started_at.is_not(None),
                        TaskRow.started_at < cutoff,
                    )
                )
            )
            requeued = 0
            dead = 0
            for r in rows:
                if r.attempts >= max(1, max_retries):
                    r.status = TaskStatus.DEAD_LETTERED.value
                    r.completed_at = _utcnow()
                    r.last_error = "orphaned_running_exceeded_max_retries"
                    dead += 1
                else:
                    r.status = TaskStatus.PENDING.value
                    r.started_at = None
                    requeued += 1
            if dead:
                s.execute(
                    update(JobRow)
                    .where(JobRow.job_id == job_id)
                    .values(tasks_dead_lettered=JobRow.tasks_dead_lettered + dead)
                )
            s.commit()
        if requeued or dead:
            logger.info("orphaned_running_reaped", job_id=job_id, requeued=requeued, dead=dead)
        return requeued

    def complete_task(
        self,
        task_id: str,
        *,
        docs_emitted: int,
        docs_dedup_dropped: int,
        bytes_processed: int,
        checkpoint: dict[str, Any] | None,
    ) -> None:
        with self.session() as s:
            row = s.get(TaskRow, task_id)
            if row is None:
                return
            row.status = TaskStatus.COMPLETED.value
            row.completed_at = _utcnow()
            row.docs_emitted += docs_emitted
            row.docs_dedup_dropped += docs_dedup_dropped
            row.bytes_processed += bytes_processed
            if checkpoint is not None:
                row.checkpoint_json = json.dumps(checkpoint)
            s.commit()

    def fail_task(
        self,
        task_id: str,
        *,
        error: str,
        dead_letter: bool = False,
    ) -> None:
        with self.session() as s:
            row = s.get(TaskRow, task_id)
            if row is None:
                return
            row.last_error = error[:4000]
            if dead_letter:
                row.status = TaskStatus.DEAD_LETTERED.value
                row.completed_at = _utcnow()
            else:
                row.status = TaskStatus.PENDING.value  # retry
                row.next_attempt_at = _utcnow() + timedelta(seconds=_retry_delay_seconds(row.attempts))
            s.commit()

    def get_last_task_checkpoint(self, partition_key: str) -> dict[str, Any]:
        """Find the checkpoint of the most recently completed task for a partition key."""
        with self.session() as s:
            stmt = (
                select(TaskRow.checkpoint_json)
                .where(TaskRow.partition_key == partition_key)
                .where(TaskRow.status == TaskStatus.COMPLETED.value)
                .order_by(TaskRow.completed_at.desc().nullslast())
                .limit(1)
            )
            val = s.scalar(stmt)
            if val:
                try:
                    return json.loads(val) or {}
                except Exception:
                    pass
            return {}

    def task_status_counts(self, job_id: str) -> dict[str, int]:
        with self.session() as s:
            stmt = (
                select(TaskRow.status, func.count())
                .where(TaskRow.job_id == job_id)
                .group_by(TaskRow.status)
            )
            return {status: int(n) for status, n in s.execute(stmt).all()}

    def list_running_tasks(self, job_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Return tasks currently in RUNNING state for a job, newest started first."""
        with self.session() as s:
            stmt = (
                select(TaskRow)
                .where(TaskRow.job_id == job_id, TaskRow.status == TaskStatus.RUNNING.value)
                .order_by(TaskRow.started_at.desc().nullslast())
                .limit(limit)
            )
            return [
                {
                    "task_id": r.task_id,
                    "source_type": r.source_type,
                    "partition_key": r.partition_key,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "attempts": r.attempts,
                }
                for r in s.scalars(stmt)
            ]

    def list_recent_completed_tasks(self, job_id: str, limit: int = 12) -> list[dict[str, Any]]:
        """Return most recently completed tasks for a job, latest first."""
        with self.session() as s:
            stmt = (
                select(TaskRow)
                .where(TaskRow.job_id == job_id, TaskRow.status == TaskStatus.COMPLETED.value)
                .order_by(TaskRow.completed_at.desc().nullslast())
                .limit(limit)
            )
            return [
                {
                    "task_id": r.task_id,
                    "source_type": r.source_type,
                    "partition_key": r.partition_key,
                    "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                    "docs_emitted": r.docs_emitted,
                    "docs_dedup_dropped": r.docs_dedup_dropped,
                }
                for r in s.scalars(stmt)
            ]

    def per_seed_progress(self, job_id: str) -> dict[str, Any]:
        """Aggregate task counts by seed (discovery partition).

        Tasks have partition_key like 'rss:https://hnrss.org/...' for discovery
        partitions and 'tail:https://www.bbc.com/article/...' for sub-fetches.
        We group sub-fetches by the *discovery_channel* portion of the
        payload — we don't have it directly, so we fall back to grouping
        sub-tasks by source_type. The discovery rows still come through with
        their own URL.
        """
        with self.session() as s:
            # Discovery partitions (one per seed feed) — group by partition_key.
            discovery = s.execute(
                select(TaskRow.partition_key, TaskRow.status)
                .where(
                    TaskRow.job_id == job_id,
                    TaskRow.source_type.in_(["rss", "atom", "sitemap", "gdelt"]),
                )
            ).all()
            by_seed: dict[str, dict[str, Any]] = {}
            for partition_key, status in discovery:
                seed = by_seed.setdefault(
                    partition_key,
                    {"partition_key": partition_key, "status": status, "kind": "feed"},
                )
                seed["status"] = status
            # Sub-fetch counts overall.
            tail_counts = s.execute(
                select(TaskRow.status, func.count())
                .where(TaskRow.job_id == job_id, TaskRow.source_type == "tail_recrawl")
                .group_by(TaskRow.status)
            ).all()
            tail_breakdown = {st: int(n) for st, n in tail_counts}
            return {
                "feeds": list(by_seed.values()),
                "fetch": tail_breakdown,
            }

    def list_recent_manifests(self, limit: int = 8) -> list[dict[str, Any]]:
        """Most recently committed JSONL chunks."""
        with self.session() as s:
            stmt = (
                select(ManifestRow)
                .order_by(ManifestRow.id.desc())
                .limit(limit)
            )
            return [
                {
                    "id": r.id,
                    "path": r.path,
                    "records": r.records,
                    "bytes": r.bytes,
                    "committed_at": r.committed_at.isoformat() if r.committed_at else None,
                }
                for r in s.scalars(stmt)
            ]

    @staticmethod
    def _task_state_from_row(row: TaskRow) -> TaskState:
        from awareness.schemas.doc import SourceKind

        return TaskState(
            task_id=row.task_id,
            job_id=row.job_id,
            source_type=SourceKind(row.source_type),
            partition_key=row.partition_key,
            payload=json.loads(row.payload_json or "{}"),
            status=TaskStatus(row.status),
            attempts=row.attempts,
            last_error=row.last_error,
            created_at=row.created_at,
            started_at=row.started_at,
            completed_at=row.completed_at,
            docs_emitted=row.docs_emitted,
            docs_dedup_dropped=row.docs_dedup_dropped,
            bytes_processed=row.bytes_processed,
            checkpoint=json.loads(row.checkpoint_json or "{}") or None,
        )

    # ── dedup ────────────────────────────────────────────────────────────
    def upsert_dedup(self, content_hash: str, doc_id: str) -> tuple[str, bool]:
        """Insert a new content_hash if absent. Returns (canonical_doc_id, was_new)."""
        if self._redis_url:
            from awareness.util.lock import RedisLock
            try:
                with RedisLock(self._redis_url, f"dedup:{content_hash}", expire_sec=10.0, timeout_sec=5.0):
                    return self._do_upsert_dedup(content_hash, doc_id)
            except ImportError:
                return self._do_upsert_dedup(content_hash, doc_id)
            except Exception as e:
                logger.warning("redis_lock_failed_falling_back_to_unlocked_upsert_dedup", error=str(e))
                return self._do_upsert_dedup(content_hash, doc_id)
        else:
            return self._do_upsert_dedup(content_hash, doc_id)

    def _do_upsert_dedup(self, content_hash: str, doc_id: str) -> tuple[str, bool]:
        from sqlalchemy.exc import IntegrityError
        with self.session() as s:
            row = s.get(DedupRow, content_hash)
            if row is None:
                try:
                    s.add(DedupRow(content_hash=content_hash, first_doc_id=doc_id))
                    s.commit()
                    return doc_id, True
                except IntegrityError:
                    s.rollback()
                    # Re-fetch since another worker inserted it concurrently
                    row = s.get(DedupRow, content_hash)
                    if row is not None:
                        row.capture_count += 1
                        s.commit()
                        return row.first_doc_id, False
                    return doc_id, True
            row.capture_count += 1
            s.commit()
            return row.first_doc_id, False

    def add_near_dup_index(self, doc_id: str, simhash_unsigned: int) -> None:
        """Insert ``NEAR_DUP_SEGMENTS`` band rows for a 128-bit signature."""
        if simhash_unsigned <= 0:
            return
        sig_hex = sig128_to_hex(simhash_unsigned)
        # Calculate legacy 64-bit signed hash for backward compatibility / database schema constraints
        legacy_hash = simhash_unsigned & 0xffffffffffffffff
        if legacy_hash >= (1 << 63):
            legacy_hash -= (1 << 64)

        from sqlalchemy.exc import IntegrityError
        with self.session() as s:
            for seg in range(NEAR_DUP_SEGMENTS):
                value = (simhash_unsigned >> (NEAR_DUP_SEG_BITS * seg)) & _NEAR_DUP_SEG_MASK
                # Check by unique constraint (doc_id, seg)
                row = s.execute(
                    select(DedupNearRow).where(
                        DedupNearRow.doc_id == doc_id,
                        DedupNearRow.seg == seg,
                    )
                ).scalar_one_or_none()
                if row is None:
                    try:
                        s.add(
                            DedupNearRow(
                                doc_id=doc_id,
                                sig_hex=sig_hex,
                                near_dup_hash=legacy_hash,
                                seg=seg,
                                seg_value=value,
                            )
                        )
                        s.commit()
                    except IntegrityError:
                        s.rollback()
                        row = s.execute(
                            select(DedupNearRow).where(
                                DedupNearRow.doc_id == doc_id,
                                DedupNearRow.seg == seg,
                            )
                        ).scalar_one_or_none()
                        if row is not None:
                            row.sig_hex = sig_hex
                            row.near_dup_hash = legacy_hash
                            row.seg_value = value
                            s.commit()
                else:
                    row.sig_hex = sig_hex
                    row.near_dup_hash = legacy_hash
                    row.seg_value = value
                    s.commit()

    def find_near_dup_candidates(self, simhash_unsigned: int) -> list[tuple[str, int]]:
        """Look up doc_ids that share at least one band with this 128-bit signature.

        Returns ``(doc_id, signature_int)`` pairs. Rows written by the legacy
        64-bit index (``sig_hex`` NULL) fall back to their stored int.
        """
        out: dict[str, int] = {}
        with self.session() as s:
            for seg in range(NEAR_DUP_SEGMENTS):
                value = (simhash_unsigned >> (NEAR_DUP_SEG_BITS * seg)) & _NEAR_DUP_SEG_MASK
                stmt = (
                    select(DedupNearRow.doc_id, DedupNearRow.sig_hex, DedupNearRow.near_dup_hash)
                    .where(DedupNearRow.seg == seg, DedupNearRow.seg_value == value)
                    .limit(NEAR_DUP_CANDIDATE_LIMIT)
                )
                for did, sig_hex, legacy in s.execute(stmt).all():
                    if sig_hex:
                        out[did] = sig128_from_hex(sig_hex)
                    elif legacy is not None:
                        out[did] = legacy & 0xffffffffffffffff
        return list(out.items())

    def dedup_stats(self) -> dict[str, int]:
        with self.session() as s:
            distinct = int(s.scalar(select(func.count(DedupRow.content_hash))) or 0)
            captures_sum = int(s.scalar(select(func.coalesce(func.sum(DedupRow.capture_count), 0))) or 0)
            return {
                "distinct_content_hashes": distinct,
                "total_captures_seen": captures_sum,
                "near_dup_index_rows": int(s.scalar(select(func.count(DedupNearRow.id))) or 0),
            }

    # ── manifests ────────────────────────────────────────────────────────
    def add_manifest(self, path: str, records: int, bytes_: int) -> None:
        from sqlalchemy.exc import IntegrityError
        with self.session() as s:
            row = s.execute(select(ManifestRow).where(ManifestRow.path == path)).scalar_one_or_none()
            if row is None:
                try:
                    s.add(ManifestRow(path=path, records=records, bytes=bytes_))
                    s.commit()
                except IntegrityError:
                    s.rollback()
                    # Re-fetch and update
                    row = s.execute(select(ManifestRow).where(ManifestRow.path == path)).scalar_one_or_none()
                    if row is not None:
                        row.records = records
                        row.bytes = bytes_
                        s.commit()
            else:
                row.records = records
                row.bytes = bytes_
                s.commit()

    def list_pending_manifests(self) -> list[dict[str, Any]]:
        with self.session() as s:
            stmt = select(ManifestRow).where(ManifestRow.compacted_at.is_(None)).order_by(ManifestRow.id)
            return [
                {"id": r.id, "path": r.path, "records": r.records, "bytes": r.bytes}
                for r in s.scalars(stmt)
            ]

    def mark_manifest_compacted(self, manifest_id: int) -> None:
        with self.session() as s:
            row = s.get(ManifestRow, manifest_id)
            if row is None:
                return
            row.compacted_at = _utcnow()
            s.commit()

    # ── DLQ ──────────────────────────────────────────────────────────────
    def add_dlq(self, job_id: str | None, task_id: str | None, payload: dict[str, Any], error: str) -> None:
        with self.session() as s:
            s.add(
                DLQRow(
                    job_id=job_id,
                    task_id=task_id,
                    payload_json=json.dumps(payload),
                    error=error[:4000],
                )
            )
            s.commit()

    # ── tail state ───────────────────────────────────────────────────────
    @staticmethod
    def _pid_alive(pid: int | None) -> bool:
        if pid is None or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def set_tail(
        self,
        running: bool,
        job_id: str | None = None,
        note: str | None = None,
        pid: int | None = None,
    ) -> None:
        with self.session() as s:
            row = s.get(TailRow, 1)
            now = _utcnow()
            if row is None:
                row = TailRow(id=1)
                s.add(row)
            row.running = 1 if running else 0
            if running:
                row.started_at = now
                row.stopped_at = None
                row.job_id = job_id
                # Default to the calling process when starting so crashes are detectable.
                row.pid = int(pid) if pid is not None else os.getpid()
            else:
                row.stopped_at = now
                row.pid = None
                if job_id is not None:
                    row.job_id = job_id
            if note:
                row.notes = note
            s.commit()

    def _tail_info_from_row(self, row: TailRow | None) -> dict[str, Any]:
        if row is None:
            return {
                "running": False,
                "job_id": None,
                "started_at": None,
                "stopped_at": None,
                "pid": None,
            }
        return {
            "running": bool(row.running),
            "job_id": row.job_id,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "stopped_at": row.stopped_at.isoformat() if row.stopped_at else None,
            "notes": row.notes,
            "pid": int(row.pid) if row.pid is not None else None,
        }

    def get_tail(self, *, reconcile: bool = True) -> dict[str, Any]:
        """Return tail daemon status.

        When ``reconcile`` is True (default), a ``running=true`` row whose
        owning PID is dead (or was never recorded — legacy) is marked stopped
        so CLI/API status never lies after a crash.

        Reconciliation uses a conditional UPDATE so a concurrent
        ``set_tail(True)`` restart cannot be clobbered.
        """
        with self.session() as s:
            row = s.get(TailRow, 1)
            info = self._tail_info_from_row(row)
            if not reconcile or not info["running"]:
                return info

            observed_pid = info.get("pid")
            alive = self._pid_alive(observed_pid if isinstance(observed_pid, int) else None)
            if alive:
                return info

            stale_job = info.get("job_id")
            reason = (
                "reconciled-stale-no-pid"
                if observed_pid is None
                else f"reconciled-dead-pid:{observed_pid}"
            )
            now = _utcnow()
            # Atomic conditional clear of the exact orphan we observed.
            stmt = (
                update(TailRow)
                .where(TailRow.id == 1, TailRow.running == 1)
                .values(running=0, stopped_at=now, pid=None, notes=reason)
            )
            if observed_pid is None:
                stmt = stmt.where(TailRow.pid.is_(None))
            else:
                stmt = stmt.where(TailRow.pid == observed_pid)
            result = s.execute(stmt)
            s.commit()
            cleared = int(result.rowcount or 0) > 0
            cancelled_job = stale_job if cleared and isinstance(stale_job, str) else None

        if cancelled_job:
            try:
                from awareness.schemas.jobs import JobStatus  # local import

                self.set_job_status(cancelled_job, JobStatus.CANCELLED, note="orphaned-by-process-exit")
            except Exception as exc:  # noqa: BLE001
                logger.warning("tail_reconcile_job_status_failed", job_id=cancelled_job, error=str(exc))

        return self.get_tail(reconcile=False)

    def abandon_inflight_tasks(self, job_id: str, *, note: str = "orphaned-process-exit") -> int:
        """Mark PENDING/RUNNING tasks for a dead job as SKIPPED so the UI stops lying."""
        with self.session() as s:
            now = _utcnow()
            stmt = (
                update(TaskRow)
                .where(
                    TaskRow.job_id == job_id,
                    TaskRow.status.in_(
                        [TaskStatus.PENDING.value, TaskStatus.RUNNING.value]
                    ),
                )
                .values(
                    status=TaskStatus.SKIPPED.value,
                    completed_at=now,
                    last_error=note[:4000] if note else None,
                )
            )
            res = s.execute(stmt)
            s.commit()
            n = int(res.rowcount or 0)
            if n:
                logger.info("inflight_tasks_abandoned", job_id=job_id, count=n, note=note)
            return n

    def reconcile_orphan_tail_jobs(self, *, limit: int = 200) -> int:
        """Cancel TAIL jobs stuck in RUNNING that are not the live tail owner.

        Historical crashes left ``jobs.status='running'`` while ``tail_state``
        was cleared. Status/UI then listed phantom jobs forever. Also abandons
        PENDING/RUNNING tasks so Tail page counters go quiet.
        """
        from awareness.schemas.jobs import JobKind, JobStatus  # local import

        info = self.get_tail(reconcile=True)
        active_id = info.get("job_id") if info.get("running") else None
        cancelled = 0
        for job in self.list_jobs(kind=JobKind.TAIL, limit=limit):
            if job.status == JobStatus.RUNNING:
                if active_id is not None and job.job_id == active_id:
                    continue
                self.set_job_status(job.job_id, JobStatus.CANCELLED, note="orphaned-reconcile-sweep")
                self.abandon_inflight_tasks(job.job_id, note="orphaned-reconcile-sweep")
                cancelled += 1
            elif job.status in (JobStatus.CANCELLED, JobStatus.COMPLETED, JobStatus.FAILED):
                # Tasks may still be PENDING/RUNNING after a crash mid-stop.
                if active_id is not None and job.job_id == active_id:
                    continue
                abandoned = self.abandon_inflight_tasks(
                    job.job_id, note="stale-tasks-on-terminal-job"
                )
                if abandoned:
                    cancelled += 1  # count as reconcile work units
        if cancelled:
            logger.info("orphan_tail_jobs_reconciled", count=cancelled, active=active_id)
        return cancelled

    # ── database reaper / cleanup ────────────────────────────────────────
    def cleanup_old_tasks(self, retention_days: int) -> int:
        """Delete completed tasks older than retention_days."""
        from datetime import timedelta
        from sqlalchemy import delete

        threshold = _utcnow() - timedelta(days=retention_days)
        with self.session() as s:
            stmt = (
                delete(TaskRow)
                .where(TaskRow.completed_at.is_not(None))
                .where(TaskRow.completed_at < threshold)
            )
            res = s.execute(stmt)
            s.commit()
            return res.rowcount

    def vacuum_database(self) -> None:
        """Run database VACUUM (or Postgres VACUUM/ANALYZE if configured)."""
        from sqlalchemy import text

        dialect_name = self._engine.dialect.name
        try:
            with self._engine.connect() as conn:
                # Vacuum and Analyze commands cannot run inside a transaction block.
                # Setting AUTOCOMMIT isolation level ensures they run outside a transaction.
                conn = conn.execution_options(isolation_level="AUTOCOMMIT")
                if dialect_name == "postgresql":
                    logger.info("vacuum_postgres_start")
                    conn.execute(text("VACUUM"))
                    conn.execute(text("ANALYZE"))
                    logger.info("vacuum_postgres_success")
                else:
                    logger.info("vacuum_sqlite_start")
                    conn.execute(text("VACUUM"))
                    logger.info("vacuum_sqlite_success")
        except Exception as e:
            logger.warning("vacuum_failed", error=str(e))
            raise

    # ── robots cache ─────────────────────────────────────────────────────
    def get_robots_cache(self, site: str) -> RobotsCacheRow | None:
        with self.session() as s:
            return s.get(RobotsCacheRow, site)

    def set_robots_cache(self, site: str, robots_txt: str | None, expires_at: float, crawl_delay: float | None) -> None:
        with self.session() as s:
            s.merge(
                RobotsCacheRow(
                    site=site,
                    robots_txt=robots_txt,
                    expires_at=expires_at,
                    crawl_delay=crawl_delay,
                )
            )
            s.commit()

    # ── url fetch log (tail_recrawl skip gate) ───────────────────────────
    def record_url_fetch(
        self,
        canonical_url: str,
        doc_id: str | None = None,
        content_hash: str | None = None,
        *,
        http_status: int | None = None,
    ) -> None:
        """Record a successful fetch of ``canonical_url`` (upsert)."""
        if not canonical_url:
            return
        with self.session() as s:
            row = s.get(UrlFetchLogRow, canonical_url)
            if row is None:
                s.add(
                    UrlFetchLogRow(
                        canonical_url=canonical_url,
                        first_doc_id=doc_id,
                        last_content_hash=content_hash,
                        fetched_at=_utcnow(),
                        http_status=http_status,
                    )
                )
            else:
                if row.first_doc_id is None and doc_id:
                    row.first_doc_id = doc_id
                if content_hash is not None:
                    row.last_content_hash = content_hash
                row.fetched_at = _utcnow()
                if http_status is not None:
                    row.http_status = http_status
            s.commit()

    def was_url_fetched(self, canonical_url: str) -> bool:
        """True if ``canonical_url`` was previously recorded as successfully fetched."""
        if not canonical_url:
            return False
        with self.session() as s:
            return s.get(UrlFetchLogRow, canonical_url) is not None

    def get_url_fetch(self, canonical_url: str) -> UrlFetchLogRow | None:
        """Return the fetch-log row for ``canonical_url``, or None."""
        if not canonical_url:
            return None
        with self.session() as s:
            return s.get(UrlFetchLogRow, canonical_url)

