"""Stale tail/API process reconciliation.

When a tail or API process dies without cleaning state, status must not lie.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from awareness.schemas.jobs import JobStatus, JobState, JobKind
from awareness.storage.state import StateDB, _utcnow


@pytest.fixture()
def state(tmp_path: Path) -> StateDB:
    db = StateDB(f"sqlite:///{tmp_path / 'state.sqlite'}")
    db.init()
    return db


def test_set_tail_records_pid(state: StateDB) -> None:
    state.set_tail(True, job_id="tail-1", note="active", pid=os.getpid())
    info = state.get_tail(reconcile=False)
    assert info["running"] is True
    assert info["pid"] == os.getpid()
    assert info["job_id"] == "tail-1"


def test_get_tail_reconciles_dead_pid(state: StateDB) -> None:
    # PID 1 is init/launchd — usually alive on macOS; use a definitely dead pid.
    dead_pid = 2_147_483_647  # INT_MAX-ish; almost never a live process
    # Confirm it's dead
    with pytest.raises(OSError):
        os.kill(dead_pid, 0)

    state.create_job(
        JobState(
            job_id="tail-dead",
            kind=JobKind.TAIL,
            status=JobStatus.RUNNING,
            request={},
            created_at=_utcnow(),
            started_at=_utcnow(),
        )
    )
    state.set_tail(True, job_id="tail-dead", note="tail-active", pid=dead_pid)

    info = state.get_tail(reconcile=True)
    assert info["running"] is False
    assert info["notes"] and "reconciled" in info["notes"]
    job = state.get_job("tail-dead")
    assert job is not None
    assert job.status == JobStatus.CANCELLED


def test_get_tail_keeps_live_pid(state: StateDB) -> None:
    state.set_tail(True, job_id="tail-live", note="tail-active", pid=os.getpid())
    info = state.get_tail(reconcile=True)
    assert info["running"] is True
    assert info["pid"] == os.getpid()


def test_get_tail_reconciles_legacy_running_without_pid(state: StateDB) -> None:
    """Pre-pid rows that still say running are treated as orphaned."""
    state.set_tail(True, job_id="tail-legacy", note="tail-active", pid=None)
    # Force-clear pid the old way (row may still have running=1, pid=NULL)
    with state.session() as s:
        from awareness.storage.state import TailRow

        row = s.get(TailRow, 1)
        assert row is not None
        row.running = 1
        row.pid = None
        row.job_id = "tail-legacy"
        s.commit()

    info = state.get_tail(reconcile=True)
    assert info["running"] is False


def test_api_pid_file_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from awareness.config import reset_settings
    from awareness.cli import main as cli_main

    monkeypatch.setenv("AW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("AW_CONFIG_FILE", raising=False)
    reset_settings()

    state_dir = tmp_path / "data" / "state"
    state_dir.mkdir(parents=True)
    dead = 2_147_483_647
    (state_dir / "api.pid").write_text(str(dead), encoding="utf-8")

    assert cli_main._get_api_pid() is None
    # Stale file cleaned
    assert not (state_dir / "api.pid").exists()


def test_reconcile_does_not_clobber_restart(state: StateDB) -> None:
    """If a new tail starts after we observe an orphan, do not stop it."""
    dead_pid = 2_147_483_647
    state.set_tail(True, job_id="tail-old", note="tail-active", pid=dead_pid)

    # Simulate concurrent restart with live pid before reconcile commits.
    with state.session() as s:
        from awareness.storage.state import TailRow

        row = s.get(TailRow, 1)
        assert row is not None
        # Manually set dead row state
        row.running = 1
        row.pid = dead_pid
        row.job_id = "tail-old"
        s.commit()

    # Now a new live tail claims the row
    state.set_tail(True, job_id="tail-new", note="tail-active", pid=os.getpid())
    info = state.get_tail(reconcile=True)
    assert info["running"] is True
    assert info["job_id"] == "tail-new"
    assert info["pid"] == os.getpid()


def test_launchd_belongs_exact_path(tmp_path: Path) -> None:
    import plistlib
    from awareness.cli.main import _launchd_belongs_to_project

    root = tmp_path / "awareness_dev"
    root.mkdir()
    sibling = tmp_path / "awareness"
    sibling.mkdir()
    plist = tmp_path / "com.awareness.api.8085.plist"
    with plist.open("wb") as fh:
        plistlib.dump({"WorkingDirectory": str(root)}, fh)

    assert _launchd_belongs_to_project(plist, root) is True
    assert _launchd_belongs_to_project(plist, sibling) is False


def test_reconcile_orphan_tail_jobs(state: StateDB) -> None:
    # Three historic tail jobs stuck RUNNING; no live tail owner.
    for i, jid in enumerate(("tail-a", "tail-b", "tail-c"), 1):
        state.create_job(
            JobState(
                job_id=jid,
                kind=JobKind.TAIL,
                status=JobStatus.RUNNING,
                request={},
                created_at=_utcnow(),
                started_at=_utcnow(),
            )
        )
    state.set_tail(False, note="stopped")
    n = state.reconcile_orphan_tail_jobs()
    assert n == 3
    for jid in ("tail-a", "tail-b", "tail-c"):
        job = state.get_job(jid)
        assert job is not None
        assert job.status == JobStatus.CANCELLED


def test_abandon_inflight_tasks(state: StateDB) -> None:
    from awareness.schemas.doc import SourceKind
    from awareness.schemas.jobs import TaskState, TaskStatus

    state.create_job(
        JobState(
            job_id="tail-z",
            kind=JobKind.TAIL,
            status=JobStatus.CANCELLED,
            request={},
            created_at=_utcnow(),
        )
    )
    state.add_tasks(
        [
            TaskState(
                task_id="t-pending",
                job_id="tail-z",
                source_type=SourceKind.RSS,
                partition_key="rss:a",
                payload={"url": "https://example.com/a"},
                status=TaskStatus.PENDING,
            ),
            TaskState(
                task_id="t-running",
                job_id="tail-z",
                source_type=SourceKind.RSS,
                partition_key="rss:b",
                payload={"url": "https://example.com/b"},
                status=TaskStatus.RUNNING,
            ),
        ]
    )
    n = state.abandon_inflight_tasks("tail-z")
    assert n == 2
    counts = state.task_status_counts("tail-z")
    assert counts.get("skipped") == 2
    assert counts.get("pending", 0) == 0
    assert counts.get("running", 0) == 0
