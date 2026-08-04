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
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    Integer,
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
    """Raise if the dedup_near table exists but lacks a required column.

    The migration in init() adds sig_hex (and the W19 token-set sketch
    columns token_hash/token_count) to legacy tables; if those ALTERs
    silently failed (locked/partial/read-only DB), surface it loudly here
    instead of deferring to a confusing 'no such column: ...' on every
    later dedup write.
    """
    try:
        cols = [c["name"] for c in inspector.get_columns("dedup_near")]
    except Exception:
        cols = []
    if cols:
        missing = [c for c in ("sig_hex", "token_hash", "token_count") if c not in cols]
        if missing:
            raise RuntimeError(
                "dedup_near is missing column(s) "
                + ", ".join(missing)
                + " after migration — the DB may be read-only, locked, or "
                "partially migrated; near-dup indexing would fail. Fix or "
                "recreate the state DB."
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
    :data:`_NEAR_DUP_DATA_BANDS` bands of :data:`NEAR_DUP_SEG_BITS` bits and
    store rows keyed by ``(segment_index, segment_value)``. Two near-dupes
    share at least one band exactly when their Hamming distance is ≤ (bands - 1) (Manku/Jain pigeonhole), and probabilistically beyond it. Query is
    ``WHERE seg = ? AND value = ?``.

    ``sig_hex`` holds the full 128-bit signature (32 hex chars); the legacy
    ``near_dup_hash`` int column is retained, nullable, for backward
    compatibility with indexes written before the 128-bit upgrade.

    ``token_hash`` / ``token_count`` are the W19 token-set sketch: a 64-bit
    xxh3 hash (stored signed) over the sorted unique tokens of the
    normalized text plus the unique-token count. Written by the dedup engine
    at index time and read back by the content-diversity guard so two
    DISTINCT articles sharing only template/boilerplate text do not merge
    into one dup group. Legacy rows (NULL sketch) are treated as 'unknown'
    and merge by Hamming distance alone (old behavior).
    """

    __tablename__ = "dedup_near"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[str] = mapped_column(String, index=True)
    sig_hex: Mapped[str | None] = mapped_column(String, nullable=True)
    near_dup_hash: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # legacy 64-bit signed
    token_hash: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # W19 token-set sketch
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)  # W19 unique-token count
    seg: Mapped[int] = mapped_column(Integer, index=True)
    seg_value: Mapped[int] = mapped_column(Integer, index=True)
    __table_args__ = (UniqueConstraint("doc_id", "seg", name="uq_dedup_near"),)


class DupParentRow(Base):
    """Union-find parent map for near-duplicate clusters.

    Each ``doc_id`` points at a ``parent_id`` in the same table. Roots have
    ``parent_id == doc_id``. Missing rows are treated as self-roots by
    :meth:`StateDB.uf_find`. Path compression rewrites intermediate parents to
    the root on find so A~B and B~C share one canonical parent.
    """

    __tablename__ = "dup_parent"
    doc_id: Mapped[str] = mapped_column(String, primary_key=True)
    parent_id: Mapped[str] = mapped_column(String, nullable=False, index=True)


# 128 bits split into 32 bands of 8 bits (H-24). 8-bit band VALUES give 1/256
# selectivity per (seg, value) bucket, so the 1024-row candidate cap covers
# ~256k docs before silent truncation (the old 32x4 layout had 1/16 selectivity
# and truncated past ~16k docs — empirically 61%/82%/96% recall loss at
# 50k/100k/500k docs). Cost: 16 tiny index rows/doc.
#
# Note on the pigeonhole guarantee: a signature is only 128 bits, so only
# 128 // 8 = 16 bands carry real bits (see _NEAR_DUP_DATA_BANDS) — bands beyond
# the signature width are constant-zero for every doc and are NOT indexed
# (indexing them would put the whole corpus in one bucket and flood every
# query). The exact-retrieval guarantee is therefore Hamming ≤ 15; distances
# 16..31 (which includes DEFAULT_NEAR_THRESHOLD=24) are retrieved
# probabilistically — per-band miss ≈ (15/16)^d, i.e. < ~1.5% at d=24 — a
# massive improvement over the 4-bit layout's capacity-driven 61%+ miss.
#
# Reindex: dedup_near rows written under the old 32x4 layout are wrong band
# width after this upgrade; init() detects the legacy layout and warns loudly —
# rebuild with `DELETE FROM dedup_near;` then re-run ingestion/dedup so band
# rows are regenerated at 8-bit width.
NEAR_DUP_SEGMENTS = 32
NEAR_DUP_SEG_BITS = 8
_NEAR_DUP_SEG_MASK = (1 << NEAR_DUP_SEG_BITS) - 1
# Bands that can carry signature bits: 128-bit signatures / 8 bits per band.
_NEAR_DUP_DATA_BANDS = min(NEAR_DUP_SEGMENTS, 128 // NEAR_DUP_SEG_BITS)
# Per-band candidate cap. Higher than the 64-bit era's 256 so moderate-scale
# corpora don't silently truncate true near-dup candidates out of a hot band.
NEAR_DUP_CANDIDATE_LIMIT = 1024

# Legacy 4-bit layout detection: 8-bit band values are uniform in [0, 255], so
# P(all 32 band values of a doc < 16) = (1/16)^32 — a legacy 32x4 table (all
# seg_value < 16) is unmistakable on any non-empty corpus.
_LEGACY_4BIT_MAX_SEG_VALUE = (1 << 4) - 1

# Exponential backoff for failed-task retries: base * 2**(attempts-1), capped.
RETRY_BACKOFF_BASE_SECONDS = 30
RETRY_BACKOFF_CAP_SECONDS = 3600


def _retry_delay_seconds(attempts: int) -> float:
    exp = max(0, attempts - 1)
    return float(min(RETRY_BACKOFF_BASE_SECONDS * (2**exp), RETRY_BACKOFF_CAP_SECONDS))


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
    # W6C-bug1: one DLQ row per task. The unique index makes add_dlq() safe
    # under concurrent multi-process reapers (e.g. requeue_orphaned_running on
    # Postgres) — the dead-letter insert becomes conflict-tolerant. Both NULL
    # task_ids (a few legacy callers) stay allowed: NULLs never collide in a
    # SQL UNIQUE index on either SQLite or Postgres.
    __table_args__ = (UniqueConstraint("task_id", name="uq_dlq_task"),)


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
                url, future=True, connect_args={"timeout": 30, "check_same_thread": False}
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
            # W6C-bug2: Postgres needs pool tuning — a stale connection must be
            # pre-pinged instead of surfacing mid-job, and the default tiny pool
            # (5+10) serializes claim/checkpoint bursts across workers. Sizes are
            # a sane default for the worker-concurrency scale this pipeline uses;
            # SQLite keeps its single-file pool untouched above.
            self._engine = create_engine(
                url,
                future=True,
                pool_pre_ping=True,
                pool_size=10,
                max_overflow=20,
            )

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
                    # W19: token-set sketch columns for the content-diversity
                    # guard (mirror the sig_hex pattern; NULL rows = legacy).
                    if near_cols and "token_hash" not in near_cols:
                        with self._engine.begin() as conn:
                            conn.execute(text("ALTER TABLE dedup_near ADD COLUMN token_hash BIGINT"))
                    if near_cols and "token_count" not in near_cols:
                        with self._engine.begin() as conn:
                            conn.execute(text("ALTER TABLE dedup_near ADD COLUMN token_count INTEGER"))
                    try:
                        tail_cols = [c["name"] for c in inspector.get_columns("tail_state")]
                    except Exception:
                        tail_cols = []
                    if tail_cols and "pid" not in tail_cols:
                        with self._engine.begin() as conn:
                            conn.execute(text("ALTER TABLE tail_state ADD COLUMN pid INTEGER"))
                    try:
                        task_cols = [c["name"] for c in inspector.get_columns("tasks")]
                    except Exception:
                        task_cols = []
                    if task_cols and "next_attempt_at" not in task_cols:
                        with self._engine.begin() as conn:
                            conn.execute(text("ALTER TABLE tasks ADD COLUMN next_attempt_at DATETIME"))
                            conn.execute(
                                text(
                                    "CREATE INDEX IF NOT EXISTS ix_tasks_next_attempt_at "
                                    "ON tasks (next_attempt_at)"
                                )
                            )
                    # W6C-bug1: legacy databases predate the DLQ unique index
                    # (uq_dlq_task). add_dlq() relies on it to dedupe concurrent
                    # dead-letter inserts, so create it here exactly like the
                    # other IF NOT EXISTS migrations above.
                    try:
                        dlq_cols = [c["name"] for c in inspector.get_columns("dlq")]
                    except Exception:
                        dlq_cols = []
                    if dlq_cols:
                        with self._engine.begin() as conn:
                            conn.execute(
                                text("CREATE UNIQUE INDEX IF NOT EXISTS uq_dlq_task ON dlq (task_id)")
                            )
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
                    task_cols = [c["name"] for c in inspector.get_columns("tasks")]
                    if task_cols and "next_attempt_at" not in task_cols:
                        # C-07: mirror the SQLite migration with Postgres-idiomatic
                        # types (TIMESTAMP WITH TIME ZONE) + the retry index so
                        # claim_pending_tasks' next_attempt_at gate stays usable.
                        with self._engine.begin() as conn:
                            conn.execute(
                                text("ALTER TABLE tasks ADD COLUMN next_attempt_at TIMESTAMP WITH TIME ZONE")
                            )
                            conn.execute(
                                text(
                                    "CREATE INDEX IF NOT EXISTS ix_tasks_next_attempt_at "
                                    "ON tasks (next_attempt_at)"
                                )
                            )
                    # C-07: dedup_near.sig_hex must exist on Postgres too — without
                    # it _verify_dedup_schema() raises and legacy Postgres DBs fail
                    # init permanently.
                    near_cols = [c["name"] for c in inspector.get_columns("dedup_near")]
                    if near_cols and "sig_hex" not in near_cols:
                        with self._engine.begin() as conn:
                            conn.execute(text("ALTER TABLE dedup_near ADD COLUMN sig_hex VARCHAR"))
                    # W19: mirror the SQLite branch — the token-set sketch columns
                    # must exist on Postgres too or the content-diversity guard's
                    # SELECT fails on legacy DBs.
                    if near_cols and "token_hash" not in near_cols:
                        with self._engine.begin() as conn:
                            conn.execute(text("ALTER TABLE dedup_near ADD COLUMN token_hash BIGINT"))
                    if near_cols and "token_count" not in near_cols:
                        with self._engine.begin() as conn:
                            conn.execute(text("ALTER TABLE dedup_near ADD COLUMN token_count INTEGER"))
                    # W6C-bug1: mirror the SQLite branch — legacy Postgres DLQs
                    # must carry the uq_dlq_task unique index for add_dlq()'s
                    # ON CONFLICT (task_id) DO NOTHING to dedupe concurrent
                    # dead-letter inserts from multi-process reapers.
                    try:
                        dlq_cols = [c["name"] for c in inspector.get_columns("dlq")]
                    except Exception:
                        dlq_cols = []
                    if dlq_cols:
                        with self._engine.begin() as conn:
                            conn.execute(
                                text("CREATE UNIQUE INDEX IF NOT EXISTS uq_dlq_task ON dlq (task_id)")
                            )
                except Exception as e:
                    logger.warning("migration_failed", error=str(e))

            from sqlalchemy import inspect as _sa_inspect

            _verify_dedup_schema(_sa_inspect(self._engine))

            # H-24/M-23: startup detection for legacy dedup_near layouts. We must
            # never silently mix band widths or compare half-width signatures —
            # both silently corrupt near-dup recall. Warn loudly and require a
            # rebuild of the dedup index.
            try:
                from sqlalchemy.orm import Session as _Session  # noqa: F401

                with self._sessionmaker() as s:
                    near_total = int(s.scalar(select(func.count(DedupNearRow.id))) or 0)
                    if near_total:
                        max_seg = int(s.scalar(select(func.max(DedupNearRow.seg_value))) or -1)
                        if max_seg >= 0 and max_seg <= _LEGACY_4BIT_MAX_SEG_VALUE:
                            logger.warning(
                                "dedup_near_legacy_4bit_layout_detected",
                                rows=near_total,
                                hint=(
                                    "dedup_near was written with the old 32x4-bit "
                                    "band layout (NEAR_DUP_SEG_BITS=4); band values "
                                    "are all <16. Near-dup recall is wrong at the "
                                    "current 8-bit band width. Rebuild the dedup "
                                    "index: `DELETE FROM dedup_near;` then re-run "
                                    "ingestion (no `awareness dedup reindex` "
                                    "command exists)."
                                ),
                            )
                        null_sig = int(
                            s.scalar(
                                select(func.count(DedupNearRow.id)).where(DedupNearRow.sig_hex.is_(None))
                            )
                            or 0
                        )
                        if null_sig:
                            logger.warning(
                                "dedup_near_legacy_null_sig_rows_detected",
                                rows=null_sig,
                                hint=(
                                    "dedup_near rows with NULL sig_hex are legacy "
                                    "64-bit index rows; they are compared with "
                                    "hamming64 and should be re-indexed: "
                                    "`DELETE FROM dedup_near;` then re-run "
                                    "ingestion."
                                ),
                            )
            except Exception as e:
                logger.warning("dedup_near_layout_detection_failed", error=str(e))

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
        """Read-then-insert task addition with bounded conflict retry (NEW-2).

        The (job_id, partition_key) unique constraint means two concurrent
        ``add_tasks`` callers can both read "no row" then both insert; the
        loser hits an IntegrityError at commit. Per the docstring intent
        ("silently RESET the existing row instead of crashing"), the whole
        batch is retried — all-or-nothing per attempt — and the retry naturally
        takes the re-arm path once the winner's row is visible.
        """
        from sqlalchemy.exc import IntegrityError

        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                return self._do_add_tasks_once(materialized)
            except IntegrityError as exc:
                last_exc = exc
                logger.warning(
                    "add_tasks_integrity_conflict_retrying",
                    attempt=attempt + 1,
                    job_id=materialized[0].job_id,
                )
        raise RuntimeError(
            f"add_tasks could not commit after 3 attempts for job "
            f"{materialized[0].job_id!r} (concurrent writer conflicts)"
        ) from last_exc

    def _do_add_tasks_once(self, materialized: list[TaskState]) -> int:
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
                    # H-05: never re-arm a row a worker is currently executing —
                    # that would duplicate execution and double-count job counters.
                    if existing.status == TaskStatus.RUNNING.value:
                        continue
                    # Re-arm: reset status, remember the previous one.
                    prev = existing.status
                    rearmed_from[prev] = rearmed_from.get(prev, 0) + 1
                    existing.status = TaskStatus.PENDING.value
                    existing.started_at = None
                    existing.completed_at = None
                    existing.last_error = None
                    # H-05: a re-armed task must not inherit a stale backoff lease.
                    existing.next_attempt_at = None
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
            now = _utcnow()
            # Candidate selection. For non-SQLite, skip-locked keeps concurrent
            # claimers from racing on the same pending rows.
            id_stmt = (
                select(TaskRow.task_id)
                .where(
                    TaskRow.job_id == job_id,
                    TaskRow.status == TaskStatus.PENDING.value,
                    (TaskRow.next_attempt_at.is_(None)) | (TaskRow.next_attempt_at <= now),
                )
                .order_by(TaskRow.created_at)
                .limit(limit)
            )
            if self._engine.dialect.name != "sqlite":
                id_stmt = id_stmt.with_for_update(skip_locked=True)
            candidates = list(s.scalars(id_stmt))
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
                list(
                    s.scalars(
                        select(TaskRow)
                        .where(TaskRow.task_id.in_(claimed_ids))
                        .execution_options(populate_existing=True)
                    )
                )
                if claimed_ids
                else []
            )
            rows.sort(key=lambda r: (r.created_at, r.task_id))
            return [self._task_state_from_row(r) for r in rows]

    def requeue_orphaned_running(self, job_id: str, *, older_than_seconds: float, max_retries: int) -> int:
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
                    # M-07: dead-lettered orphan tasks must also enter the DLQ so
                    # operators can replay them — status alone made them
                    # unrecoverable before.
                    try:
                        self.add_dlq(
                            r.job_id,
                            r.task_id,
                            json.loads(r.payload_json or "{}"),
                            "orphaned_running_exceeded_max_retries",
                        )
                    except Exception as exc:
                        logger.warning("orphan_dlq_write_failed", task_id=r.task_id, error=str(exc))
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
                select(TaskRow.status, func.count()).where(TaskRow.job_id == job_id).group_by(TaskRow.status)
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

    def count_retry_scheduled(self, job_id: str) -> int:
        """Count PENDING tasks waiting on a future next_attempt_at backoff."""
        with self.session() as s:
            now = _utcnow()
            n = s.scalar(
                select(func.count())
                .select_from(TaskRow)
                .where(
                    TaskRow.job_id == job_id,
                    TaskRow.status == TaskStatus.PENDING.value,
                    TaskRow.next_attempt_at.is_not(None),
                    TaskRow.next_attempt_at > now,
                )
            )
            return int(n or 0)

    def list_retry_scheduled_tasks(self, job_id: str, limit: int = 12) -> list[dict[str, Any]]:
        """PENDING tasks whose retry backoff has not elapsed yet (newest lease first)."""
        with self.session() as s:
            now = _utcnow()
            stmt = (
                select(TaskRow)
                .where(
                    TaskRow.job_id == job_id,
                    TaskRow.status == TaskStatus.PENDING.value,
                    TaskRow.next_attempt_at.is_not(None),
                    TaskRow.next_attempt_at > now,
                )
                .order_by(TaskRow.next_attempt_at.asc())
                .limit(limit)
            )
            return [
                {
                    "task_id": r.task_id,
                    "source_type": r.source_type,
                    "partition_key": r.partition_key,
                    "attempts": r.attempts,
                    "next_attempt_at": r.next_attempt_at.isoformat() if r.next_attempt_at else None,
                    "last_error": r.last_error,
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
                select(TaskRow.partition_key, TaskRow.status).where(
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
            stmt = select(ManifestRow).order_by(ManifestRow.id.desc()).limit(limit)
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
        """Insert a new content_hash if absent, else bump capture_count.

        Returns ``(canonical_doc_id, was_new)``. ``was_new`` is only ever True
        for a row this call actually INSERTED — on a concurrent-writer
        IntegrityError the insert is retried (bounded) instead of returning
        ``was_new=True`` for an unpersisted row (M-20), which would make the
        dedup engine skip near-dup indexing of a brand-new doc.
        """
        from sqlalchemy.exc import IntegrityError

        for _attempt in range(3):
            with self.session() as s:
                row = s.get(DedupRow, content_hash)
                if row is None:
                    try:
                        s.add(DedupRow(content_hash=content_hash, first_doc_id=doc_id))
                        s.commit()
                        return doc_id, True
                    except IntegrityError:
                        s.rollback()
                        # Another worker won the race for this content_hash;
                        # loop and re-read instead of claiming was_new.
                        continue
                row.capture_count += 1
                s.commit()
                return row.first_doc_id, False
        # Exhausted retries (persistent write conflict). Never claim was_new for
        # a row we could not persist — surface the conflict loudly instead of
        # silently dropping the doc's near-dup band index.
        with self.session() as s:
            row = s.get(DedupRow, content_hash)
            if row is not None:
                row.capture_count += 1
                s.commit()
                return row.first_doc_id, False
        raise RuntimeError(
            f"upsert_dedup could not persist content_hash {content_hash!r} after "
            "3 attempts (concurrent writer conflict) — refusing to report was_new "
            "for an unpersisted row"
        )

    def add_near_dup_index(
        self,
        doc_id: str,
        simhash_unsigned: int,
        *,
        token_hash: int | None = None,
        token_count: int | None = None,
    ) -> None:
        """Insert band rows for a 128-bit signature (one per data band).

        All bands for the doc are upserted in ONE transaction / commit (M-21) —
        previously each band committed separately (32 commits per doc).

        ``token_hash`` / ``token_count`` are the W19 token-set sketch used by
        the content-diversity guard; None stores NULL (legacy rows, treated as
        'unknown' → merge by Hamming alone).
        """
        if simhash_unsigned <= 0:
            return
        sig_hex = sig128_to_hex(simhash_unsigned)
        # Calculate legacy 64-bit signed hash for backward compatibility / database schema constraints
        legacy_hash = simhash_unsigned & 0xFFFFFFFFFFFFFFFF
        if legacy_hash >= (1 << 63):
            legacy_hash -= 1 << 64

        bands = [
            ((simhash_unsigned >> (NEAR_DUP_SEG_BITS * seg)) & _NEAR_DUP_SEG_MASK)
            for seg in range(_NEAR_DUP_DATA_BANDS)
        ]

        from sqlalchemy.exc import IntegrityError

        for _attempt in range(3):
            with self.session() as s:
                try:
                    self._upsert_band_rows(
                        s, doc_id, sig_hex, legacy_hash, bands,
                        token_hash=token_hash, token_count=token_count,
                    )
                    s.commit()
                    return
                except IntegrityError:
                    s.rollback()
                    # Concurrent writer inserted some bands mid-flight; the
                    # per-(doc_id, seg) unique constraint means re-running the
                    # upsert is safe — retry bounded.
                    continue
        # Persistent conflict (3 tries) — last resort: merge rows one commit.
        with self.session() as s:
            self._upsert_band_rows(
                s, doc_id, sig_hex, legacy_hash, bands, ignore_existing=True,
                token_hash=token_hash, token_count=token_count,
            )
            s.commit()

    @staticmethod
    def _upsert_band_rows(
        s: Session,
        doc_id: str,
        sig_hex: str,
        legacy_hash: int,
        bands: list[int],
        *,
        ignore_existing: bool = False,
        token_hash: int | None = None,
        token_count: int | None = None,
    ) -> None:
        """Upsert ``(seg, seg_value)`` band rows for ``doc_id`` in one pass.

        Rows are keyed by the unique ``(doc_id, seg)`` constraint; existing rows
        are updated in place. When ``ignore_existing`` is True (retry path after
        IntegrityError), rows that already exist are refreshed instead of
        re-inserted.
        """
        existing = {
            row.seg: row
            for row in s.execute(select(DedupNearRow).where(DedupNearRow.doc_id == doc_id)).scalars()
        }
        for seg, value in enumerate(bands):
            row = existing.get(seg)
            if row is None:
                if ignore_existing:
                    continue
                s.add(
                    DedupNearRow(
                        doc_id=doc_id,
                        sig_hex=sig_hex,
                        near_dup_hash=legacy_hash,
                        token_hash=token_hash,
                        token_count=token_count,
                        seg=seg,
                        seg_value=value,
                    )
                )
            else:
                row.sig_hex = sig_hex
                row.near_dup_hash = legacy_hash
                row.token_hash = token_hash
                row.token_count = token_count
                row.seg_value = value

    def find_near_dup_candidates(self, simhash_unsigned: int) -> list[tuple[str, int]]:
        """Look up doc_ids that share at least one band with this 128-bit signature.

        Returns ``(doc_id, signature_int)`` pairs. Rows written by the legacy
        64-bit index (``sig_hex`` NULL) fall back to their stored int — the
        caller must compare those with :func:`~awareness.util.hashing.hamming64`
        against the query's low 64 bits (M-23). Garbage ``sig_hex`` values
        decode to ``None`` and are skipped (L-04).
        """
        return [
            (did, sig)
            for did, sig, _token_hash, _token_count in self.find_near_dup_candidate_rows(
                simhash_unsigned
            )
        ]

    def find_near_dup_candidate_rows(
        self, simhash_unsigned: int
    ) -> list[tuple[str, int, int | None, int | None]]:
        """Band lookup that also returns the stored W19 token-set sketch.

        Returns ``(doc_id, signature_int, token_hash, token_count)``. The
        sketch columns are NULL for legacy/unknown rows — the engine's
        content-diversity guard treats those as 'unknown' and allows the
        merge by Hamming distance alone.
        """
        out: dict[str, tuple[int, int | None, int | None]] = {}
        with self.session() as s:
            for seg in range(_NEAR_DUP_DATA_BANDS):
                value = (simhash_unsigned >> (NEAR_DUP_SEG_BITS * seg)) & _NEAR_DUP_SEG_MASK
                stmt = (
                    select(
                        DedupNearRow.doc_id,
                        DedupNearRow.sig_hex,
                        DedupNearRow.near_dup_hash,
                        DedupNearRow.token_hash,
                        DedupNearRow.token_count,
                    )
                    .where(DedupNearRow.seg == seg, DedupNearRow.seg_value == value)
                    .limit(NEAR_DUP_CANDIDATE_LIMIT)
                )
                for did, sig_hex, legacy, token_hash, token_count in s.execute(stmt).all():
                    if sig_hex:
                        parsed = sig128_from_hex(sig_hex)
                        if parsed is not None:
                            out[did] = (parsed, token_hash, token_count)
                    elif legacy is not None:
                        out[did] = (legacy & 0xFFFFFFFFFFFFFFFF, token_hash, token_count)
        return [(did, sig, th, tc) for did, (sig, th, tc) in out.items()]

    def uf_find(self, doc_id: str) -> str:
        """Find the canonical root of ``doc_id``'s near-dup cluster.

        Walks parent links in ``dup_parent``. Read-only (M-22): a missing row
        means the doc is its own root — no self-root row is inserted and no
        path-compression writes happen here, so ``uf_find`` can never mutate
        state or commit. (``uf_union`` still creates rows when linking NEW
        docs into clusters.)
        """
        if not doc_id:
            return doc_id
        with self.session() as s:
            seen: list[str] = []
            cur = doc_id
            # Cap walk length against accidental cycles in corrupted state.
            for _ in range(256):
                row = s.get(DupParentRow, cur)
                if row is None or row.parent_id == cur:
                    return cur
                if cur in seen:
                    # Cycle — treat current as root and break.
                    return cur
                seen.append(cur)
                cur = row.parent_id
            return cur

    def uf_union(self, a: str, b: str) -> str:
        """Link ``a`` under :meth:`uf_find` of ``b``; return the resulting root.

        Self-union (``a == b``) registers ``a`` as its own root. When ``a``
        already heads a cluster, that whole tree is folded under ``find(b)`` so
        near-dup links remain transitive.
        """
        if not a:
            return self.uf_find(b) if b else a
        if not b or a == b:
            with self.session() as s:
                row = s.get(DupParentRow, a)
                if row is None:
                    s.add(DupParentRow(doc_id=a, parent_id=a))
                    s.commit()
            return self.uf_find(a)

        root = self.uf_find(b)
        # Fold a's existing group into root (union of two trees).
        existing_root = self.uf_find(a)
        if existing_root != root:
            with self.session() as s:
                old = s.get(DupParentRow, existing_root)
                if old is None:
                    s.add(DupParentRow(doc_id=existing_root, parent_id=root))
                else:
                    old.parent_id = root
                # Also point a directly at root (path compression).
                a_row = s.get(DupParentRow, a)
                if a_row is None:
                    s.add(DupParentRow(doc_id=a, parent_id=root))
                else:
                    a_row.parent_id = root
                root_row = s.get(DupParentRow, root)
                if root_row is None:
                    s.add(DupParentRow(doc_id=root, parent_id=root))
                elif root_row.parent_id != root:
                    root_row.parent_id = root
                s.commit()
        return root

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
                {
                    "id": r.id,
                    "path": r.path,
                    "records": r.records,
                    "bytes": r.bytes,
                    "committed_at": r.committed_at.isoformat() if r.committed_at else None,
                }
                for r in s.scalars(stmt)
            ]

    def pending_manifest_summary(self) -> dict[str, Any]:
        """Aggregate counts for staging manifests not yet compacted into Iceberg.

        Used by ``awareness compact --status`` so operators can see backlog size
        without starting a compaction pass. Includes oldest commit age so lagging
        compaction is visible without scanning the manifests list client-side.
        """
        pending = self.list_pending_manifests()
        total_records = sum(int(m.get("records") or 0) for m in pending)
        total_bytes = sum(int(m.get("bytes") or 0) for m in pending)
        oldest_committed_at: str | None = None
        oldest_age_seconds: float | None = None
        oldest_dt: datetime | None = None
        for m in pending:
            raw = m.get("committed_at")
            if not raw:
                continue
            try:
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            else:
                dt = dt.astimezone(UTC)
            if oldest_dt is None or dt < oldest_dt:
                oldest_dt = dt
                oldest_committed_at = dt.isoformat()
        if oldest_dt is not None:
            oldest_age_seconds = max(0.0, (_utcnow() - oldest_dt).total_seconds())
        return {
            "pending_count": len(pending),
            "total_records": total_records,
            "total_bytes": total_bytes,
            "oldest_committed_at": oldest_committed_at,
            "oldest_age_seconds": (round(oldest_age_seconds, 1) if oldest_age_seconds is not None else None),
            "manifests": pending,
        }

    def mark_manifest_compacted(self, manifest_id: int) -> None:
        with self.session() as s:
            row = s.get(ManifestRow, manifest_id)
            if row is None:
                return
            row.compacted_at = _utcnow()
            s.commit()

    # ── DLQ ──────────────────────────────────────────────────────────────
    def add_dlq(self, job_id: str | None, task_id: str | None, payload: dict[str, Any], error: str) -> None:
        """Insert a dead-letter row, deduplicated by ``task_id`` (W6C-bug1).

        The ``dlq`` table carries a UNIQUE index on task_id (``uq_dlq_task``,
        created by :meth:`init`); the insert is conflict-tolerant so two
        processes reaping the same orphaned task concurrently (the dead-letter
        branch of :meth:`requeue_orphaned_running` on a multi-process Postgres
        deployment) cannot produce duplicate DLQ rows. Rows with a NULL
        task_id are unaffected (NULLs never collide in a UNIQUE index).
        """
        from sqlalchemy import text  # noqa: PLC0415

        with self.session() as s:
            params = {
                "job_id": job_id,
                "task_id": task_id,
                "payload_json": json.dumps(payload),
                "error": error[:4000],
                "created_at": _utcnow(),
            }
            if self._engine.dialect.name == "sqlite":
                stmt = text(
                    "INSERT OR IGNORE INTO dlq "
                    "(job_id, task_id, payload_json, error, created_at) "
                    "VALUES (:job_id, :task_id, :payload_json, :error, :created_at)"
                )
            else:
                stmt = text(
                    "INSERT INTO dlq "
                    "(job_id, task_id, payload_json, error, created_at) "
                    "VALUES (:job_id, :task_id, :payload_json, :error, :created_at) "
                    "ON CONFLICT (task_id) DO NOTHING"
                )
            s.execute(stmt, params)
            s.commit()

    def count_dlq(self, *, job_id: str | None = None) -> int:
        """Return the number of dead-letter rows (optionally filtered by job)."""
        with self.session() as s:
            q = select(func.count(DLQRow.id))
            if job_id:
                q = q.where(DLQRow.job_id == job_id)
            return int(s.scalar(q) or 0)

    def list_dlq(
        self,
        *,
        limit: int = 50,
        job_id: str | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List dead-letter queue entries newest-first.

        Each item is a plain dict suitable for CLI/JSON export::

            {
              "id": int,
              "job_id": str | None,
              "task_id": str | None,
              "error": str,
              "payload": dict,   # parsed JSON ({} if corrupt)
              "created_at": str | None,  # ISO-8601 UTC
            }
        """
        limit = max(1, min(int(limit), 1000))
        offset = max(0, int(offset))
        with self.session() as s:
            q = select(DLQRow).order_by(DLQRow.created_at.desc(), DLQRow.id.desc())
            if job_id:
                q = q.where(DLQRow.job_id == job_id)
            q = q.offset(offset).limit(limit)
            rows = list(s.scalars(q).all())
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row.payload_json or "{}")
                if not isinstance(payload, dict):
                    payload = {"_raw": payload}
            except (json.JSONDecodeError, TypeError, ValueError):
                payload = {"_raw": row.payload_json}
            created = row.created_at
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            out.append(
                {
                    "id": int(row.id),
                    "job_id": row.job_id,
                    "task_id": row.task_id,
                    "error": row.error or "",
                    "payload": payload,
                    "created_at": created.isoformat() if created is not None else None,
                }
            )
        return out

    def get_dlq(self, dlq_id: int) -> dict[str, Any] | None:
        """Return one DLQ row by id, or ``None`` if missing."""
        with self.session() as s:
            row = s.get(DLQRow, int(dlq_id))
            if row is None:
                return None
            try:
                payload = json.loads(row.payload_json or "{}")
                if not isinstance(payload, dict):
                    payload = {"_raw": payload}
            except (json.JSONDecodeError, TypeError, ValueError):
                payload = {"_raw": row.payload_json}
            created = row.created_at
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            return {
                "id": int(row.id),
                "job_id": row.job_id,
                "task_id": row.task_id,
                "error": row.error or "",
                "payload": payload,
                "created_at": created.isoformat() if created is not None else None,
            }

    def replay_dlq(
        self,
        dlq_id: int,
        *,
        reset_attempts: bool = True,
    ) -> dict[str, Any]:
        """Re-arm a dead-lettered task and remove its DLQ row.

        Looks up the DLQ entry by *dlq_id*, finds the associated task, resets it
        to ``PENDING`` (optionally clearing ``attempts`` so max-retries can fire
        again), decrements the job's dead-letter counter when the task was
        ``DEAD_LETTERED``, and deletes the DLQ row.

        Returns a result dict::

            {"ok": True, "dlq_id": int, "task_id": str, "job_id": str,
             "previous_status": str, "attempts": int}
            {"ok": False, "reason": "dlq_missing"|"task_missing"|"no_task_id", ...}

        Does not create new tasks when the task row is gone — operators must
        re-plan or reseed for those cases.
        """
        dlq_id = int(dlq_id)
        with self.session() as s:
            dlq = s.get(DLQRow, dlq_id)
            if dlq is None:
                return {"ok": False, "reason": "dlq_missing", "dlq_id": dlq_id}
            task_id = dlq.task_id
            job_id = dlq.job_id
            if not task_id:
                return {
                    "ok": False,
                    "reason": "no_task_id",
                    "dlq_id": dlq_id,
                    "job_id": job_id,
                }
            task = s.get(TaskRow, task_id)
            if task is None:
                return {
                    "ok": False,
                    "reason": "task_missing",
                    "dlq_id": dlq_id,
                    "task_id": task_id,
                    "job_id": job_id,
                }
            prev_status = task.status
            task.status = TaskStatus.PENDING.value
            task.started_at = None
            task.completed_at = None
            task.next_attempt_at = None
            task.last_error = None
            if reset_attempts:
                task.attempts = 0
            if prev_status == TaskStatus.DEAD_LETTERED.value and task.job_id:
                # Keep job dead-letter counter consistent with re-arm (mirror
                # add_tasks rearmed_from DEAD_LETTERED path).
                job = s.get(JobRow, task.job_id)
                if job is not None and (job.tasks_dead_lettered or 0) > 0:
                    job.tasks_dead_lettered = int(job.tasks_dead_lettered) - 1
            s.delete(dlq)
            s.commit()
            return {
                "ok": True,
                "dlq_id": dlq_id,
                "task_id": task_id,
                "job_id": task.job_id,
                "previous_status": prev_status,
                "attempts": int(task.attempts),
                "reset_attempts": bool(reset_attempts),
            }

    def purge_dlq(self, dlq_id: int) -> dict[str, Any]:
        """Drop a DLQ row without re-arming the task.

        Use when an operator has already handled the failure (or decided to
        abandon the task) and only wants to clear the dead-letter queue entry.
        Task status and job ``tasks_dead_lettered`` counters are left untouched.

        Returns::

            {"ok": True, "dlq_id": int, "task_id": str|None, "job_id": str|None,
             "error": str}
            {"ok": False, "reason": "dlq_missing", "dlq_id": int}
        """
        dlq_id = int(dlq_id)
        with self.session() as s:
            dlq = s.get(DLQRow, dlq_id)
            if dlq is None:
                return {"ok": False, "reason": "dlq_missing", "dlq_id": dlq_id}
            result = {
                "ok": True,
                "dlq_id": dlq_id,
                "task_id": dlq.task_id,
                "job_id": dlq.job_id,
                "error": dlq.error or "",
            }
            s.delete(dlq)
            s.commit()
            return result

    def purge_dlq_bulk(
        self,
        *,
        job_id: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Drop many DLQ rows without re-arming tasks.

        Optionally filter by *job_id*. *limit* caps how many rows are deleted
        (newest-first by ``created_at`` / id) so operators can drain a large
        queue in batches. Task status and job ``tasks_dead_lettered`` counters
        are left untouched (same contract as :meth:`purge_dlq`).

        Returns::

            {"ok": True, "purged": int, "job_id": str|None, "limit": int|None,
             "remaining": int}
        """
        jid = (job_id or "").strip() or None
        cap = None if limit is None else max(0, int(limit))
        with self.session() as s:
            q = select(DLQRow).order_by(DLQRow.created_at.desc(), DLQRow.id.desc())
            if jid:
                q = q.where(DLQRow.job_id == jid)
            if cap is not None:
                q = q.limit(cap)
            rows = list(s.scalars(q).all())
            purged = 0
            for row in rows:
                s.delete(row)
                purged += 1
            if purged:
                s.commit()
            remaining_q = select(func.count(DLQRow.id))
            if jid:
                remaining_q = remaining_q.where(DLQRow.job_id == jid)
            remaining = int(s.scalar(remaining_q) or 0)
        return {
            "ok": True,
            "purged": purged,
            "job_id": jid,
            "limit": cap,
            "remaining": remaining,
        }

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
                "reconciled-stale-no-pid" if observed_pid is None else f"reconciled-dead-pid:{observed_pid}"
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
            except Exception as exc:
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
                    TaskRow.status.in_([TaskStatus.PENDING.value, TaskStatus.RUNNING.value]),
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
                abandoned = self.abandon_inflight_tasks(job.job_id, note="stale-tasks-on-terminal-job")
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

    def set_robots_cache(
        self, site: str, robots_txt: str | None, expires_at: float, crawl_delay: float | None
    ) -> None:
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
