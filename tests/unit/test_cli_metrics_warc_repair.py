"""CLI metrics table polish: WARC range-repair summary strip."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from awareness.cli.main import (
    app,
    format_warc_repair_summary_line,
    summarize_warc_repair_metrics_table,
)
from awareness.obs.metrics import get_metrics

runner = CliRunner()


def test_summarize_warc_repair_metrics_table_none_without_series() -> None:
    snap = {
        "counters": [{"name": "http.fetch_attempts", "value": 3}],
        "histograms": [{"name": "http.fetch_seconds", "count": 1, "p95": 0.1}],
    }
    assert summarize_warc_repair_metrics_table(snap) is None


def test_summarize_warc_repair_metrics_table_aggregates() -> None:
    snap = {
        "counters": [
            {
                "name": "warc_repair.docs_emitted",
                "labels": {"crawl_id": "c1"},
                "value": 4,
            },
            {
                "name": "warc_repair.fetch_attempts",
                "labels": {"outcome": "ok", "crawl_id": "c1"},
                "value": 5,
            },
            {
                "name": "warc_repair.fetch_attempts",
                "labels": {"outcome": "http_error", "crawl_id": "c1"},
                "value": 2,
            },
            {
                "name": "warc_repair.fetch_attempts",
                "labels": {"outcome": "network_error", "crawl_id": "c2"},
                "value": 1,
            },
            {
                "name": "warc_repair.parse_attempts",
                "labels": {"outcome": "emitted", "crawl_id": "c1"},
                "value": 4,
            },
            {
                "name": "warc_repair.parse_attempts",
                "labels": {"outcome": "empty", "crawl_id": "c1"},
                "value": 1,
            },
            {"name": "fts.builds", "labels": {"mode": "full"}, "value": 9},
        ],
        "histograms": [
            {
                "name": "warc_repair.fetch_seconds",
                "labels": {"outcome": "ok"},
                "count": 5,
                "p95": 0.4,
            },
            {
                "name": "warc_repair.fetch_seconds",
                "labels": {"outcome": "http_error"},
                "count": 2,
                "p95": 0.1,
            },
            {
                "name": "warc_repair.parse_seconds",
                "labels": {"outcome": "emitted"},
                "count": 4,
                "p95": 0.05,
            },
            {
                "name": "warc_repair.parse_seconds",
                "labels": {"outcome": "empty"},
                "count": 1,
                "p95": 0.02,
            },
        ],
    }
    summary = summarize_warc_repair_metrics_table(snap)
    assert summary is not None
    assert summary["docs_emitted"] == 4
    assert summary["fetch_attempts"] == 8
    assert summary["fetch_ok"] == 5
    assert summary["fetch_http_error"] == 2
    assert summary["fetch_network_error"] == 1
    assert summary["parse_emitted"] == 4
    assert summary["parse_empty"] == 1
    assert summary["fetch_p95"] == pytest.approx((0.4 * 5 + 0.1 * 2) / 7)
    assert summary["parse_p95"] == pytest.approx((0.05 * 4 + 0.02 * 1) / 5)


def test_format_warc_repair_summary_line() -> None:
    line = format_warc_repair_summary_line(
        {
            "docs_emitted": 4,
            "fetch_attempts": 8,
            "fetch_ok": 5,
            "fetch_http_error": 2,
            "fetch_network_error": 1,
            "parse_empty": 1,
            "fetch_p95": 0.3,
            "parse_p95": 0.04,
        }
    )
    assert line.startswith("WARC")
    assert "docs=4" in line
    assert "fetch=5/8 ok" in line
    assert "http_err=2" in line
    assert "net_err=1" in line
    assert "empty=1" in line
    assert "fetch_p95=" in line
    assert "parse_p95=" in line


def test_metrics_table_prints_warc_repair_strip() -> None:
    m = get_metrics()
    m.inc("warc_repair.docs_emitted", labels={"crawl_id": "c1"})
    m.inc(
        "warc_repair.fetch_attempts",
        labels={"outcome": "ok", "crawl_id": "c1"},
    )
    m.observe("warc_repair.fetch_seconds", 0.2, labels={"outcome": "ok"})
    result = runner.invoke(app, ["metrics", "--format", "table"])
    assert result.exit_code == 0, result.output
    assert "WARC" in result.output
    assert "docs=" in result.output or "fetch=" in result.output
