"""Static + pure-logic checks: dashboard robots network fetch KPIs."""

from __future__ import annotations

from pathlib import Path

import pytest

APP_JS = Path("src/awareness/api/web/app.js")
INDEX_HTML = Path("src/awareness/api/web/index.html")


def test_dashboard_html_has_robots_fetch_kpis() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="kpi-dash-robots-p95"' in html
    assert 'id="kpi-dash-robots-attempts"' in html
    assert "Robots fetch p95" in html
    assert "Robots fetch attempts" in html


def test_app_js_summarizes_robots_fetch_metrics() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "robots.fetch_attempts" in app_js
    assert "robots.fetch_seconds" in app_js
    assert "robotsFetchP95" in app_js
    assert "robotsFetchAttempts" in app_js
    assert "kpi-dash-robots-p95" in app_js
    assert "kpi-dash-robots-attempts" in app_js
    assert "function summarizeStorageObsMetrics(" in app_js


def test_summarize_robots_fetch_contract() -> None:
    """Pin aggregation: attempts/ok from counters; weighted p95 from histograms."""
    snap = {
        "counters": [
            {
                "name": "robots.fetch_attempts",
                "labels": {"outcome": "ok", "status_class": "2xx"},
                "value": 6,
            },
            {
                "name": "robots.fetch_attempts",
                "labels": {"outcome": "missing", "status_class": "4xx"},
                "value": 2,
            },
            {
                "name": "robots.fetch_attempts",
                "labels": {"outcome": "error", "status_class": "transport"},
                "value": 1,
            },
            {"name": "iceberg.appended_rows", "value": 50},
        ],
        "histograms": [
            {
                "name": "robots.fetch_seconds",
                "labels": {"outcome": "ok", "status_class": "2xx"},
                "count": 6,
                "p95": 0.1,
            },
            {
                "name": "robots.fetch_seconds",
                "labels": {"outcome": "missing", "status_class": "4xx"},
                "count": 2,
                "p95": 0.5,
            },
            {
                "name": "iceberg.append_seconds",
                "count": 3,
                "p95": 9.0,
            },
        ],
        "gauges": [],
    }
    attempts = sum(
        c["value"] for c in snap["counters"] if c["name"] == "robots.fetch_attempts"
    )
    ok = sum(
        c["value"]
        for c in snap["counters"]
        if c["name"] == "robots.fetch_attempts"
        and (c.get("labels") or {}).get("outcome")
        in ("ok", "missing", "forbidden")
    )
    hists = [h for h in snap["histograms"] if h["name"] == "robots.fetch_seconds"]
    total = sum(h["count"] for h in hists)
    weighted = sum(h["p95"] * h["count"] for h in hists) / total
    assert attempts == 9
    assert ok == 8
    # (0.1*6 + 0.5*2) / 8 = 0.2
    assert weighted == pytest.approx(0.2)
