"""Worker task wall-clock duration + failure outcome metrics."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from awareness.dedup.engine import DedupDecision, DedupOutcome
from awareness.obs.metrics import get_metrics
from awareness.planner.planner import Planner
from awareness.schemas.doc import DocCapture, RobotsDecision, SourceKind, SourceRef
from awareness.schemas.jobs import JobKind, JobState, JobStatus, TaskState
from awareness.storage.state import StateDB
from awareness.util.hashing import content_hash, doc_id_for, simhash64
from awareness.workers.engine import WorkerEngine


def _cap(url: str, text: str) -> DocCapture:
    ch = content_hash(text)
    did = doc_id_for(url, ch)
    return DocCapture(
        doc_id=did,
        capture_id=f"cap-{did[:8]}",
        source=SourceRef(
            source_type=SourceKind.LOCAL_FIXTURE,
            source_name="fixture",
            source_locator="local",
        ),
        discovery_channel="test",
        ingest_version="0.0",
        url=url,
        canonical_url=url,
        domain=url.split("/")[2],
        fetch_ts=datetime(2024, 1, 1, tzinfo=UTC),
        observed_ts=datetime(2024, 1, 1, tzinfo=UTC),
        text=text,
        content_hash=ch,
        near_dup_hash=simhash64(text),
        robots_decision=RobotsDecision.NOT_APPLICABLE,
        title="t",
        language="en",
    )


def _engine(tmp_path, *, attempts: int = 0) -> tuple[WorkerEngine, StateDB, str]:
    state = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    state.init()
    engine = WorkerEngine(state, Planner(state), concurrency=1)
    engine._is_tty = False
    engine._silent_progress = True
    engine._topic_filter_for = lambda _job_id: None  # type: ignore[method-assign]

    async def noop_flush(force: bool = False) -> None:
        return None

    engine._flush = noop_flush  # type: ignore[method-assign]
    job_id = "j-dur"
    state.create_job(
        JobState(
            job_id=job_id,
            kind=JobKind.BACKFILL,
            status=JobStatus.RUNNING,
            request={"sources": ["local_fixture"]},
        )
    )
    task = TaskState(
        task_id="t-dur",
        job_id=job_id,
        source_type=SourceKind.LOCAL_FIXTURE,
        partition_key="pk",
        payload={},
        attempts=attempts,
    )
    state.add_tasks([task])
    return engine, state, job_id


@pytest.mark.asyncio
async def test_completed_task_records_duration_histogram(tmp_path) -> None:
    engine, state, _job_id = _engine(tmp_path)
    body = " ".join(["task duration body phrase."] * 30)
    cap = _cap("https://a.test/doc", body)

    def evaluate(c: DocCapture) -> DedupOutcome:
        c.parent_doc_or_dup_group = c.doc_id
        return DedupOutcome(decision=DedupDecision.NEW, dup_group=c.doc_id, reason="new")

    engine._dedup.evaluate = evaluate  # type: ignore[method-assign]

    async def fake_run_partition(partition, context):
        yield cap

    adapter = MagicMock()
    adapter.run_partition = fake_run_partition
    engine._registry = MagicMock()
    engine._registry.get.return_value = adapter

    m = get_metrics()
    before_completed = m.counter_value(
        "tasks.completed", labels={"source": SourceKind.LOCAL_FIXTURE.value}
    )

    task = TaskState(
        task_id="t-dur",
        job_id="j-dur",
        source_type=SourceKind.LOCAL_FIXTURE,
        partition_key="pk",
        payload={},
    )
    await engine._run_task(task)

    assert (
        m.counter_value(
            "tasks.completed", labels={"source": SourceKind.LOCAL_FIXTURE.value}
        )
        >= before_completed + 1
    )
    snap = m.snapshot()
    hists = [
        h
        for h in snap["histograms"]
        if h["name"] == "tasks.duration_seconds"
        and (h.get("labels") or {}).get("outcome") == "completed"
        and (h.get("labels") or {}).get("source") == SourceKind.LOCAL_FIXTURE.value
    ]
    assert hists and sum(h["count"] for h in hists) >= 1


@pytest.mark.asyncio
async def test_no_adapter_records_failed_and_duration(tmp_path) -> None:
    engine, _state, _job_id = _engine(tmp_path)
    engine._registry = MagicMock()
    engine._registry.get.return_value = None

    m = get_metrics()
    before = m.counter_value(
        "tasks.failed",
        labels={"source": SourceKind.LOCAL_FIXTURE.value, "outcome": "no_adapter"},
    )

    task = TaskState(
        task_id="t-dur",
        job_id="j-dur",
        source_type=SourceKind.LOCAL_FIXTURE,
        partition_key="pk",
        payload={},
    )
    await engine._run_task(task)

    assert (
        m.counter_value(
            "tasks.failed",
            labels={"source": SourceKind.LOCAL_FIXTURE.value, "outcome": "no_adapter"},
        )
        >= before + 1
    )
    snap = m.snapshot()
    hists = [
        h
        for h in snap["histograms"]
        if h["name"] == "tasks.duration_seconds"
        and (h.get("labels") or {}).get("outcome") == "no_adapter"
    ]
    assert hists and sum(h["count"] for h in hists) >= 1


@pytest.mark.asyncio
async def test_exception_retry_records_duration(tmp_path) -> None:
    engine, _state, _job_id = _engine(tmp_path, attempts=0)
    # max_retries default is high enough that attempts=0 is a retry, not dead letter
    async def boom_partition(partition, context):
        raise RuntimeError("adapter boom")
        yield  # pragma: no cover — make this an async generator

    adapter = MagicMock()
    adapter.run_partition = boom_partition
    engine._registry = MagicMock()
    engine._registry.get.return_value = adapter

    m = get_metrics()
    before = m.counter_value(
        "tasks.failed",
        labels={"source": SourceKind.LOCAL_FIXTURE.value, "outcome": "retry"},
    )

    task = TaskState(
        task_id="t-dur",
        job_id="j-dur",
        source_type=SourceKind.LOCAL_FIXTURE,
        partition_key="pk",
        payload={},
        attempts=0,
    )
    await engine._run_task(task)

    assert (
        m.counter_value(
            "tasks.failed",
            labels={"source": SourceKind.LOCAL_FIXTURE.value, "outcome": "retry"},
        )
        >= before + 1
    )
    snap = m.snapshot()
    hists = [
        h
        for h in snap["histograms"]
        if h["name"] == "tasks.duration_seconds"
        and (h.get("labels") or {}).get("outcome") == "retry"
    ]
    assert hists and sum(h["count"] for h in hists) >= 1
