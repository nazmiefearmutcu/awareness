"""Static + pure-logic checks: dashboard FineWeb stream SPA KPIs."""

from __future__ import annotations

from pathlib import Path

import pytest

APP_JS = Path("src/awareness/api/web/app.js")
INDEX_HTML = Path("src/awareness/api/web/index.html")


def test_dashboard_html_has_fineweb_kpis() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="kpi-dash-fineweb-admitted"' in html
    assert 'id="kpi-dash-fineweb-filtered"' in html
    assert 'id="kpi-dash-fineweb-p95"' in html
    assert 'id="kpi-dash-fineweb-attempts"' in html
    assert "FineWeb admitted" in html
    assert "FineWeb filtered" in html
    assert "FineWeb load p95" in html
    assert "FineWeb load attempts" in html


def test_app_js_wires_fineweb_kpis() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "function summarizeFinewebMetrics(" in app_js
    assert "fineweb.rows_admitted" in app_js
    assert "fineweb.rows_filtered" in app_js
    assert "fineweb.rows_seen" in app_js
    assert "fineweb.load_attempts" in app_js
    assert "fineweb.load_seconds" in app_js
    assert "kpi-dash-fineweb-admitted" in app_js
    assert "kpi-dash-fineweb-filtered" in app_js
    assert "kpi-dash-fineweb-p95" in app_js
    assert "kpi-dash-fineweb-attempts" in app_js


def test_summarize_fineweb_metrics_contract() -> None:
    """Pin admit/filter/seen + load attempts/ok + weighted load p95."""
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
                "name": "fineweb.rows_seen",
                "labels": {"dataset": "fineweb_2"},
                "value": 20,
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
                "name": "fineweb.rows_filtered",
                "labels": {"reason": "domain", "dataset": "fineweb_2"},
                "value": 5,
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
            {"name": "gdelt.fetch_attempts", "value": 99},
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
            {
                "name": "gdelt.fetch_seconds",
                "count": 9,
                "p95": 9.0,
            },
        ],
    }
    counters = snap["counters"]
    admitted = sum(c["value"] for c in counters if c["name"] == "fineweb.rows_admitted")
    seen = sum(c["value"] for c in counters if c["name"] == "fineweb.rows_seen")
    filtered = sum(c["value"] for c in counters if c["name"] == "fineweb.rows_filtered")
    load_attempts = sum(
        c["value"] for c in counters if c["name"] == "fineweb.load_attempts"
    )
    load_ok = sum(
        c["value"]
        for c in counters
        if c["name"] == "fineweb.load_attempts"
        and (c.get("labels") or {}).get("outcome") == "ok"
    )
    by_reason: dict[str, float] = {}
    for c in counters:
        if c["name"] != "fineweb.rows_filtered":
            continue
        reason = (c.get("labels") or {}).get("reason") or "unknown"
        by_reason[reason] = by_reason.get(reason, 0) + c["value"]
    top_reason = max(by_reason, key=by_reason.get)  # type: ignore[arg-type]
    hists = [h for h in snap["histograms"] if h["name"] == "fineweb.load_seconds"]
    total = sum(h["count"] for h in hists)
    weighted = sum(h["p95"] * h["count"] for h in hists) / total

    assert admitted == 15
    assert seen == 60
    assert filtered == 25
    assert load_attempts == 4
    assert load_ok == 3
    assert top_reason == "language"
    assert by_reason["language"] == 12
    # (0.4*3 + 0.1*1) / 4 = 0.325
    assert weighted == pytest.approx(0.325)
