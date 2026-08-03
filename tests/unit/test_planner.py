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
    # Stay inside ISO year 2024 to avoid the year-boundary ISO-week shift
    # (Dec 30-31 2024 fall in ISO year 2025).
    import awareness.sources.cc_crawls as cc

    # Deterministic: force the bundled snapshot (no network) so the planned
    # IDs are the REAL published crawls, not fabricated even-week anchors.
    monkeypatch.setattr(cc, "_fetch_catalog", lambda: None)
    monkeypatch.setattr(cc, "_read_cache", lambda: None)
    start = datetime(2024, 1, 8, tzinfo=UTC)  # ISO week 2 of 2024
    end = datetime(2024, 12, 22, tzinfo=UTC)
    crawls = crawl_ids_for_range(start, end)
    assert len(crawls) >= 9
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


def test_planner_zero_tasks_warns_for_rss_only_source(tmp_path: Path) -> None:
    """RSS has no historical plan partitions — submit must flag zero_tasks."""
    db = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    db.init()
    p = Planner(db)
    req = BackfillRequest(
        start=datetime(2024, 6, 1, tzinfo=UTC),
        end=datetime(2024, 6, 14, tzinfo=UTC),
        sources=[SourceKind.RSS],
        notes="smoke-rss",
    )
    job_id = p.submit_backfill(req)
    status = p.status(job_id)
    assert status["tasks_total"] == 0
    assert status["warning"] == "zero_tasks"
    assert status["notes"] and "ZERO_TASKS" in status["notes"]
    assert "rss" in status["notes"].lower()
    reasons = status["zero_task_reasons"]
    assert isinstance(reasons, list) and reasons
    assert any(r.get("source") == "rss" for r in reasons)
    # User note is preserved after the warning payload.
    assert "smoke-rss" in status["notes"]


def test_parse_zero_task_reasons_helper() -> None:
    from awareness.planner.planner import _parse_zero_task_reasons

    assert _parse_zero_task_reasons(None) == []
    assert _parse_zero_task_reasons("all good") == []
    parsed = _parse_zero_task_reasons(
        "ZERO_TASKS: planned 0 tasks — rss: adapter plan() returned no partitions "
        "for this range/filters; fineweb: adapter plan() returned no partitions "
        "for this range/filters | user-note"
    )
    assert len(parsed) == 2
    assert parsed[0]["source"] == "rss"
    assert "no partitions" in parsed[0]["detail"]
    assert parsed[1]["source"] == "fineweb"
