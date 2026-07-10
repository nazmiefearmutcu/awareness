from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import pytest

from awareness.config import get_settings
from awareness.schemas.jobs import TaskState, TaskStatus
from awareness.schemas.doc import SourceKind
from awareness.storage.state import StateDB, TaskRow
from awareness.workers.engine import DatabaseReaper


def test_cleanup_old_tasks(tmp_path) -> None:
    state = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    state.init()

    # Create dummy tasks in different states and ages
    now = datetime.now(UTC)
    old_time = now - timedelta(days=10)
    recent_time = now - timedelta(days=2)

    # 1. Old completed task -> should be deleted
    task1 = TaskState(
        task_id="t1",
        job_id="j1",
        source_type=SourceKind.RSS,
        partition_key="rss:url1",
        payload={},
        status=TaskStatus.COMPLETED,
        created_at=old_time,
        completed_at=old_time,
    )
    
    # 2. Recent completed task -> should be kept
    task2 = TaskState(
        task_id="t2",
        job_id="j1",
        source_type=SourceKind.RSS,
        partition_key="rss:url2",
        payload={},
        status=TaskStatus.COMPLETED,
        created_at=recent_time,
        completed_at=recent_time,
    )

    # 3. Old pending task -> should be kept (completed_at is None)
    task3 = TaskState(
        task_id="t3",
        job_id="j1",
        source_type=SourceKind.RSS,
        partition_key="rss:url3",
        payload={},
        status=TaskStatus.PENDING,
        created_at=old_time,
    )

    state.add_tasks([task1, task2, task3])

    # Mark completion in DB to ensure completed_at is set properly in the rows
    with state.session() as s:
        row1 = s.get(TaskRow, "t1")
        row1.completed_at = old_time
        row1.status = TaskStatus.COMPLETED.value
        
        row2 = s.get(TaskRow, "t2")
        row2.completed_at = recent_time
        row2.status = TaskStatus.COMPLETED.value
        s.commit()

    # Run cleanup with 5 days retention
    deleted = state.cleanup_old_tasks(retention_days=5)
    assert deleted == 1

    # Verify rows in DB
    with state.session() as s:
        assert s.get(TaskRow, "t1") is None
        assert s.get(TaskRow, "t2") is not None
        assert s.get(TaskRow, "t3") is not None


def test_vacuum_database(tmp_path) -> None:
    state = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    state.init()
    # Verify that executing vacuum doesn't raise errors on SQLite
    state.vacuum_database()


async def test_database_reaper_loop(tmp_path) -> None:
    state = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    state.init()

    # Create an old task that should be deleted
    old_time = datetime.now(UTC) - timedelta(days=10)
    task = TaskState(
        task_id="t_reaper",
        job_id="j1",
        source_type=SourceKind.RSS,
        partition_key="rss:url_reaper",
        payload={},
        status=TaskStatus.COMPLETED,
        created_at=old_time,
        completed_at=old_time,
    )
    state.add_tasks([task])
    with state.session() as s:
        row = s.get(TaskRow, "t_reaper")
        row.completed_at = old_time
        row.status = TaskStatus.COMPLETED.value
        s.commit()

    # Initialize reaper with a very short interval
    reaper = DatabaseReaper(state, interval_seconds=1, retention_days=5)
    
    # Start reaper
    await reaper.start()
    assert reaper._task is not None
    assert not reaper._task.done()

    # Wait for reaper to run at least once
    await asyncio.sleep(1.5)

    # Stop reaper
    await reaper.stop()
    assert reaper._task is None

    # Check if task was cleaned up
    with state.session() as s:
        assert s.get(TaskRow, "t_reaper") is None
