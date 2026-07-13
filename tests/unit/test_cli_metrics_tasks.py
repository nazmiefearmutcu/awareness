"""CLI metrics table polish: worker task duration/failure summary strip."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from awareness.cli.main import (
    app,
    format_task_summary_line,
    summarize_task_metrics_table,
)
from awareness.obs.metrics import get_metrics

runner = CliRunner()


def test_summarize_task_metrics_table_none_without_series() -> None:
    snap = {
        "counters": [{"name": "http.fetch_attempts", "value": 3}],
        "histograms": [{"name": "http.fetch_seconds", "count": 1, "p95": 0.1}],
    }
    assert summarize_task_metrics_table(snap) is None


def test_summarize_task_metrics_table_aggregates() -> None:
    snap = {
        "counters": [
            {"name": "tasks.completed", "labels": {"source": "rss"}, "value": 7},
            {"name": "tasks.completed", "labels": {"source": "gdelt"}, "value": 3},
            {
                "name": "tasks.failed",
                "labels": {"source": "rss", "outcome": "retry"},
                "value": 2,
            },
            {
                "name": "tasks.failed",
                "labels": {"source": "rss", "outcome": "dead_letter"},
                "value": 1,
            },
            {
                "name": "tasks.failed",
                "labels": {"source": "local_fixture", "outcome": "no_adapter"},
                "value": 1,
            },
            {"name": "warc_repair.docs_emitted", "value": 9},
        ],
        "histograms": [
            {
                "name": "tasks.duration_seconds",
                "labels": {"outcome": "completed", "source": "rss"},
                "count": 7,
                "p95": 1.0,
            },
            {
                "name": "tasks.duration_seconds",
                "labels": {"outcome": "completed", "source": "gdelt"},
                "count": 3,
                "p95": 0.4,
            },
            {
                "name": "tasks.duration_seconds",
                "labels": {"outcome": "retry", "source": "rss"},
                "count": 2,
                "p95": 2.0,
            },
            {
                "name": "tasks.duration_seconds",
                "labels": {"outcome": "dead_letter", "source": "rss"},
                "count": 1,
                "p95": 5.0,
            },
            {"name": "warc_repair.fetch_seconds", "count": 3, "p95": 9.0},
        ],
    }
    summary = summarize_task_metrics_table(snap)
    assert summary is not None
    assert summary["completed"] == 10
    assert summary["failed"] == 4
    assert summary["retry"] == 2
    assert summary["dead_letter"] == 1
    assert summary["no_adapter"] == 1
    assert summary["duration_p95"] == pytest.approx(
        (1.0 * 7 + 0.4 * 3 + 2.0 * 2 + 5.0 * 1) / 13
    )


def test_format_task_summary_line() -> None:
    line = format_task_summary_line(
        {
            "completed": 10,
            "failed": 4,
            "retry": 2,
            "dead_letter": 1,
            "no_adapter": 1,
            "duration_p95": 1.2,
        }
    )
    assert line.startswith("TASKS")
    assert "done=10" in line
    assert "fail=4" in line
    assert "retry=2" in line
    assert "dead=1" in line
    assert "no_adapter=1" in line
    assert "p95=" in line


def test_metrics_table_prints_task_strip() -> None:
    m = get_metrics()
    m.inc("tasks.completed", labels={"source": "rss"})
    m.observe(
        "tasks.duration_seconds",
        0.5,
        labels={"outcome": "completed", "source": "rss"},
    )
    result = runner.invoke(app, ["metrics", "--format", "table"])
    assert result.exit_code == 0, result.output
    assert "TASKS" in result.output
    assert "done=" in result.output or "p95=" in result.output
