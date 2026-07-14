"""Static + pure-logic checks: dashboard Iceberg compact KPIs."""

from __future__ import annotations

from pathlib import Path

import pytest

APP_JS = Path("src/awareness/api/web/app.js")
INDEX_HTML = Path("src/awareness/api/web/index.html")


def test_dashboard_html_has_compact_kpis() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="kpi-dash-compact-p95"' in html
    assert 'id="kpi-dash-compact-rows"' in html
    assert "Compact p95" in html
    assert "Compacted rows" in html


def test_app_js_summarizes_compact_metrics() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "iceberg.compact_seconds" in app_js
    assert "iceberg.compacted_rows" in app_js
    assert "iceberg.compact_manifests" in app_js
    assert "icebergCompactP95" in app_js
    assert "icebergCompactedRows" in app_js
    assert "icebergCompactManifests" in app_js
    assert "kpi-dash-compact-p95" in app_js
    assert "kpi-dash-compact-rows" in app_js
    assert "function summarizeStorageObsMetrics(" in app_js


def test_summarize_compact_contract() -> None:
    """Pin aggregation: compact rows/manifests from counters; weighted p95 from hists."""
    snap = {
        "counters": [
            {"name": "iceberg.compacted_rows", "value": 100},
            {
                "name": "iceberg.compact_manifests",
                "labels": {"outcome": "ok"},
                "value": 3,
            },
            {
                "name": "iceberg.compact_manifests",
                "labels": {"outcome": "empty"},
                "value": 1,
            },
            {
                "name": "iceberg.compact_manifests",
                "labels": {"outcome": "read_error"},
                "value": 1,
            },
            {"name": "iceberg.appended_rows", "value": 50},
        ],
        "histograms": [
            {
                "name": "iceberg.compact_seconds",
                "labels": {"outcome": "ok"},
                "count": 3,
                "p95": 0.4,
            },
            {
                "name": "iceberg.compact_seconds",
                "labels": {"outcome": "empty"},
                "count": 1,
                "p95": 0.1,
            },
            {
                "name": "iceberg.append_seconds",
                "count": 9,
                "p95": 9.0,
            },
        ],
        "gauges": [],
    }
    compact_rows = sum(
        c["value"] for c in snap["counters"] if c["name"] == "iceberg.compacted_rows"
    )
    manifests = sum(
        c["value"] for c in snap["counters"] if c["name"] == "iceberg.compact_manifests"
    )
    ok = sum(
        c["value"]
        for c in snap["counters"]
        if c["name"] == "iceberg.compact_manifests"
        and (c.get("labels") or {}).get("outcome") in ("ok", "empty")
    )
    hists = [h for h in snap["histograms"] if h["name"] == "iceberg.compact_seconds"]
    total = sum(h["count"] for h in hists)
    weighted = sum(h["p95"] * h["count"] for h in hists) / total
    assert compact_rows == 100
    assert manifests == 5
    assert ok == 4
    # (0.4*3 + 0.1*1) / 4 = 0.325
    assert weighted == pytest.approx(0.325)
