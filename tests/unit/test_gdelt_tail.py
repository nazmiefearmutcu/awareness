"""Tests for the GDELT live-firehose wiring and tail topic-filter plumbing."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from awareness.cli.main import app
from awareness.config import get_settings
from awareness.planner.planner import Planner
from awareness.schemas.doc import SourceKind
from awareness.schemas.jobs import JobKind, JobState, JobStatus, TaskState, TaskStatus
from awareness.sources.gdelt import latest_gkg_slot
from awareness.storage.state import StateDB
from awareness.tail.engine import TailEngine
from awareness.workers.engine import WorkerEngine

runner = CliRunner()


def test_latest_gkg_slot_rounds_down_with_lag() -> None:
    # 12:07 minus 30-min lag = 11:37 → floored to the 15-min slot 11:30.
    now = datetime(2026, 6, 1, 12, 7, 0, tzinfo=UTC)
    assert latest_gkg_slot(now, lag_minutes=30) == "20260601113000"
    # zero lag, exact quarter hour stays put
    assert latest_gkg_slot(datetime(2026, 6, 1, 9, 45, 0, tzinfo=UTC), lag_minutes=0) == "20260601094500"


def test_latest_gkg_slot_default_is_quarter_hour_stamp() -> None:
    slot = latest_gkg_slot()
    assert len(slot) == 14 and slot.isdigit()
    assert slot[10:12] in {"00", "15", "30", "45"}  # minutes land on a quarter
    assert slot.endswith("00")  # seconds zeroed


def test_gdelt_task_shape(tmp_project: Path) -> None:
    settings = get_settings()
    state = StateDB(settings.state_db_url or "sqlite:///t.db")
    state.init()
    engine = TailEngine(state, Planner(state))
    task = engine._gdelt_task("tail-x", "20260601113000", 250)
    assert task.source_type == SourceKind.GDELT
    assert task.partition_key == "gdelt:gkg:20260601113000"
    assert task.payload == {"slot": "20260601113000", "max_urls": 250}


def test_worker_resolves_topic_filter_from_tail_request(tmp_project: Path) -> None:
    settings = get_settings()
    state = StateDB(settings.state_db_url or "sqlite:///t.db")
    state.init()
    planner = Planner(state)
    # Tail job request is the seeds dict; match config lives alongside it.
    job_id = planner.submit_tail({"feeds": [], "match": ["ukraine"], "match_field": "both"})
    engine = WorkerEngine(state, planner)
    flt = engine._topic_filter_for(job_id)
    assert flt is not None and flt.active
    assert flt.matches("Ukraine grain deal", "")
    assert not flt.matches("Local bakery opens", "")

    plain = planner.submit_tail({"feeds": []})
    assert engine._topic_filter_for(plain) is None


def test_completed_tail_recrawl_is_not_rearmed(tmp_project: Path) -> None:
    """The HIGH-severity fix: re-adding an already-fetched GDELT/feed URL must
    NOT re-arm it (which would force a redundant network re-crawl)."""
    settings = get_settings()
    state = StateDB(settings.state_db_url or "sqlite:///t.db")
    state.init()
    state.create_job(JobState(job_id="tail-x", kind=JobKind.TAIL, status=JobStatus.RUNNING, request={}))

    url_task = TaskState(task_id="t-1", job_id="tail-x", source_type=SourceKind.TAIL_RECRAWL,
                         partition_key="tail:https://e.x/a", payload={"url": "https://e.x/a"})
    rss_task = TaskState(task_id="t-2", job_id="tail-x", source_type=SourceKind.RSS,
                         partition_key="rss:https://e.x/feed", payload={"kind": "rss", "url": "https://e.x/feed"})
    assert state.add_tasks([url_task, rss_task]) == 2
    # Mark both COMPLETED.
    for tid in ("t-1", "t-2"):
        state.complete_task(tid, docs_emitted=1, docs_dedup_dropped=0, bytes_processed=10, checkpoint={})

    # Re-add the same partition_keys (as a reseed / overlapping GDELT slot would).
    state.add_tasks([
        TaskState(task_id="t-3", job_id="tail-x", source_type=SourceKind.TAIL_RECRAWL,
                  partition_key="tail:https://e.x/a", payload={"url": "https://e.x/a"}),
        TaskState(task_id="t-4", job_id="tail-x", source_type=SourceKind.RSS,
                  partition_key="rss:https://e.x/feed", payload={"kind": "rss", "url": "https://e.x/feed"}),
    ])
    counts = state.task_status_counts("tail-x")
    # The one-shot tail_recrawl row stays COMPLETED (no redundant re-fetch);
    # the RSS discovery row is re-armed back to PENDING (must re-poll).
    assert counts.get(TaskStatus.PENDING.value, 0) == 1
    assert counts.get(TaskStatus.COMPLETED.value, 0) == 1


def test_tail_start_rejects_negative_gdelt_max_urls() -> None:
    res = runner.invoke(app, ["tail", "start", "--gdelt-max-urls", "-5", "--no-interactive"])
    assert res.exit_code != 0


def test_backfill_submit_help_advertises_match() -> None:
    res = runner.invoke(app, ["backfill", "submit", "--help"])
    assert res.exit_code == 0
    assert "--match" in res.output


def test_tail_start_help_advertises_gdelt_and_match() -> None:
    res = runner.invoke(app, ["tail", "start", "--help"])
    assert res.exit_code == 0
    assert "--gdelt" in res.output
    assert "--match" in res.output
