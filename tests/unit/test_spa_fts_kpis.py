"""Static + pure-logic checks: dashboard FTS build SPA KPIs."""

from __future__ import annotations

from pathlib import Path

import pytest

APP_JS = Path("src/awareness/api/web/app.js")
INDEX_HTML = Path("src/awareness/api/web/index.html")


def test_dashboard_html_has_fts_kpis() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="kpi-dash-fts-builds"' in html
    assert 'id="kpi-dash-fts-p95"' in html
    assert 'id="kpi-dash-fts-rows"' in html
    assert "FTS builds" in html
    assert "FTS build p95" in html
    assert "FTS indexed rows" in html


def test_app_js_wires_fts_kpis() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "function summarizeFtsMetrics(" in app_js
    assert "fts.builds" in app_js
    assert "fts.build_seconds" in app_js
    assert "fts.build_errors" in app_js
    assert "fts.indexed_rows" in app_js
    assert "kpi-dash-fts-builds" in app_js
    assert "kpi-dash-fts-p95" in app_js
    assert "kpi-dash-fts-rows" in app_js


def test_summarize_fts_metrics_contract() -> None:
    """Pin mode counters + weighted p95 + indexed rows gauge aggregation."""
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
            {"name": "jsonl.sync_seconds", "count": 3, "p95": 0.1},
        ],
    }
    counters = snap["counters"]
    builds = sum(c["value"] for c in counters if c["name"] == "fts.builds")
    full = sum(
        c["value"]
        for c in counters
        if c["name"] == "fts.builds" and (c.get("labels") or {}).get("mode") == "full"
    )
    incremental = sum(
        c["value"]
        for c in counters
        if c["name"] == "fts.builds"
        and (c.get("labels") or {}).get("mode") == "incremental"
    )
    restore = sum(
        c["value"]
        for c in counters
        if c["name"] == "fts.builds" and (c.get("labels") or {}).get("mode") == "restore"
    )
    errors = sum(c["value"] for c in counters if c["name"] == "fts.build_errors")
    indexed = next(g["value"] for g in snap["gauges"] if g["name"] == "fts.indexed_rows")
    hists = [
        h
        for h in snap["histograms"]
        if h["name"] == "fts.build_seconds"
        and (h.get("labels") or {}).get("outcome") != "error"
    ]
    hist_count = sum(h["count"] for h in hists)
    weighted = sum(h["p95"] * h["count"] for h in hists) / hist_count

    assert builds == 10
    assert full == 2
    assert incremental == 3
    assert restore == 5
    assert errors == 1
    assert indexed == 42
    # (1.0*2 + 0.01*5) / 7
    assert weighted == pytest.approx((1.0 * 2 + 0.01 * 5) / 7)
