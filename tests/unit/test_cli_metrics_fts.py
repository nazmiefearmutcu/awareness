"""CLI metrics table polish: FTS build-path summary strip."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from awareness.cli.main import (
    app,
    format_fts_summary_line,
    summarize_fts_metrics_table,
)
from awareness.obs.metrics import get_metrics

runner = CliRunner()


def test_summarize_fts_metrics_table_none_without_series() -> None:
    snap = {
        "counters": [{"name": "http.fetch_attempts", "value": 3}],
        "histograms": [{"name": "http.fetch_seconds", "count": 1, "p95": 0.1}],
    }
    assert summarize_fts_metrics_table(snap) is None


def test_summarize_fts_metrics_table_aggregates() -> None:
    snap = {
        "counters": [
            {"name": "fts.builds", "labels": {"mode": "full"}, "value": 2},
            {"name": "fts.builds", "labels": {"mode": "incremental"}, "value": 3},
            {"name": "fts.builds", "labels": {"mode": "restore"}, "value": 5},
            {"name": "fts.build_errors", "labels": {"mode": "full"}, "value": 1},
            {"name": "jsonl.syncs", "value": 9},
        ],
        "gauges": [
            {"name": "fts.indexed_rows", "value": 42},
            {"name": "jsonl.open_records", "value": 1},
        ],
        "histograms": [
            {
                "name": "fts.build_seconds",
                "labels": {"mode": "full"},
                "count": 2,
                "p95": 1.0,
            },
            {
                "name": "fts.build_seconds",
                "labels": {"mode": "restore"},
                "count": 5,
                "p95": 0.01,
            },
            {
                "name": "fts.build_seconds",
                "labels": {"mode": "full", "outcome": "error"},
                "count": 1,
                "p95": 9.0,
            },
        ],
    }
    summary = summarize_fts_metrics_table(snap)
    assert summary is not None
    assert summary["builds"] == 10
    assert summary["full"] == 2
    assert summary["incremental"] == 3
    assert summary["restore"] == 5
    assert summary["errors"] == 1
    assert summary["indexed_rows"] == 42
    # (1.0*2 + 0.01*5) / 7 — error series excluded
    assert summary["build_p95"] == pytest.approx((1.0 * 2 + 0.01 * 5) / 7)


def test_format_fts_summary_line() -> None:
    line = format_fts_summary_line(
        {
            "builds": 10,
            "full": 2,
            "incremental": 3,
            "restore": 5,
            "errors": 1,
            "build_p95": 0.3,
            "indexed_rows": 42,
        }
    )
    assert line.startswith("FTS")
    assert "builds=10" in line
    assert "full=2" in line
    assert "incr=3" in line
    assert "restore=5" in line
    assert "rows=42" in line
    assert "errors=1" in line
    assert "p95=" in line


def test_metrics_table_prints_fts_strip() -> None:
    m = get_metrics()
    m.inc("fts.builds", labels={"mode": "full"})
    m.observe("fts.build_seconds", 0.5, labels={"mode": "full"})
    m.set("fts.indexed_rows", 7.0)
    result = runner.invoke(app, ["metrics", "--format", "table"])
    assert result.exit_code == 0, result.output
    assert "FTS" in result.output
    assert "builds=" in result.output or "full=" in result.output
