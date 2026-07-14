"""Static + pure-logic checks: dashboard discovery / tail-fetch KPIs."""

from __future__ import annotations

from pathlib import Path

APP_JS = Path("src/awareness/api/web/app.js")
INDEX_HTML = Path("src/awareness/api/web/index.html")


def test_dashboard_html_has_discovery_kpis() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="kpi-dash-discover"' in html
    assert 'id="kpi-dash-tail-fetches"' in html
    assert "URLs discovered" in html
    assert "Tail fetches" in html


def test_app_js_summarizes_discovery_metrics() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "function summarizeDiscoveryMetrics(" in app_js
    assert "feeds.urls_discovered" in app_js
    assert "gdelt.urls_discovered" in app_js
    assert "gdelt.urls_enqueued" in app_js
    assert "gdelt.fetch_attempts" in app_js
    assert "tail.fetches" in app_js
    assert "kpi-dash-discover" in app_js
    assert "kpi-dash-tail-fetches" in app_js


def test_summarize_discovery_metrics_contract() -> None:
    """Pin aggregation: sum discovery counters across label series."""
    snap = {
        "counters": [
            {"name": "feeds.urls_discovered", "labels": {"channel": "rss"}, "value": 10},
            {"name": "feeds.urls_discovered", "labels": {"channel": "atom"}, "value": 5},
            {"name": "gdelt.urls_discovered", "labels": {"slot": "a"}, "value": 20},
            {"name": "gdelt.urls_enqueued", "labels": {"slot": "a"}, "value": 18},
            {
                "name": "gdelt.fetch_attempts",
                "labels": {"outcome": "ok", "status_class": "2xx"},
                "value": 3,
            },
            {
                "name": "gdelt.fetch_attempts",
                "labels": {"outcome": "missing", "status_class": "4xx"},
                "value": 1,
            },
            {"name": "tail.fetches", "labels": {"domain": "ex.com"}, "value": 7},
            {"name": "tail.fetches", "labels": {"domain": "ny.com"}, "value": 2},
            {"name": "jsonl.records_committed", "value": 99},
        ]
    }
    counters = snap["counters"]
    feeds = sum(c["value"] for c in counters if c["name"] == "feeds.urls_discovered")
    gdelt = sum(c["value"] for c in counters if c["name"] == "gdelt.urls_discovered")
    enqueued = sum(c["value"] for c in counters if c["name"] == "gdelt.urls_enqueued")
    fetch_ok = sum(
        c["value"]
        for c in counters
        if c["name"] == "gdelt.fetch_attempts" and (c.get("labels") or {}).get("outcome") == "ok"
    )
    fetch_attempts = sum(c["value"] for c in counters if c["name"] == "gdelt.fetch_attempts")
    tail = sum(c["value"] for c in counters if c["name"] == "tail.fetches")
    assert feeds == 15
    assert gdelt == 20
    assert enqueued == 18
    assert fetch_ok == 3
    assert fetch_attempts == 4
    assert tail == 9
    assert feeds + gdelt == 35
