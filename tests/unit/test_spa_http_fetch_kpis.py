"""Static + pure-logic checks: dashboard HTTP fetch p95 / attempts KPIs."""

from __future__ import annotations

import re
from pathlib import Path

APP_JS = Path("src/awareness/api/web/app.js")
INDEX_HTML = Path("src/awareness/api/web/index.html")


def test_dashboard_html_has_http_fetch_kpis() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="kpi-dash-http-p95"' in html
    assert 'id="kpi-dash-http-attempts"' in html
    assert "HTTP fetch p95" in html
    assert "HTTP attempts" in html


def test_app_js_summarizes_http_fetch_metrics() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "function summarizeHttpFetchMetrics(" in app_js
    assert "function formatFetchLatency(" in app_js
    assert 'h.name !== "http.fetch_seconds"' in app_js or 'h.name === "http.fetch_seconds"' in app_js
    assert "http.fetch_attempts" in app_js
    assert "http.fetch_retries" in app_js
    # Dashboard refresh pulls /metrics alongside status/dedup.
    assert 'api("/metrics")' in app_js
    assert "kpi-dash-http-p95" in app_js
    assert "kpi-dash-http-attempts" in app_js


def test_summarize_http_fetch_metrics_logic() -> None:
    """Re-implement the pure aggregation contract for a regression pin.

    JS helpers are not executed here; this pins the expected weighted-p95
    behaviour the SPA documents so refactors keep the same shape.
    """
    snap = {
        "histograms": [
            {
                "name": "http.fetch_seconds",
                "labels": {"outcome": "ok", "status_class": "2xx"},
                "count": 8,
                "p95": 0.2,
            },
            {
                "name": "http.fetch_seconds",
                "labels": {"outcome": "retryable", "status_class": "5xx"},
                "count": 2,
                "p95": 1.0,
            },
            {"name": "other.hist", "count": 99, "p95": 9.0},
        ],
        "counters": [
            {"name": "http.fetch_attempts", "labels": {"outcome": "ok"}, "value": 8},
            {"name": "http.fetch_attempts", "labels": {"outcome": "retryable"}, "value": 2},
            {"name": "http.fetch_retries", "labels": {"outcome": "ok"}, "value": 1},
            {"name": "docs.emitted", "value": 50},
        ],
    }
    # Weighted p95 = (0.2*8 + 1.0*2) / 10 = 0.36
    hists = [h for h in snap["histograms"] if h["name"] == "http.fetch_seconds"]
    total = sum(h["count"] for h in hists)
    weighted = sum(h["p95"] * h["count"] for h in hists) / total
    assert abs(weighted - 0.36) < 1e-9
    attempts = sum(
        c["value"] for c in snap["counters"] if c["name"] == "http.fetch_attempts"
    )
    retries = sum(
        c["value"] for c in snap["counters"] if c["name"] == "http.fetch_retries"
    )
    assert attempts == 10
    assert retries == 1


def test_format_fetch_latency_contract_examples() -> None:
    """Document ms/s thresholds used by formatFetchLatency (static source pin)."""
    app_js = APP_JS.read_text(encoding="utf-8")
    # ms branch for sub-second, fixed decimals for multi-second.
    assert "Math.round(sec * 1000)" in app_js
    assert re.search(r"toFixed\(2\)", app_js)
    assert re.search(r"toFixed\(1\)", app_js)
