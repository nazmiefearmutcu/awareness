from __future__ import annotations

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
