from __future__ import annotations

from awareness.workers.engine import _format_size, WorkerEngine
from awareness.storage.state import StateDB
from awareness.planner.planner import Planner
import pytest

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
