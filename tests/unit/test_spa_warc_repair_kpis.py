"""Static + pure-logic checks: dashboard WARC repair SPA KPIs."""

from __future__ import annotations

from pathlib import Path

import pytest

APP_JS = Path("src/awareness/api/web/app.js")
INDEX_HTML = Path("src/awareness/api/web/index.html")


def test_dashboard_html_has_warc_repair_kpis() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="kpi-dash-warc-docs"' in html
    assert 'id="kpi-dash-warc-fetch-p95"' in html
    assert 'id="kpi-dash-warc-attempts"' in html
    assert "WARC repair docs" in html
    assert "WARC repair fetch p95" in html
    assert "WARC repair attempts" in html


def test_app_js_wires_warc_repair_kpis() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "function summarizeWarcRepairMetrics(" in app_js
    assert "warc_repair.docs_emitted" in app_js
    assert "warc_repair.fetch_attempts" in app_js
    assert "warc_repair.parse_attempts" in app_js
    assert "warc_repair.fetch_seconds" in app_js
    assert "warc_repair.parse_seconds" in app_js
    assert "kpi-dash-warc-docs" in app_js
    assert "kpi-dash-warc-fetch-p95" in app_js
    assert "kpi-dash-warc-attempts" in app_js


def test_summarize_warc_repair_metrics_contract() -> None:
    """Pin fetch/parse outcome counters + weighted p95 aggregation."""
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
            {"name": "fts.build_seconds", "count": 3, "p95": 9.0},
        ],
    }
    counters = snap["counters"]
    docs = sum(c["value"] for c in counters if c["name"] == "warc_repair.docs_emitted")
    fetch_attempts = sum(
        c["value"] for c in counters if c["name"] == "warc_repair.fetch_attempts"
    )
    fetch_ok = sum(
        c["value"]
        for c in counters
        if c["name"] == "warc_repair.fetch_attempts"
        and (c.get("labels") or {}).get("outcome") == "ok"
    )
    fetch_http = sum(
        c["value"]
        for c in counters
        if c["name"] == "warc_repair.fetch_attempts"
        and (c.get("labels") or {}).get("outcome") == "http_error"
    )
    fetch_net = sum(
        c["value"]
        for c in counters
        if c["name"] == "warc_repair.fetch_attempts"
        and (c.get("labels") or {}).get("outcome") == "network_error"
    )
    parse_emitted = sum(
        c["value"]
        for c in counters
        if c["name"] == "warc_repair.parse_attempts"
        and (c.get("labels") or {}).get("outcome") == "emitted"
    )
    parse_empty = sum(
        c["value"]
        for c in counters
        if c["name"] == "warc_repair.parse_attempts"
        and (c.get("labels") or {}).get("outcome") == "empty"
    )
    fetch_hists = [
        h for h in snap["histograms"] if h["name"] == "warc_repair.fetch_seconds"
    ]
    parse_hists = [
        h for h in snap["histograms"] if h["name"] == "warc_repair.parse_seconds"
    ]
    fetch_count = sum(h["count"] for h in fetch_hists)
    fetch_p95 = sum(h["p95"] * h["count"] for h in fetch_hists) / fetch_count
    parse_count = sum(h["count"] for h in parse_hists)
    parse_p95 = sum(h["p95"] * h["count"] for h in parse_hists) / parse_count

    assert docs == 4
    assert fetch_attempts == 8
    assert fetch_ok == 5
    assert fetch_http == 2
    assert fetch_net == 1
    assert parse_emitted == 4
    assert parse_empty == 1
    assert fetch_p95 == pytest.approx((0.4 * 5 + 0.1 * 2) / 7)
    assert parse_p95 == pytest.approx((0.05 * 4 + 0.02 * 1) / 5)
