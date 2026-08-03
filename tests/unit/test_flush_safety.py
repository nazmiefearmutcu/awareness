"""C-08 flush-safety tests.

Covers:
- A failed JSONL write keeps the buffered rows for one retry (no silent loss);
  the buffer is only cleared after a successful write.
- A second consecutive failure drops the batch with a critical log + metric
  (bounded retry — the buffer cannot grow unbounded).
- Iceberg-only mode keeps the temp chunk when the Iceberg append fails
  (the chunk is the only on-disk copy) and deletes it on success.
"""

from __future__ import annotations

import types

from awareness.config import get_settings, reset_settings
from awareness.obs.metrics import get_metrics
from awareness.planner.planner import Planner
from awareness.storage.state import StateDB
from awareness.workers.engine import WorkerEngine


class _FakeWriter:
    """JsonlStagingWriter stand-in: fails the first ``fail_times`` writes."""

    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.written: list[list[dict]] = []

    def write(self, rows: list[dict]) -> int:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise OSError("disk full")
        self.written.append(list(rows))
        return len(rows)

    def flush(self):
        return None

    def close(self) -> None:
        return None


def _engine(tmp_project, writer: _FakeWriter) -> WorkerEngine:
    state = StateDB(f"sqlite:///{tmp_project / 'state.db'}")
    state.init()
    engine = WorkerEngine(state, Planner(state), concurrency=1, jsonl_writer=writer)
    return engine


def _row(doc_id: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(as_iceberg_row=lambda: {"doc_id": doc_id, "text": "hello"})


async def test_flush_keeps_buffer_on_jsonl_failure(tmp_project) -> None:
    writer = _FakeWriter(fail_times=1)
    engine = _engine(tmp_project, writer)
    engine._batch_buffer.extend([_row("d1"), _row("d2")])

    await engine._flush(force=True)
    # C-08: rows survive a failed write.
    assert len(engine._batch_buffer) == 2
    assert writer.written == []
    assert engine._jsonl_retry_pending is True

    # Next attempt succeeds → buffer cleared exactly once.
    await engine._flush(force=True)
    assert engine._batch_buffer == []
    assert len(writer.written) == 1 and len(writer.written[0]) == 2
    assert engine._jsonl_retry_pending is False


async def test_flush_drops_batch_after_second_failure(tmp_project, caplog) -> None:
    writer = _FakeWriter(fail_times=2)
    engine = _engine(tmp_project, writer)
    engine._batch_buffer.extend([_row("d1"), _row("d2")])
    before = get_metrics().counter_value("flushes.dropped")

    await engine._flush(force=True)  # failure #1 → retained
    assert len(engine._batch_buffer) == 2
    await engine._flush(force=True)  # failure #2 → dropped
    assert engine._batch_buffer == []
    assert engine._jsonl_retry_pending is False
    assert get_metrics().counter_value("flushes.dropped") == before + 2
    assert any("jsonl_write_failed_batch_dropped" in r.message for r in caplog.records)


def _iceberg_engine(tmp_project, iceberg_writer) -> WorkerEngine:
    state = StateDB(f"sqlite:///{tmp_project / 'state.db'}")
    state.init()
    return WorkerEngine(
        state,
        Planner(state),
        concurrency=1,
        iceberg_writer=iceberg_writer,
    )


async def test_iceberg_only_append_failure_retains_chunk(tmp_project, monkeypatch) -> None:
    # C-08: in Iceberg-only mode a failed append must NOT delete the only
    # on-disk copy of the chunk (mirrors the gdrive_ok guard).
    monkeypatch.setenv("AW_ENABLE_JSONL_STAGING", "false")
    monkeypatch.setenv("AW_ENABLE_ICEBERG", "true")
    monkeypatch.setenv("AW_ENABLE_GDRIVE", "false")
    reset_settings()

    iceberg = types.SimpleNamespace()
    iceberg.ensure_table = lambda: None
    iceberg.append = lambda rows: (_ for _ in ()).throw(OSError("iceberg down"))
    engine = _iceberg_engine(tmp_project, iceberg)
    engine._batch_buffer.append(_row("d1"))

    await engine._flush(force=True)
    chunks = list(get_settings().staging_jsonl_dir().rglob("*.jsonl"))
    assert chunks, "failed Iceberg append must retain the chunk for recovery"


async def test_iceberg_only_append_success_deletes_chunk(tmp_project, monkeypatch) -> None:
    monkeypatch.setenv("AW_ENABLE_JSONL_STAGING", "false")
    monkeypatch.setenv("AW_ENABLE_ICEBERG", "true")
    monkeypatch.setenv("AW_ENABLE_GDRIVE", "false")
    reset_settings()

    appended: list[list[dict]] = []
    iceberg = types.SimpleNamespace()
    iceberg.ensure_table = lambda: None
    iceberg.append = lambda rows: appended.append(list(rows))
    engine = _iceberg_engine(tmp_project, iceberg)
    engine._batch_buffer.append(_row("d1"))

    await engine._flush(force=True)
    assert len(appended) == 1 and len(appended[0]) == 1
    chunks = list(get_settings().staging_jsonl_dir().rglob("*.jsonl"))
    assert not chunks, "successful Iceberg append should remove the temp chunk"
