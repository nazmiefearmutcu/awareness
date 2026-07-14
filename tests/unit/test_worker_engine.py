from __future__ import annotations

import pytest

import types

from awareness.config import get_settings, reset_settings
from awareness.planner.planner import Planner
from awareness.storage import gdrive
from awareness.storage.state import StateDB
from awareness.workers.engine import WorkerEngine, _format_size


def test_format_size() -> None:
    assert _format_size(0) == "0 B"
    assert _format_size(1023) == "1023.0 B"
    assert _format_size(1024) == "1.0 KB"
    assert _format_size(1024 * 1024) == "1.0 MB"
    assert _format_size(1024 * 1024 * 1024 * 5) == "5.0 GB"

def test_worker_engine_metrics_initialization(tmp_path) -> None:
    state = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    state.init()
    planner = Planner(state)
    
    engine = WorkerEngine(state, planner, concurrency=1)
    assert engine._total_bytes_processed == 0
    assert engine._total_docs_processed == 0
    assert engine._silent_progress is False

def test_worker_engine_silent_progress_initialization(tmp_path) -> None:
    state = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    state.init()
    planner = Planner(state)
    
    engine = WorkerEngine(state, planner, concurrency=1, silent_progress=True)
    assert engine._silent_progress is True


def _gdrive_only_engine(tmp_project, monkeypatch) -> WorkerEngine:
    """A WorkerEngine in GDrive-only mode (local staging + Iceberg disabled)."""
    monkeypatch.setenv("AW_ENABLE_JSONL_STAGING", "false")
    monkeypatch.setenv("AW_ENABLE_ICEBERG", "false")
    monkeypatch.setenv("AW_ENABLE_GDRIVE", "true")
    reset_settings()
    state = StateDB(f"sqlite:///{tmp_project / 'state.db'}")
    state.init()
    return WorkerEngine(state, Planner(state), concurrency=1)


async def test_gdrive_only_failed_upload_retains_chunk(tmp_project, monkeypatch) -> None:
    # Regression: in GDrive-only mode a FAILED upload must NOT delete the only
    # on-disk copy of the chunk (that was silent data loss).
    monkeypatch.setattr(gdrive, "is_authorized", lambda: True)
    monkeypatch.setattr(gdrive, "upload_file", lambda p: None)  # simulate failure
    engine = _gdrive_only_engine(tmp_project, monkeypatch)
    engine._batch_buffer.append(
        types.SimpleNamespace(as_iceberg_row=lambda: {"doc_id": "d1", "text": "hello"})
    )
    await engine._flush(force=True)
    chunks = list(get_settings().staging_jsonl_dir().rglob("*.jsonl"))
    assert chunks, "failed GDrive-only upload must retain the chunk for recovery"


async def test_gdrive_only_successful_upload_deletes_chunk(tmp_project, monkeypatch) -> None:
    # The flip side: a successful upload in GDrive-only mode still cleans up the
    # temp chunk (no local staging requested).
    monkeypatch.setattr(gdrive, "is_authorized", lambda: True)
    monkeypatch.setattr(gdrive, "upload_file", lambda p: "file-id-123")
    engine = _gdrive_only_engine(tmp_project, monkeypatch)
    engine._batch_buffer.append(
        types.SimpleNamespace(as_iceberg_row=lambda: {"doc_id": "d2", "text": "world"})
    )
    await engine._flush(force=True)
    chunks = list(get_settings().staging_jsonl_dir().rglob("*.jsonl"))
    assert not chunks, "successful upload with staging disabled should remove the temp chunk"


def test_worker_engine_mute_duplicates_initialization(tmp_path) -> None:
    state = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    state.init()
    planner = Planner(state)
    
    engine = WorkerEngine(state, planner, concurrency=1, mute_duplicates=True)
    assert engine._mute_duplicates is True


