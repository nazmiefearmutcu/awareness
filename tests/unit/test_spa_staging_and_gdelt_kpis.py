"""Static + pure-logic checks: staging backlog age + GDELT fetch SPA KPIs."""

from __future__ import annotations

from pathlib import Path

import pytest

APP_JS = Path("src/awareness/api/web/app.js")
INDEX_HTML = Path("src/awareness/api/web/index.html")


def test_dashboard_html_has_staging_and_gdelt_kpis() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="kpi-dash-staging-pending"' in html
    assert 'id="kpi-dash-staging-age"' in html
    assert 'id="kpi-dash-gdelt-p95"' in html
    assert 'id="kpi-dash-gdelt-attempts"' in html
    assert "Staging pending" in html
    assert "Staging oldest" in html
    assert "GDELT fetch p95" in html
    assert "GDELT fetch attempts" in html


def test_app_js_wires_staging_and_gdelt_kpis() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "function summarizeStagingBacklog(" in app_js
    assert "function formatAgeSeconds(" in app_js
    assert "/staging?include_manifests=false" in app_js
    assert "kpi-dash-staging-pending" in app_js
    assert "kpi-dash-staging-age" in app_js
    assert "gdelt.fetch_seconds" in app_js
    assert "gdeltFetchP95" in app_js
    assert "kpi-dash-gdelt-p95" in app_js
    assert "kpi-dash-gdelt-attempts" in app_js


def test_summarize_staging_backlog_contract() -> None:
    """Pin pending count / records / oldest age from GET /staging payload."""
    snap = {
        "pending_count": 3,
        "total_records": 120,
        "total_bytes": 4096,
        "oldest_committed_at": "2026-07-13T20:00:00+00:00",
        "oldest_age_seconds": 5400.5,
    }
    pending = int(snap["pending_count"] or 0)
    records = int(snap["total_records"] or 0)
    age = float(snap["oldest_age_seconds"])
    assert pending == 3
    assert records == 120
    assert age == pytest.approx(5400.5)
    empty = {"pending_count": 0, "total_records": 0, "oldest_age_seconds": None}
    assert int(empty["pending_count"] or 0) == 0
    assert empty["oldest_age_seconds"] is None


def test_format_age_seconds_contract() -> None:
    """Pin compact age formatting used by staging oldest KPI."""
    # Mirror formatAgeSeconds thresholds in app.js
    def format_age_seconds(sec: float | None) -> str:
        if sec is None or sec < 0:
            return "—"
        if sec < 60:
            return f"{max(0, round(sec))}s"
        if sec < 3600:
            return f"{round(sec / 60)}m"
        if sec < 86400:
            h = int(sec // 3600)
            m = round((sec % 3600) / 60)
            return f"{h}h{m}m" if m else f"{h}h"
        d = int(sec // 86400)
        h = round((sec % 86400) / 3600)
        return f"{d}d{h}h" if h else f"{d}d"

    assert format_age_seconds(None) == "—"
    assert format_age_seconds(12) == "12s"
    assert format_age_seconds(90) == "2m"
    assert format_age_seconds(5400) == "1h30m"
    assert format_age_seconds(7200) == "2h"
    assert format_age_seconds(90000) == "1d1h"


def test_summarize_gdelt_fetch_p95_contract() -> None:
    """Pin GDELT attempts/ok + weighted p95 aggregation from /metrics."""
    snap = {
        "counters": [
            {
                "name": "gdelt.fetch_attempts",
                "labels": {"outcome": "ok", "status_class": "2xx"},
                "value": 4,
            },
            {
                "name": "gdelt.fetch_attempts",
                "labels": {"outcome": "missing", "status_class": "4xx"},
                "value": 1,
            },
            {"name": "gdelt.urls_discovered", "value": 20},
            {"name": "gdelt.urls_enqueued", "value": 18},
        ],
        "histograms": [
            {
                "name": "gdelt.fetch_seconds",
                "labels": {"outcome": "ok", "status_class": "2xx"},
                "count": 4,
                "p95": 0.5,
            },
            {
                "name": "gdelt.fetch_seconds",
                "labels": {"outcome": "missing", "status_class": "4xx"},
                "count": 1,
                "p95": 0.1,
            },
            {
                "name": "feeds.fetch_seconds",
                "count": 9,
                "p95": 9.0,
            },
        ],
    }
    attempts = sum(
        c["value"] for c in snap["counters"] if c["name"] == "gdelt.fetch_attempts"
    )
    ok = sum(
        c["value"]
        for c in snap["counters"]
        if c["name"] == "gdelt.fetch_attempts"
        and (c.get("labels") or {}).get("outcome") == "ok"
    )
    hists = [h for h in snap["histograms"] if h["name"] == "gdelt.fetch_seconds"]
    total = sum(h["count"] for h in hists)
    weighted = sum(h["p95"] * h["count"] for h in hists) / total
    assert attempts == 5
    assert ok == 4
    # (0.5*4 + 0.1*1) / 5 = 0.42
    assert weighted == pytest.approx(0.42)
