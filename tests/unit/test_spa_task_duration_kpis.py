"""Static + pure-logic checks: dashboard task duration SPA KPIs."""

from __future__ import annotations

from pathlib import Path

import pytest

APP_JS = Path("src/awareness/api/web/app.js")
INDEX_HTML = Path("src/awareness/api/web/index.html")


def test_dashboard_html_has_task_duration_kpis() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="kpi-dash-tasks-completed"' in html
    assert 'id="kpi-dash-tasks-p95"' in html
    assert 'id="kpi-dash-tasks-failed"' in html
    assert "Tasks completed" in html
    assert "Task duration p95" in html
    assert "Tasks failed" in html


def test_app_js_wires_task_duration_kpis() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "function summarizeTaskMetrics(" in app_js
    assert "tasks.completed" in app_js
    assert "tasks.failed" in app_js
    assert "tasks.duration_seconds" in app_js
    assert "kpi-dash-tasks-completed" in app_js
    assert "kpi-dash-tasks-p95" in app_js
    assert "kpi-dash-tasks-failed" in app_js


def test_summarize_task_metrics_contract() -> None:
    """Pin completed/failed outcome counters + weighted duration p95."""
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
    counters = snap["counters"]
    completed = sum(c["value"] for c in counters if c["name"] == "tasks.completed")
    failed = sum(c["value"] for c in counters if c["name"] == "tasks.failed")
    retry = sum(
        c["value"]
        for c in counters
        if c["name"] == "tasks.failed"
        and (c.get("labels") or {}).get("outcome") == "retry"
    )
    dead = sum(
        c["value"]
        for c in counters
        if c["name"] == "tasks.failed"
        and (c.get("labels") or {}).get("outcome") == "dead_letter"
    )
    no_adapter = sum(
        c["value"]
        for c in counters
        if c["name"] == "tasks.failed"
        and (c.get("labels") or {}).get("outcome") == "no_adapter"
    )
    hists = [h for h in snap["histograms"] if h["name"] == "tasks.duration_seconds"]
    hist_count = sum(h["count"] for h in hists)
    weighted = sum(h["p95"] * h["count"] for h in hists) / hist_count

    assert completed == 10
    assert failed == 4
    assert retry == 2
    assert dead == 1
    assert no_adapter == 1
    # (1.0*7 + 0.4*3 + 2.0*2 + 5.0*1) / 13
    assert weighted == pytest.approx((1.0 * 7 + 0.4 * 3 + 2.0 * 2 + 5.0 * 1) / 13)
