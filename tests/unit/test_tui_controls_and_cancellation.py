import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from awareness.cli.main import _make_tui_layout
from awareness.planner.planner import Planner
from awareness.schemas.jobs import BackfillRequest, JobStatus
from awareness.storage.state import StateDB
from awareness.workers.engine import WorkerEngine


@pytest.mark.asyncio
async def test_worker_engine_run_tail_cancellation(tmp_path):
    db = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    db.init()
    p = Planner(db)
    
    # Submit tail job
    job_id = p.submit_tail({"feeds": []})
    
    engine = WorkerEngine(db, p, concurrency=1)
    
    # Launch run_tail in a background task
    run_task = asyncio.create_task(engine.run_tail(job_id, poll_seconds=0.05))
    
    await asyncio.sleep(0.05)
    
    # Cancel the job in DB
    db.set_job_status(job_id, JobStatus.CANCELLED)
    
    # Wait for run_tail to finish
    await asyncio.wait_for(run_task, timeout=2.0)
    
    assert db.get_job(job_id).status == JobStatus.CANCELLED


@pytest.mark.asyncio
async def test_worker_engine_run_tail_pause_and_resume(tmp_path):
    db = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    db.init()
    p = Planner(db)
    job_id = p.submit_tail({"feeds": []})
    
    engine = WorkerEngine(db, p, concurrency=1)
    
    run_task = asyncio.create_task(engine.run_tail(job_id, poll_seconds=0.05))
    await asyncio.sleep(0.05)
    
    # Set job status to PAUSED
    db.set_job_status(job_id, JobStatus.PAUSED)
    await asyncio.sleep(0.1)
    
    # Verify it is still running (hasn't exited)
    assert not run_task.done()
    
    # Cancel the job so it exits
    db.set_job_status(job_id, JobStatus.CANCELLED)
    await asyncio.wait_for(run_task, timeout=2.0)
    
    assert db.get_job(job_id).status == JobStatus.CANCELLED


def test_tui_layout_generation(tmp_path):
    db = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    db.init()
    p = Planner(db)
    
    # Submit some jobs
    p.submit_tail({"feeds": []})
    req = BackfillRequest(
        start=datetime(2024, 6, 1, tzinfo=UTC),
        end=datetime(2024, 6, 14, tzinfo=UTC),
        max_tasks=5,
    )
    p.submit_backfill(req)
    
    # Mock settings
    mock_settings = MagicMock()
    mock_settings.data_dir = tmp_path
    mock_settings.staging_jsonl_dir.return_value = tmp_path / "staging"
    mock_settings.duckdb_path.return_value = tmp_path / "metadata.duckdb"
    mock_settings.iceberg_warehouse = "s3://warehouse"
    mock_settings.iceberg_catalog_db = "db"
    
    # Mock DuckDbIndex execute method to return fake captures
    mock_idx = MagicMock()
    mock_idx.execute.return_value = [
        {"fetch_ts": datetime.now(), "title": "Test Capture 1", "domain": "example.com"},
        {"fetch_ts": datetime.now(), "title": "Test Capture 2", "domain": "test.org"},
    ]
    
    # Call _make_tui_layout
    layout = _make_tui_layout(db, mock_settings, mock_idx, selected_job_idx=0)
    
    # Verify that the layout components are updated
    assert layout is not None
    child_names = [child.name for child in layout.children]
    assert "header" in child_names
    assert "body" in child_names
    assert "footer" in child_names

    
    # Verify DuckDB was queried for captures
    mock_idx.execute.assert_called_once()
    assert "captures" in mock_idx.execute.call_args[0][0]
