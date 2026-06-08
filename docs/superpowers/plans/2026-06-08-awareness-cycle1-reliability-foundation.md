# Awareness Cycle 1 — Plan 1: Reliability Foundation (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the job/state core crash-safe and concurrency-correct: no double-claimed tasks, no lost work on crash/stop, no false "completed", and resilient SQLite concurrency — the foundation every later cycle-1 plan builds on.

**Architecture:** Single-process asyncio worker drains a SQLite-backed task queue (`StateDB`). Fixes are concentrated in `storage/state.py` (claim/retry/reaper + SQLite pragmas), `workers/engine.py` (don't mark COMPLETED on stop; reap orphans on start), `tail/engine.py` (import fix), and `schemas/jobs.py` is read-only here. No new infrastructure.

**Tech Stack:** Python 3.13, SQLAlchemy 2.x (sync) over SQLite, asyncio, pytest (`asyncio_mode=auto`).

**Standard test command:** `PYTHONPATH=src .venv/bin/python -m pytest -q -p no:cacheprovider`
**Baseline at branch start:** 193 passing (`-m "not slow and not smoke"`), 0 failures.

**Scope source:** `docs/superpowers/specs/2026-06-08-awareness-make-it-work-design.md` workstream **C** + the SQLite-concurrency blind-spot. Audit detail: `docs/superpowers/audit/2026-06-08-awareness-audit.json`.

**Co-landing constraint:** Tasks 4 and 5 must both land before this plan is considered done (a reaper without the COMPLETED-on-stop fix, or vice versa, is a half-fix). Implement them back-to-back.

---

### Task 1: SQLite WAL + busy_timeout pragmas on every connection

**Why:** Cross-process workers (standalone `awareness-worker` + API + CLI) hit the same SQLite file. Default rollback-journal + no busy timeout causes "database is locked" and enables the read-then-write races behind double-claim. WAL + a busy timeout is the foundation for the atomic claim in Task 3.

**Files:**
- Modify: `src/awareness/storage/state.py` (imports near line 18-28; `StateDB.__init__` near lines 180-188)
- Test: `tests/unit/test_state_concurrency.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_state_concurrency.py`:

```python
from __future__ import annotations

from sqlalchemy import text

from awareness.storage.state import StateDB


def test_sqlite_uses_wal_and_busy_timeout(tmp_path) -> None:
    state = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    state.init()
    with state.session() as s:
        journal_mode = s.execute(text("PRAGMA journal_mode")).scalar()
        busy_timeout = s.execute(text("PRAGMA busy_timeout")).scalar()
    assert str(journal_mode).lower() == "wal"
    assert int(busy_timeout) == 5000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_state_concurrency.py -q`
Expected: FAIL — `journal_mode` is `delete` (or `memory`), not `wal`.

- [ ] **Step 3: Implement the pragma listener**

In `src/awareness/storage/state.py`, add `event` to the SQLAlchemy import block (the `from sqlalchemy import (...)` list near line 18):

```python
from sqlalchemy import (
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    event,
    func,
    select,
    update,
)
```

Then in `StateDB.__init__`, immediately after `self._engine = create_engine(url, future=True)`:

```python
        self._engine = create_engine(url, future=True)
        if url.startswith("sqlite"):
            # WAL + a busy timeout make concurrent readers/writers across
            # processes (standalone worker + API + CLI) coexist without
            # "database is locked", and underpin the atomic claim in
            # claim_pending_tasks. synchronous=NORMAL is the safe WAL pairing.
            @event.listens_for(self._engine, "connect")
            def _set_sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=5000")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_state_concurrency.py -q`
Expected: PASS

- [ ] **Step 5: Run the full suite to confirm no regression**

Run: `PYTHONPATH=src .venv/bin/python -m pytest -q -m "not slow and not smoke"`
Expected: PASS (194 passing).

- [ ] **Step 6: Commit**

```bash
git add src/awareness/storage/state.py tests/unit/test_state_concurrency.py
git commit -m "fix(state): enable SQLite WAL + busy_timeout for safe concurrent access"
```

---

### Task 2: Fix `JobStatus` NameError on the tail resume path

**Why:** `tail/engine.py:108` calls `self._state.set_job_status(job_id, JobStatus.RUNNING)` on the `--job-id` resume branch, but line 29 only imports `TaskState`. Any resume crashes with `NameError`. (Audit: `bug:tail-engine-jobstatus-nameerror`, high.)

**Files:**
- Modify: `src/awareness/tail/engine.py:29`
- Test: `tests/unit/test_tail_resume.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_tail_resume.py`:

```python
from __future__ import annotations

from awareness.planner.planner import Planner
from awareness.schemas.jobs import JobKind, JobState, JobStatus
from awareness.storage.state import StateDB
from awareness.tail.engine import TailEngine


async def test_tail_resume_sets_running_without_nameerror(tmp_project) -> None:
    state = StateDB(f"sqlite:///{tmp_project / 'state.db'}")
    state.init()
    planner = Planner(state)
    job_id = "tail-resume-1"
    state.create_job(JobState(job_id=job_id, kind=JobKind.TAIL, request={}))

    engine = TailEngine(state, planner)
    # Resume path (explicit job_id): previously raised NameError: JobStatus.
    await engine.start(job_id=job_id, gdelt=False)
    try:
        assert state.get_job(job_id).status == JobStatus.RUNNING
    finally:
        await engine.stop(drain_seconds=2.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_tail_resume.py -q`
Expected: FAIL — `NameError: name 'JobStatus' is not defined`.

- [ ] **Step 3: Add the import**

In `src/awareness/tail/engine.py`, change line 29 from:

```python
from awareness.schemas.jobs import TaskState
```

to:

```python
from awareness.schemas.jobs import JobStatus, TaskState
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_tail_resume.py -q`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=src .venv/bin/python -m pytest -q -m "not slow and not smoke"`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/awareness/tail/engine.py tests/unit/test_tail_resume.py
git commit -m "fix(tail): import JobStatus so --job-id resume no longer crashes"
```

---

### Task 3: Atomic `claim_pending_tasks` (no cross-process double-claim)

**Why:** The current claim does `SELECT ... WHERE status='pending'` then a Python loop that sets `status='running'`, committed at the end — no row locking, no atomic conditional update. Two processes both see the same PENDING rows and both claim them. (Audit: `bug:nonatomic-claim-pending-tasks`, high. The robust fix is DB-level, not the in-process lock.)

**Files:**
- Modify: `src/awareness/storage/state.py` (`claim_pending_tasks`, lines 420-436)
- Test: `tests/unit/test_state_claim.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_state_claim.py`:

```python
from __future__ import annotations

from awareness.schemas.doc import SourceKind
from awareness.schemas.jobs import JobKind, JobState, TaskState, TaskStatus
from awareness.storage.state import StateDB


def _state_with_tasks(tmp_path, n: int) -> StateDB:
    state = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    state.init()
    state.create_job(JobState(job_id="j1", kind=JobKind.BACKFILL, request={}))
    state.add_tasks(
        [
            TaskState(
                task_id=f"t{i}",
                job_id="j1",
                source_type=SourceKind.RSS,
                partition_key=f"rss:p{i}",
                payload={},
            )
            for i in range(n)
        ]
    )
    return state


def test_claims_are_disjoint_and_mark_running(tmp_path) -> None:
    state = _state_with_tasks(tmp_path, 5)
    a = state.claim_pending_tasks("j1", limit=3)
    b = state.claim_pending_tasks("j1", limit=3)
    ids_a = {t.task_id for t in a}
    ids_b = {t.task_id for t in b}
    assert len(ids_a) == 3
    assert len(ids_b) == 2
    assert ids_a.isdisjoint(ids_b)
    for t in a:
        assert t.status == TaskStatus.RUNNING
        assert t.attempts == 1
    # Corpus drained: a third claim sees nothing.
    assert state.claim_pending_tasks("j1", limit=10) == []


def test_claim_does_not_reclaim_running(tmp_path) -> None:
    state = _state_with_tasks(tmp_path, 2)
    first = state.claim_pending_tasks("j1", limit=2)
    assert len(first) == 2
    # All RUNNING now; re-claim returns nothing (no double-claim).
    assert state.claim_pending_tasks("j1", limit=2) == []
```

- [ ] **Step 2: Run test to verify it fails or is brittle**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_state_claim.py -q`
Expected: The disjoint/running assertions may pass on the old code in-process, but proceed — Step 3 makes the claim atomic and the suite must stay green. (The real cross-process guarantee is exercised by the atomic SQL, not unit-testable in one process.)

- [ ] **Step 3: Replace `claim_pending_tasks` with an atomic conditional update**

In `src/awareness/storage/state.py`, replace the whole `claim_pending_tasks` method (lines 420-436) with:

```python
    def claim_pending_tasks(self, job_id: str, limit: int) -> list[TaskState]:
        """Atomically transition up to ``limit`` PENDING tasks to RUNNING.

        The claim is a single conditional ``UPDATE ... WHERE status='pending'
        RETURNING`` over a bounded candidate set. Because the status guard is
        evaluated inside the write-locked UPDATE, two concurrent claimers (even
        across processes) can never both win the same row — the loser's guard no
        longer matches. ``self._lock`` serializes in-process callers; WAL +
        busy_timeout (see __init__) keep cross-process writers from erroring.
        """
        now = _utcnow()
        with self._lock, self.session() as s:
            candidates = list(
                s.scalars(
                    select(TaskRow.task_id)
                    .where(
                        TaskRow.job_id == job_id,
                        TaskRow.status == TaskStatus.PENDING.value,
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
            rows = (
                list(s.scalars(select(TaskRow).where(TaskRow.task_id.in_(claimed_ids))))
                if claimed_ids
                else []
            )
            s.commit()
            rows.sort(key=lambda r: (r.created_at, r.task_id))
            return [self._task_state_from_row(r) for r in rows]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_state_claim.py -q`
Expected: PASS (both tests).

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=src .venv/bin/python -m pytest -q -m "not slow and not smoke"`
Expected: PASS. (Existing worker/tail tests still drain tasks via the new claim.)

- [ ] **Step 6: Commit**

```bash
git add src/awareness/storage/state.py tests/unit/test_state_claim.py
git commit -m "fix(state): make claim_pending_tasks an atomic conditional UPDATE (no double-claim)"
```

---

### Task 4: Orphaned-RUNNING task reaper

**Why:** If a process crashes or is stopped mid-task, the row stays `RUNNING` forever — `claim_pending_tasks` only selects `PENDING`, so the work is lost (permanent for backfill jobs). Need a reaper that requeues stale RUNNING rows. (Audit: `bug:orphaned-running-tasks-never-requeued`, high.) **Co-lands with Task 5.**

**Files:**
- Modify: `src/awareness/storage/state.py` (imports line 15; add method after `claim_pending_tasks`)
- Test: `tests/unit/test_state_reaper.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_state_reaper.py`:

```python
from __future__ import annotations

from datetime import timedelta

from awareness.schemas.doc import SourceKind
from awareness.schemas.jobs import JobKind, JobState, TaskState
from awareness.storage.state import StateDB, TaskRow, _utcnow


def _state_with_tasks(tmp_path, n: int) -> StateDB:
    state = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    state.init()
    state.create_job(JobState(job_id="j1", kind=JobKind.BACKFILL, request={}))
    state.add_tasks(
        [
            TaskState(
                task_id=f"t{i}",
                job_id="j1",
                source_type=SourceKind.RSS,
                partition_key=f"rss:p{i}",
                payload={},
            )
            for i in range(n)
        ]
    )
    return state


def test_reaper_requeues_only_stale_running(tmp_path) -> None:
    state = _state_with_tasks(tmp_path, 2)
    claimed = state.claim_pending_tasks("j1", limit=2)  # both RUNNING, started_at≈now
    # Backdate one task's started_at so it looks orphaned.
    with state.session() as s:
        r = s.get(TaskRow, claimed[0].task_id)
        r.started_at = _utcnow() - timedelta(seconds=10_000)
        s.commit()
    requeued = state.requeue_orphaned_running("j1", older_than_seconds=900, max_retries=3)
    assert requeued == 1
    counts = state.task_status_counts("j1")
    assert counts.get("pending") == 1  # the stale one came back
    assert counts.get("running") == 1  # the fresh one stayed RUNNING
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_state_reaper.py -q`
Expected: FAIL — `AttributeError: 'StateDB' object has no attribute 'requeue_orphaned_running'`.

- [ ] **Step 3: Add `timedelta` to the datetime import and implement the reaper**

In `src/awareness/storage/state.py`, change line 15 from:

```python
from datetime import UTC, datetime
```

to:

```python
from datetime import UTC, datetime, timedelta
```

Then add this method directly after `claim_pending_tasks`:

```python
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_state_reaper.py -q`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=src .venv/bin/python -m pytest -q -m "not slow and not smoke"`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/awareness/storage/state.py tests/unit/test_state_reaper.py
git commit -m "feat(state): add reaper to requeue orphaned RUNNING tasks after crash/stop"
```

---

### Task 5: `run_job` — reap on start, never mark COMPLETED on stop

**Why:** `run_job`'s `finally` block marks the job COMPLETED whenever the loop exits — including on `request_stop()` with PENDING/RUNNING tasks remaining — producing a falsely-completed, unresumable job. It must only complete when the queue was genuinely drained. It should also reap orphaned RUNNING tasks at start so a resumed/crashed job re-runs them. (Audit: `bug:premature-job-completed-on-stop`, high. **Co-lands with Task 4.**)

**Files:**
- Modify: `src/awareness/workers/engine.py` (imports near line 24; add constant + helper; `run_job` lines 144-229)
- Test: `tests/unit/test_run_job_completion.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_run_job_completion.py`:

```python
from __future__ import annotations

from awareness.planner.planner import Planner
from awareness.schemas.jobs import JobKind, JobState, JobStatus
from awareness.storage.state import StateDB
from awareness.workers.engine import WorkerEngine


def _engine(tmp_project):
    state = StateDB(f"sqlite:///{tmp_project / 'state.db'}")
    state.init()
    state.create_job(JobState(job_id="j1", kind=JobKind.BACKFILL, request={}))
    return state, WorkerEngine(state, Planner(state), concurrency=1, silent_progress=True)


def test_should_complete_logic() -> None:
    assert WorkerEngine._should_complete(drained=True, stopping=False) is True
    assert WorkerEngine._should_complete(drained=False, stopping=False) is False
    assert WorkerEngine._should_complete(drained=True, stopping=True) is False


async def test_run_job_with_zero_tasks_completes(tmp_project) -> None:
    state, engine = _engine(tmp_project)
    await engine.run_job("j1", poll_seconds=0.01)
    await engine.aclose()
    assert state.get_job("j1").status == JobStatus.COMPLETED


async def test_run_job_stopped_before_drain_does_not_complete(tmp_project) -> None:
    state, engine = _engine(tmp_project)
    engine.request_stop()  # stop before any draining
    await engine.run_job("j1", poll_seconds=0.01)
    await engine.aclose()
    # Stopped, not drained → left RUNNING (resumable), never COMPLETED.
    assert state.get_job("j1").status == JobStatus.RUNNING
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_run_job_completion.py -q`
Expected: FAIL — `_should_complete` doesn't exist; and `test_run_job_stopped...` fails because the old finally marks it COMPLETED.

- [ ] **Step 3: Add the lease constant, the helper, the start-reaper, and the drained guard**

In `src/awareness/workers/engine.py`, add a module constant after the imports (after line 38 `logger = ...`):

```python
# How long a task may sit RUNNING before a (re)started run_job treats it as
# orphaned and requeues it. Comfortably longer than any single partition fetch.
ORPHAN_LEASE_SECONDS = 900
```

Add this static method to `WorkerEngine` (e.g. right after `is_stopping`, near line 135):

```python
    @staticmethod
    def _should_complete(*, drained: bool, stopping: bool) -> bool:
        """A job is COMPLETED only when the queue genuinely drained AND we are
        not stopping. A stop leaves the job RUNNING so it can be resumed."""
        return drained and not stopping
```

In `run_job`, right after `self._state.set_job_status(job_id, JobStatus.RUNNING)` (line 146), add the start-of-run reaper:

```python
        self._state.set_job_status(job_id, JobStatus.RUNNING)
        # Recover tasks left RUNNING by a previous crash/stop before draining.
        self._state.requeue_orphaned_running(
            job_id,
            older_than_seconds=ORPHAN_LEASE_SECONDS,
            max_retries=get_settings().max_retries,
        )
```

Change the drain loop to record genuine drainage. Replace lines 196-217 (the `try: empty_polls = 0` through the `await asyncio.gather(...)` body) so the empty-poll break sets a `drained` flag:

```python
        try:
            drained = False
            empty_polls = 0
            while not self.is_stopping():
                js = self._state.get_job(job_id)
                if js:
                    if js.status in (JobStatus.CANCELLED, JobStatus.FAILED):
                        break
                    while js.status == JobStatus.PAUSED and not self.is_stopping():
                        await asyncio.sleep(1.0)
                        js = self._state.get_job(job_id)
                    if js and js.status in (JobStatus.CANCELLED, JobStatus.FAILED):
                        break

                tasks = self._state.claim_pending_tasks(job_id, limit=self._concurrency * 2)
                if not tasks:
                    empty_polls += 1
                    if empty_polls >= 3:
                        drained = True
                        break
                    await asyncio.sleep(poll_seconds)
                    continue
                empty_polls = 0
                await asyncio.gather(*(run_one(t) for t in tasks), return_exceptions=False)
                await self._flush(force=False)
```

Replace the `finally` block (lines 219-229) so completion is gated by `_should_complete`:

```python
        finally:
            if progress_bar:
                progress_bar.stop()
            await self._flush(force=True)
            job = self._state.get_job(job_id)
            if (
                job
                and job.status not in (JobStatus.CANCELLED, JobStatus.COMPLETED, JobStatus.FAILED)
                and self._should_complete(drained=drained, stopping=self.is_stopping())
            ):
                self._state.set_job_status(job_id, JobStatus.COMPLETED)
```

Note: `drained` is defined inside the `try`; it is in scope in the `finally` only if assigned before the first `await`. It is assigned (`drained = False`) as the first statement of the `try`, before any `await`, so it is always bound. Keep that ordering.

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_run_job_completion.py -q`
Expected: PASS (all three).

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=src .venv/bin/python -m pytest -q -m "not slow and not smoke"`
Expected: PASS. (Integration worker tests that drain real tasks still reach COMPLETED via the drained path.)

- [ ] **Step 6: Commit**

```bash
git add src/awareness/workers/engine.py tests/unit/test_run_job_completion.py
git commit -m "fix(workers): reap orphans on start; only mark COMPLETED when truly drained"
```

---

### Task 6: Attempt-bounded, backoff-delayed task retries

**Why:** `fail_task` currently flips a failed task straight back to `PENDING`, so it is re-claimed on the very next poll — a hot busy-retry against a failing endpoint. Add a `next_attempt_at` lease so retries back off exponentially and the claim skips not-yet-ready tasks. (Audit: `imp:backoff-on-task-retry`, `imp:reliability-backpressure`.)

**Files:**
- Modify: `src/awareness/storage/state.py` (`TaskRow` model near line 87; `init()` migration near lines 200-211; `fail_task` lines 460-477; `claim_pending_tasks` candidate filter)
- Test: `tests/unit/test_state_retry_backoff.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_state_retry_backoff.py`:

```python
from __future__ import annotations

from datetime import timedelta

from awareness.schemas.doc import SourceKind
from awareness.schemas.jobs import JobKind, JobState, TaskState
from awareness.storage.state import StateDB, TaskRow, _utcnow


def _state_with_one_task(tmp_path) -> StateDB:
    state = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    state.init()
    state.create_job(JobState(job_id="j1", kind=JobKind.BACKFILL, request={}))
    state.add_tasks(
        [TaskState(task_id="t0", job_id="j1", source_type=SourceKind.RSS, partition_key="rss:p0", payload={})]
    )
    return state


def test_failed_task_backs_off_then_becomes_claimable(tmp_path) -> None:
    state = _state_with_one_task(tmp_path)
    [t] = state.claim_pending_tasks("j1", limit=1)
    state.fail_task(t.task_id, error="boom", dead_letter=False)

    # Pending again, but the backoff lease is in the future → not yet claimable.
    assert state.claim_pending_tasks("j1", limit=1) == []

    # Backdate the lease into the past → claimable again.
    with state.session() as s:
        row = s.get(TaskRow, t.task_id)
        assert row.next_attempt_at is not None
        row.next_attempt_at = _utcnow() - timedelta(seconds=1)
        s.commit()
    again = state.claim_pending_tasks("j1", limit=1)
    assert len(again) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_state_retry_backoff.py -q`
Expected: FAIL — `TaskRow` has no `next_attempt_at`, or the failed task is immediately re-claimable.

- [ ] **Step 3: Add the column, backoff constants, migration, fail_task lease, and claim filter**

In `src/awareness/storage/state.py`:

(a) Add the column to `TaskRow` (after `checkpoint_json`, line 87):

```python
    checkpoint_json: Mapped[str] = mapped_column(String, default="{}")
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    __table_args__ = (UniqueConstraint("job_id", "partition_key", name="uq_task_part"),)
```

(b) Add backoff constants near the other module constants (after `NEAR_DUP_CANDIDATE_LIMIT`, line 140):

```python
# Exponential backoff for failed-task retries: base * 2**(attempts-1), capped.
RETRY_BACKOFF_BASE_SECONDS = 30
RETRY_BACKOFF_CAP_SECONDS = 3600


def _retry_delay_seconds(attempts: int) -> float:
    exp = max(0, attempts - 1)
    return float(min(RETRY_BACKOFF_BASE_SECONDS * (2**exp), RETRY_BACKOFF_CAP_SECONDS))
```

(c) Extend the legacy-DB migration in `init()` so old `tasks` tables gain the column. Replace the migration block (lines 200-211) with:

```python
            # Simple automatic schema migration for legacy databases.
            from sqlalchemy import inspect, text

            try:
                inspector = inspect(self._engine)
                near_cols = [c["name"] for c in inspector.get_columns("dedup_near")]
                if near_cols and "sig_hex" not in near_cols:
                    with self._engine.begin() as conn:
                        conn.execute(text("ALTER TABLE dedup_near ADD COLUMN sig_hex VARCHAR"))
                task_cols = [c["name"] for c in inspector.get_columns("tasks")]
                if task_cols and "next_attempt_at" not in task_cols:
                    with self._engine.begin() as conn:
                        conn.execute(text("ALTER TABLE tasks ADD COLUMN next_attempt_at TIMESTAMP"))
            except Exception as e:
                logger.warning("migration_failed", error=str(e))

            self._initialized = True
```

(d) Set the lease in `fail_task` on the retry branch (lines 460-477). Replace the method body's status branch:

```python
            row.last_error = error[:4000]
            if dead_letter:
                row.status = TaskStatus.DEAD_LETTERED.value
                row.completed_at = _utcnow()
            else:
                row.status = TaskStatus.PENDING.value  # retry
                row.next_attempt_at = _utcnow() + timedelta(seconds=_retry_delay_seconds(row.attempts))
            s.commit()
```

(e) Add the readiness filter to the candidate select in `claim_pending_tasks` (the `select(TaskRow.task_id).where(...)` added in Task 3):

```python
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_state_retry_backoff.py -q`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=src .venv/bin/python -m pytest -q -m "not slow and not smoke"`
Expected: PASS (now ~199 passing).

- [ ] **Step 6: Commit**

```bash
git add src/awareness/storage/state.py tests/unit/test_state_retry_backoff.py
git commit -m "feat(state): exponential backoff lease for failed-task retries"
```

---

## Plan-level self-review checklist (run after all tasks)

- [ ] All six tasks committed; full suite green: `PYTHONPATH=src .venv/bin/python -m pytest -q -m "not slow and not smoke"`.
- [ ] Tasks 4 + 5 both landed (co-landing constraint).
- [ ] `ruff check src/awareness/storage/state.py src/awareness/workers/engine.py src/awareness/tail/engine.py` clean (run `.venv/bin/python -m ruff check ...`).
- [ ] No new mypy errors in touched files (`.venv/bin/python -m mypy src/awareness/storage/state.py` — note: project is `strict`; match surrounding style).

## Spec coverage map

| Spec workstream C item | Task |
|---|---|
| SQLite WAL + busy_timeout (blind-spot) | 1 |
| `JobStatus` NameError | 2 |
| Atomic `claim_pending_tasks` | 3 |
| Orphaned-RUNNING reaper | 4 |
| No premature COMPLETED on stop | 5 |
| Backoff task retries | 6 |
| Single job-ownership/leasing contract | Partially (reaper lease + atomic claim); the API double-runner guard is deferred to Plan 2/3 where `api/server.py` is in scope. |
