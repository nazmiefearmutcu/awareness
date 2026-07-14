"""CLI metrics table polish: WET parse + JSONL sync summary strips."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from awareness.cli.main import (
    app,
    format_jsonl_sync_summary_line,
    format_wet_parse_summary_line,
    summarize_jsonl_sync_metrics_table,
    summarize_wet_parse_metrics_table,
)
from awareness.obs.metrics import get_metrics

runner = CliRunner()


def test_summarize_wet_parse_metrics_table_none_without_series() -> None:
    snap = {
        "counters": [{"name": "http.fetch_attempts", "value": 3}],
        "histograms": [{"name": "http.fetch_seconds", "count": 1, "p95": 0.1}],
    }
    assert summarize_wet_parse_metrics_table(snap) is None


def test_summarize_wet_parse_metrics_table_aggregates() -> None:
    snap = {
        "counters": [
            {
                "name": "cc_wet.records_seen",
                "labels": {"crawl_id": "c1"},
                "value": 100,
            },
            {
                "name": "cc_wet.records_seen",
                "labels": {"crawl_id": "c2"},
                "value": 50,
            },
            {
                "name": "cc_wet.shard_parse_emitted",
                "labels": {"crawl_id": "c1"},
                "value": 40,
            },
            {
                "name": "cc_wet.shard_download_attempts",
                "labels": {"outcome": "cache_hit", "crawl_id": "c1"},
                "value": 3,
            },
            {
                "name": "cc_wet.shard_download_attempts",
                "labels": {"outcome": "ok", "crawl_id": "c1"},
                "value": 2,
            },
            {
                "name": "cc_wet.shard_download_attempts",
                "labels": {"outcome": "error", "crawl_id": "c1"},
                "value": 1,
            },
            # Quality-only series must not alone trigger the strip when mixed in.
            {"name": "cc_wet.quality_filtered", "value": 9},
        ],
        "histograms": [
            {
                "name": "cc_wet.shard_parse_seconds",
                "labels": {"crawl_id": "c1"},
                "count": 3,
                "p95": 0.4,
            },
            {
                "name": "cc_wet.iter_parse_seconds",
                "count": 1,
                "p95": 0.2,
            },
            {
                "name": "cc_wet.shard_download_seconds",
                "labels": {"outcome": "ok"},
                "count": 2,
                "p95": 1.0,
            },
            {
                "name": "cc_wet.shard_download_seconds",
                "labels": {"outcome": "cache_hit"},
                "count": 3,
                "p95": 0.01,
            },
        ],
    }
    summary = summarize_wet_parse_metrics_table(snap)
    assert summary is not None
    assert summary["records_seen"] == 150
    assert summary["parse_emitted"] == 40
    assert summary["download_attempts"] == 6
    assert summary["download_ok"] == 5
    assert summary["download_cache_hits"] == 3
    # (0.4*3 + 0.2*1) / 4 = 0.35
    assert summary["parse_p95"] == pytest.approx(0.35)
    # (1.0*2 + 0.01*3) / 5 = 0.406
    assert summary["download_p95"] == pytest.approx(0.406)

    line = format_wet_parse_summary_line(summary)
    assert line.startswith("WET")
    assert "seen=150" in line
    assert "emitted=40" in line
    assert "download=5/6 ok" in line
    assert "cache=3" in line
    assert "parse_p95=350ms" in line
    assert "dl_p95=406ms" in line


def test_summarize_jsonl_sync_metrics_table_none_without_series() -> None:
    snap = {
        "counters": [{"name": "jsonl.records_committed", "value": 10}],
        "histograms": [{"name": "jsonl.commit_seconds", "count": 1, "p95": 0.01}],
        "gauges": [],
    }
    # Commit-only series do not trigger the crash-safe sync strip.
    assert summarize_jsonl_sync_metrics_table(snap) is None


def test_summarize_jsonl_sync_metrics_table_aggregates() -> None:
    snap = {
        "counters": [
            {"name": "jsonl.syncs", "labels": {"outcome": "ok"}, "value": 8},
            {"name": "jsonl.syncs", "labels": {"outcome": "error"}, "value": 1},
            {"name": "jsonl.orphans_recovered", "value": 2},
            {"name": "jsonl.orphans_removed", "value": 3},
            {"name": "jsonl.records_committed", "value": 50},
        ],
        "histograms": [
            {"name": "jsonl.sync_seconds", "count": 8, "p95": 0.002},
            {"name": "jsonl.sync_seconds", "count": 1, "p95": 0.01},
            {"name": "jsonl.commit_seconds", "count": 2, "p95": 0.05},
        ],
        "gauges": [
            {"name": "jsonl.open_records", "value": 12},
            {"name": "jsonl.open_bytes", "value": 4096},
        ],
    }
    summary = summarize_jsonl_sync_metrics_table(snap)
    assert summary is not None
    assert summary["syncs"] == 9
    assert summary["sync_ok"] == 8
    assert summary["orphans_recovered"] == 2
    assert summary["orphans_removed"] == 3
    assert summary["open_records"] == 12
    assert summary["sync_p95"] == pytest.approx((0.002 * 8 + 0.01 * 1) / 9)

    line = format_jsonl_sync_summary_line(summary)
    assert line.startswith("JSONL")
    assert "sync=8/9 ok" in line
    assert "sync_p95=" in line
    assert "open=12" in line
    assert "orphans=2 recovered/3 removed" in line


def test_metrics_table_shows_wet_and_jsonl_sync_strips() -> None:
    m = get_metrics()
    m.inc(
        "cc_wet.records_seen",
        value=5.0,
        labels={"crawl_id": "CC-MAIN-2024-26"},
    )
    m.inc(
        "cc_wet.shard_parse_emitted",
        value=2.0,
        labels={"crawl_id": "CC-MAIN-2024-26"},
    )
    m.inc(
        "cc_wet.shard_download_attempts",
        value=1.0,
        labels={"crawl_id": "CC-MAIN-2024-26", "outcome": "cache_hit"},
    )
    m.observe(
        "cc_wet.shard_parse_seconds",
        0.12,
        labels={"crawl_id": "CC-MAIN-2024-26"},
    )
    m.inc("jsonl.syncs", value=1.0, labels={"outcome": "ok"})
    m.observe("jsonl.sync_seconds", 0.003)
    m.inc("jsonl.orphans_recovered", value=1.0)

    result = runner.invoke(app, ["metrics", "--format", "table"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "WET" in out
    assert "seen=5" in out or "seen=" in out
    assert "JSONL" in out
    assert "sync=" in out
    assert "ms" in out
