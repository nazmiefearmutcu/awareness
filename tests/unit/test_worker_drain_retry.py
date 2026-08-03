"""H-01/H-02/H-05/M-05/M-06 worker drain, retry-backoff, and counter fixes.

Covers:
- run_job must NOT declare the job drained while a retry is parked in backoff
  (PENDING with a future next_attempt_at) or while a task is RUNNING.
- Once the backoff elapses, the retry runs and the job COMPLETES.
- Job counters: no_adapter → tasks_failed+1 / tasks_dead_lettered+1;
  terminal failure → tasks_failed+1; transient retry → no increment.
- Loose NEAR_DUP is stored (docs_emitted) and NOT also counted as dedup-dropped.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from awareness.config import reset_settings
from awareness.dedup.engine import DedupDecision, DedupOutcome
from awareness.planner.planner import Planner
from awareness.schemas.doc import DocCapture, RobotsDecision, SourceKind, SourceRef
from awareness.schemas.jobs import JobKind, JobState, JobStatus, TaskState, TaskStatus
from awareness.storage.state import StateDB, TaskRow, _utcnow
from awareness.util.hashing import content_hash, doc_id_for, simhash64
from awareness.workers.engine import WorkerEngine


def _cap(url: str, text: str) -> DocCapture:
    ch = content_hash(text)
    did = doc_id_for(url, ch)
    return DocCapture(
        doc_id=did,
        capture_id=f"cap-{did[:8]}-test",
        source=SourceRef(
            source_type=SourceKind.LOCAL_FIXTURE,
            source_name="fixture",
            source_locator="local",
        ),
        discovery_channel="test",
        ingest_version="0.0",
        url=url,
        canonical_url=url,
        domain="a.test",
        fetch_ts=datetime(2024, 1, 1, tzinfo=UTC),
        observed_ts=datetime(2024, 1, 1, tzinfo=UTC),
        text=text,
        content_hash=ch,
        near_dup_hash=simhash64(text),
        robots_decision=RobotsDecision.NOT_APPLICABLE,
        title=f"Title {url}",
        language="en",
    )


class _FlakyAdapter:
    """Succeeds after ``fail_first_n`` attempts."""

    def __init__(self, caps: list[DocCapture], fail_first_n: int = 1) -> None:
        self.caps = caps
        self.fail_first_n = fail_first_n
        self.attempts = 0

    async def run_partition(self, partition, context):
        self.attempts += 1
        if self.attempts <= self.fail_first_n:
            raise RuntimeError("boom")
        for cap in self.caps:
            yield cap


def _engine_with_adapter(tmp_project, adapter) -> tuple[StateDB, WorkerEngine]:
    state = StateDB(f"sqlite:///{tmp_project / 'state.db'}")
    state.init()
    planner = Planner(state)
    engine = WorkerEngine(state, planner, concurrency=1, silent_progress=True)
    engine._registry = MagicMock()
    engine._registry.get.return_value = adapter
    engine._topic_filter_for = lambda _job_id: None  # type: ignore[method-assign]
    return state, engine


def _seed_job(state: StateDB, job_id: str = "j1") -> str:
    state.create_job(JobState(job_id=job_id, kind=JobKind.BACKFILL, request={}))
    return job_id


async def test_run_job_retry_then_drain(tmp_project, monkeypatch) -> None:
    # H-01: a failed task (attempts < max) is PENDING with a future
    # next_attempt_at; run_job must keep polling instead of declaring drained.
    monkeypatch.setattr("awareness.storage.state._retry_delay_seconds", lambda _attempts: 0.01)
    state, engine = _engine_with_adapter(tmp_project, _FlakyAdapter(caps=[_cap("https://a.test/1", "unique body text one")]))
    job_id = _seed_job(state)
    state.add_tasks(
        [
            TaskState(
                task_id="t1",
                job_id=job_id,
                source_type=SourceKind.LOCAL_FIXTURE,
                partition_key="pk1",
                payload={},
            )
        ]
    )
    await engine.run_job(job_id, poll_seconds=0.01)
    await engine.aclose()

    job = state.get_job(job_id)
    assert job.status == JobStatus.COMPLETED
    assert job.docs_emitted == 1
    counts = state.task_status_counts(job_id)
    assert counts.get(TaskStatus.COMPLETED.value) == 1


async def test_run_job_does_not_complete_while_backoff_pending(tmp_project, monkeypatch) -> None:
    # Long backoff: the empty-claim poll must NOT count toward drain while a
    # retry is scheduled in the future.
    monkeypatch.setattr("awareness.storage.state._retry_delay_seconds", lambda _attempts: 3600.0)
    state, engine = _engine_with_adapter(tmp_project, _FlakyAdapter(caps=[_cap("https://a.test/2", "unique body text two")]))
    job_id = _seed_job(state)
    state.add_tasks(
        [
            TaskState(
                task_id="t1",
                job_id=job_id,
                source_type=SourceKind.LOCAL_FIXTURE,
                partition_key="pk1",
                payload={},
            )
        ]
    )
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(engine.run_job(job_id, poll_seconds=0.02), timeout=0.6)

    job = state.get_job(job_id)
    assert job.status == JobStatus.RUNNING  # never false-COMPLETED
    assert state.task_status_counts(job_id).get(TaskStatus.PENDING.value) == 1

    # Backdate the retry lease → the next run drains to COMPLETED.
    with state.session() as s:
        row = s.get(TaskRow, "t1")
        assert row is not None
        row.next_attempt_at = _utcnow() - timedelta(seconds=1)
        s.commit()
    await engine.run_job(job_id, poll_seconds=0.01)
    await engine.aclose()
    assert state.get_job(job_id).status == JobStatus.COMPLETED


async def test_run_job_does_not_complete_while_task_running(tmp_project) -> None:
    # H-02: a RUNNING task (orphan with un-expired lease) must block drain.
    state, engine = _engine_with_adapter(tmp_project, None)
    job_id = _seed_job(state)
    state.add_tasks(
        [
            TaskState(
                task_id="t1",
                job_id=job_id,
                source_type=SourceKind.LOCAL_FIXTURE,
                partition_key="pk1",
                payload={},
            )
        ]
    )
    claimed = state.claim_pending_tasks(job_id, limit=1)
    assert len(claimed) == 1  # task stranded RUNNING, no live worker

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(engine.run_job(job_id, poll_seconds=0.02), timeout=0.5)
    assert state.get_job(job_id).status == JobStatus.RUNNING

    # Release the stranded task → the job genuinely drains.
    state.complete_task("t1", docs_emitted=0, docs_dedup_dropped=0, bytes_processed=0, checkpoint=None)
    await engine.run_job(job_id, poll_seconds=0.01)
    await engine.aclose()
    assert state.get_job(job_id).status == JobStatus.COMPLETED


async def test_no_adapter_increments_failed_and_dead_lettered(tmp_project) -> None:
    # M-05: no_adapter DLQ path was missing job counters.
    state, engine = _engine_with_adapter(tmp_project, None)  # registry.get → None
    job_id = _seed_job(state)
    state.add_tasks(
        [
            TaskState(
                task_id="t1",
                job_id=job_id,
                source_type=SourceKind.RSS,
                partition_key="pk1",
                payload={},
            )
        ]
    )
    await engine.run_job(job_id, poll_seconds=0.01)
    await engine.aclose()

    job = state.get_job(job_id)
    assert job.tasks_failed == 1
    assert job.tasks_dead_lettered == 1
    assert state.count_dlq(job_id=job_id) == 1


async def test_terminal_failure_increments_tasks_failed(tmp_project, monkeypatch) -> None:
    # M-05: tasks_failed must be incremented on terminal (dead-lettered) failures.
    monkeypatch.setenv("AW_MAX_RETRIES", "0")
    reset_settings()
    state, engine = _engine_with_adapter(tmp_project, _FlakyAdapter(caps=[], fail_first_n=99))
    job_id = _seed_job(state)
    state.add_tasks(
        [
            TaskState(
                task_id="t1",
                job_id=job_id,
                source_type=SourceKind.LOCAL_FIXTURE,
                partition_key="pk1",
                payload={},
            )
        ]
    )
    await engine.run_job(job_id, poll_seconds=0.01)
    await engine.aclose()

    job = state.get_job(job_id)
    assert job.status == JobStatus.COMPLETED  # drained; task dead-lettered
    assert job.tasks_failed == 1
    assert job.tasks_dead_lettered == 1
    assert state.count_dlq(job_id=job_id) == 1


async def test_retry_failure_does_not_increment_tasks_failed(tmp_project, monkeypatch) -> None:
    # A transient (retryable) failure is not terminal — tasks_failed stays 0.
    monkeypatch.setattr("awareness.storage.state._retry_delay_seconds", lambda _attempts: 3600.0)
    state, engine = _engine_with_adapter(tmp_project, _FlakyAdapter(caps=[], fail_first_n=99))
    job_id = _seed_job(state)
    state.add_tasks(
        [
            TaskState(
                task_id="t1",
                job_id=job_id,
                source_type=SourceKind.LOCAL_FIXTURE,
                partition_key="pk1",
                payload={},
            )
        ]
    )
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(engine.run_job(job_id, poll_seconds=0.02), timeout=0.5)

    job = state.get_job(job_id)
    assert job.tasks_failed == 0
    assert job.tasks_dead_lettered == 0
    assert state.task_status_counts(job_id).get(TaskStatus.PENDING.value) == 1


async def test_loose_near_dup_not_double_counted(tmp_project) -> None:
    # M-06: a loose NEAR_DUP that IS stored counts as emitted, not dedup-dropped.
    body = " ".join(["A distinctive shared base paragraph for near-dup tests."] * 12)
    cap_new = _cap("https://a.test/base", body)
    cap_loose = _cap("https://c.test/loose", body + " somewhat different trailing content here")

    outcomes = {
        cap_new.doc_id: DedupOutcome(decision=DedupDecision.NEW, dup_group=cap_new.doc_id, reason="new_content"),
        cap_loose.doc_id: DedupOutcome(
            decision=DedupDecision.NEAR_DUP,
            dup_group=cap_new.doc_id,
            reason="simhash128_hamming=18",
            hamming=18,
        ),
    }

    state = StateDB(f"sqlite:///{tmp_project / 'neardup.db'}")
    state.init()
    planner = Planner(state)
    engine = WorkerEngine(state, planner, concurrency=1, silent_progress=True)

    def evaluate(cap: DocCapture) -> DedupOutcome:
        out = outcomes[cap.doc_id]
        cap.parent_doc_or_dup_group = out.dup_group
        return out

    engine._dedup.evaluate = evaluate  # type: ignore[method-assign]

    async def fake_run_partition(partition, context):
        yield cap_new
        yield cap_loose

    adapter = MagicMock()
    adapter.run_partition = fake_run_partition
    engine._registry = MagicMock()
    engine._registry.get.return_value = adapter
    engine._topic_filter_for = lambda _job_id: None  # type: ignore[method-assign]

    job_id = _seed_job(state)
    task = TaskState(
        task_id="t-nd",
        job_id=job_id,
        source_type=SourceKind.LOCAL_FIXTURE,
        partition_key="pk-nd",
        payload={},
    )
    state.add_tasks([task])
    await engine._run_task(task)
    await engine.aclose()

    job = state.get_job(job_id)
    assert job.docs_emitted == 2  # NEW + loose NEAR_DUP both stored
    assert job.docs_dedup_dropped == 0  # no double-count