@pytest.mark.asyncio
async def test_near_dup_terminal_log_includes_hamming_and_mute_silences(tmp_path) -> None:
    """NEAR_DUP lines include reason/hamming; mute_duplicates hides NEAR_SKIP too."""
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    from awareness.dedup.engine import DedupDecision, DedupOutcome
    from awareness.schemas.doc import DocCapture, RobotsDecision, SourceKind, SourceRef
    from awareness.schemas.jobs import JobKind, JobState, JobStatus, TaskState
    from awareness.util.hashing import content_hash, doc_id_for, simhash64

    def _cap(url: str, text: str, *, observed: str = "2024-01-01T00:00:00+00:00") -> DocCapture:
        ch = content_hash(text)
        did = doc_id_for(url, ch)
        return DocCapture(
            doc_id=did,
            capture_id=f"cap-{did[:8]}-{observed}",
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
            observed_ts=datetime.fromisoformat(observed),
            text=text,
            content_hash=ch,
            near_dup_hash=simhash64(text),
            robots_decision=RobotsDecision.NOT_APPLICABLE,
            title=f"Title {url}",
            language="en",
        )

    body = " ".join(["Near dup terminal log body phrase."] * 20)
    cap_new = _cap("https://a.test/base", body)
    cap_tight = _cap(
        "https://b.test/tight",
        body + " tiny edit",
        observed="2024-01-02T00:00:00+00:00",
    )
    cap_loose = _cap(
        "https://c.test/loose",
        body + " somewhat more different trailing words here",
        observed="2024-01-03T00:00:00+00:00",
    )

    outcomes = {
        cap_new.doc_id: DedupOutcome(
            decision=DedupDecision.NEW, dup_group=cap_new.doc_id, reason="new_content"
        ),
        cap_tight.doc_id: DedupOutcome(
            decision=DedupDecision.NEAR_DUP,
            dup_group=cap_new.doc_id,
            reason="simhash128_hamming=5",
            hamming=5,
        ),
        cap_loose.doc_id: DedupOutcome(
            decision=DedupDecision.NEAR_DUP,
            dup_group=cap_new.doc_id,
            reason="simhash128_hamming=18",
            hamming=18,
        ),
    }

    async def _run(mute: bool) -> list[str]:
        state = StateDB(f"sqlite:///{tmp_path / f'state-mute-{int(mute)}.db'}")
        state.init()
        engine = WorkerEngine(state, Planner(state), concurrency=1, mute_duplicates=mute)

        def evaluate(cap: DocCapture) -> DedupOutcome:
            out = outcomes[cap.doc_id]
            cap.parent_doc_or_dup_group = out.dup_group
            return out

        engine._dedup.evaluate = evaluate  # type: ignore[method-assign]
        engine._is_tty = True
        engine._silent_progress = False
        printed: list[str] = []
        engine._console.print = lambda *a, **k: printed.append(str(a[0]) if a else "")  # type: ignore[method-assign]

        async def fake_run_partition(partition, context):
            yield cap_new
            yield cap_tight
            yield cap_loose

        adapter = MagicMock()
        adapter.run_partition = fake_run_partition
        engine._registry = MagicMock()
        engine._registry.get.return_value = adapter
        engine._topic_filter_for = lambda _job_id: None  # type: ignore[method-assign]

        async def noop_flush(force: bool = False) -> None:
            return None

        engine._flush = noop_flush  # type: ignore[method-assign]

        job_id = f"j-log-{int(mute)}"
        state.create_job(
            JobState(
                job_id=job_id,
                kind=JobKind.BACKFILL,
                status=JobStatus.RUNNING,
                request={"sources": ["local_fixture"]},
            )
        )
        task = TaskState(
            task_id=f"t-log-{int(mute)}",
            job_id=job_id,
            source_type=SourceKind.LOCAL_FIXTURE,
            partition_key="pk",
            payload={},
        )
        state.add_tasks([task])
        await engine._run_task(task)
        return printed

    unmuted = await _run(False)
    assert any("NEAR_SKIP" in line for line in unmuted), unmuted
    assert any("simhash128_hamming=5" in line for line in unmuted), unmuted
    assert any("NEAR_DUP" in line and "simhash128_hamming=18" in line for line in unmuted), unmuted
    assert any("NEW" in line for line in unmuted), unmuted

    muted = await _run(True)
    assert any("NEW" in line for line in muted), muted
    assert not any("NEAR_SKIP" in line or "NEAR_DUP" in line for line in muted), muted
