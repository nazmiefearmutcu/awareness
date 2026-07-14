"""CLI metrics table polish: FineWeb summary strip + duration formatting."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from awareness.cli.main import (
    _format_metric_duration,
    _hist_is_seconds,
    app,
    format_fineweb_summary_line,
    summarize_fineweb_metrics_table,
)
from awareness.obs.metrics import get_metrics

runner = CliRunner()


def test_format_metric_duration_units() -> None:
    assert _format_metric_duration(0.0005) == "500µs"
    assert _format_metric_duration(0.012) == "12ms"
    assert _format_metric_duration(0.325) == "325ms"
    assert _format_metric_duration(1.5) == "1.50s"
    assert _format_metric_duration(12.34) == "12.3s"
    assert _format_metric_duration(-1) == "—"


def test_hist_is_seconds_names() -> None:
    assert _hist_is_seconds("fineweb.load_seconds")
    assert _hist_is_seconds("http.fetch_seconds")
    assert _hist_is_seconds("jsonl.commit_seconds")
    assert not _hist_is_seconds("fineweb.rows_admitted")
    assert not _hist_is_seconds("something_bytes")


def test_summarize_fineweb_metrics_table_none_without_series() -> None:
    snap = {
        "counters": [{"name": "http.fetch_attempts", "value": 3}],
        "histograms": [{"name": "http.fetch_seconds", "count": 1, "p95": 0.1}],
    }
    assert summarize_fineweb_metrics_table(snap) is None


def test_summarize_fineweb_metrics_table_aggregates() -> None:
    snap = {
        "counters": [
            {
                "name": "fineweb.rows_admitted",
                "labels": {"dataset": "fineweb"},
                "value": 10,
            },
            {
                "name": "fineweb.rows_admitted",
                "labels": {"dataset": "fineweb_2"},
                "value": 5,
            },
            {
                "name": "fineweb.rows_seen",
                "labels": {"dataset": "fineweb"},
                "value": 40,
            },
            {
                "name": "fineweb.rows_filtered",
                "labels": {"reason": "empty", "dataset": "fineweb"},
                "value": 8,
            },
            {
                "name": "fineweb.rows_filtered",
                "labels": {"reason": "language", "dataset": "fineweb"},
                "value": 12,
            },
            {
                "name": "fineweb.load_attempts",
                "labels": {"outcome": "ok", "dataset": "fineweb"},
                "value": 3,
            },
            {
                "name": "fineweb.load_attempts",
                "labels": {"outcome": "error", "dataset": "fineweb"},
                "value": 1,
            },
        ],
        "histograms": [
            {
                "name": "fineweb.load_seconds",
                "labels": {"outcome": "ok", "dataset": "fineweb"},
                "count": 3,
                "p95": 0.4,
            },
            {
                "name": "fineweb.load_seconds",
                "labels": {"outcome": "error", "dataset": "fineweb"},
                "count": 1,
                "p95": 0.1,
            },
        ],
    }
    summary = summarize_fineweb_metrics_table(snap)
    assert summary is not None
    assert summary["admitted"] == 15
    assert summary["filtered"] == 20
    assert summary["seen"] == 40
    assert summary["load_attempts"] == 4
    assert summary["load_ok"] == 3
    assert summary["top_filter"] == "language"
    # (0.4*3 + 0.1*1) / 4 = 0.325
    assert summary["load_p95"] == pytest.approx(0.325)

    line = format_fineweb_summary_line(summary)
    assert line.startswith("FineWeb  ")
    assert "admitted=15" in line
    assert "filtered=20" in line
    assert "seen=40" in line
    assert "load=3/4 ok" in line
    assert "load_p95=325ms" in line
    assert "top_filter=language" in line


def test_metrics_table_shows_fineweb_summary_and_ms_latencies() -> None:
    m = get_metrics()
    m.inc("fineweb.rows_admitted", value=2.0, labels={"dataset": "fineweb"})
    m.inc("fineweb.rows_filtered", value=5.0, labels={"reason": "empty", "dataset": "fineweb"})
    m.inc("fineweb.rows_seen", value=7.0, labels={"dataset": "fineweb"})
    m.inc(
        "fineweb.load_attempts",
        value=1.0,
        labels={"outcome": "ok", "dataset": "fineweb"},
    )
    m.observe(
        "fineweb.load_seconds",
        0.08,
        labels={"outcome": "ok", "dataset": "fineweb"},
    )

    result = runner.invoke(app, ["metrics", "--format", "table", "--prefix", "fineweb"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "FineWeb" in out
    assert "admitted=2" in out
    assert "filtered=5" in out
    assert "load=1/1 ok" in out
    # Sub-second histogram values should render as ms, not raw 0.08.
    assert "ms" in out
    assert "fineweb.load_seconds" in out or "Histograms" in out
