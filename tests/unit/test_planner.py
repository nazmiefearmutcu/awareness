"""Planner tests — partition emission and request routing."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from awareness.planner.planner import Planner
from awareness.schemas.doc import SourceKind
from awareness.schemas.jobs import BackfillRequest, JobStatus
from awareness.sources import get_adapter_registry
from awareness.sources.commoncrawl_wet import crawl_ids_for_range
from awareness.storage.state import StateDB


def test_crawl_ids_for_range_covers_a_year(monkeypatch) -> None:
    import awareness.sources.cc_crawls as cc

    monkeypatch.setattr(cc, "_fetch_catalog", lambda: None)
    monkeypatch.setattr(cc, "_read_cache", lambda: None)
    crawls = crawl_ids_for_range(
        datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 12, 31, tzinfo=UTC)
    )
    assert crawls, "a one-year range must resolve at least one real crawl"
    assert set(crawls).issubset(set(cc.BUNDLED_CRAWL_IDS))
    assert all(c.startswith("CC-MAIN-2024-") for c in crawls)


def test_planner_emits_tasks_for_default_sources(tmp_path: Path) -> None:
    db = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    db.init()
    p = Planner(db)
    req = BackfillRequest(
        start=datetime(2024, 6, 1, tzinfo=UTC),
        end=datetime(2024, 6, 14, tzinfo=UTC),
        max_tasks=5,
    )
    job_id = p.submit_backfill(req)
    status = p.status(job_id)
    assert status["job_id"] == job_id
    assert status["tasks_total"] >= 1
    assert status["status"] in (JobStatus.PENDING.value, JobStatus.RUNNING.value)


def test_planner_status_for_unknown_job_returns_error(tmp_path: Path) -> None:
    db = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    db.init()
    p = Planner(db)
    assert p.status("does-not-exist").get("error") == "unknown_job"


def test_adapter_registry_has_all_sources(tmp_path: Path) -> None:
    reg = get_adapter_registry()
    kinds = {a.source_type for a in reg.all()}
    # All declared kinds must be registered.
    expected = {
        SourceKind.COMMON_CRAWL_WET,
        SourceKind.COMMON_CRAWL_INDEX,
        SourceKind.COMMON_CRAWL_WARC,
        SourceKind.FINEWEB,
        SourceKind.RSS,
        SourceKind.TAIL_RECRAWL,
        SourceKind.GDELT,
    }
    assert expected.issubset(kinds)


def test_delete_job(tmp_path: Path) -> None:
    db = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    db.init()
    p = Planner(db)
    req = BackfillRequest(
        start=datetime(2024, 6, 1, tzinfo=UTC),
        end=datetime(2024, 6, 14, tzinfo=UTC),
        max_tasks=5,
    )
    job_id = p.submit_backfill(req)
    
    # Check that job and tasks exist
    assert db.get_job(job_id) is not None
    # Verify tasks were added
    with db.session() as s:
        from sqlalchemy import select
        from awareness.storage.state import TaskRow
        tasks = list(s.scalars(select(TaskRow).where(TaskRow.job_id == job_id)))
        assert len(tasks) > 0

    # Delete the job
    db.delete_job(job_id)

    # Check that job and tasks no longer exist
    assert db.get_job(job_id) is None
    with db.session() as s:
        tasks = list(s.scalars(select(TaskRow).where(TaskRow.job_id == job_id)))
        assert len(tasks) == 0
