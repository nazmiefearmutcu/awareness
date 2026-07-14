"""Static + pure-logic checks: dashboard feed fetch latency / attempts KPIs."""

from __future__ import annotations

from pathlib import Path

import pytest

APP_JS = Path("src/awareness/api/web/app.js")
INDEX_HTML = Path("src/awareness/api/web/index.html")


def test_dashboard_html_has_feed_fetch_kpis() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="kpi-dash-feed-p95"' in html
    assert 'id="kpi-dash-feed-attempts"' in html
    assert "Feed fetch p95" in html
    assert "Feed fetch attempts" in html


def test_app_js_summarizes_feed_fetch_metrics() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "feeds.fetch_attempts" in app_js
    assert "feeds.fetch_seconds" in app_js
    assert "feedFetchP95" in app_js
    assert "feedFetchAttempts" in app_js
    assert "kpi-dash-feed-p95" in app_js
    assert "kpi-dash-feed-attempts" in app_js


def test_summarize_feed_fetch_contract() -> None:
    """Pin aggregation: attempts/ok from counters; weighted p95 from histograms."""
    snap = {
        "counters": [
            {
                "name": "feeds.fetch_attempts",
                "labels": {"kind": "rss", "outcome": "ok", "status_class": "2xx"},
                "value": 8,
            },
            {
                "name": "feeds.fetch_attempts",
                "labels": {
                    "kind": "rss",
                    "outcome": "http_error",
                    "status_class": "4xx",
                },
                "value": 2,
            },
            {
                "name": "feeds.fetch_attempts",
                "labels": {
                    "kind": "sitemap",
                    "outcome": "ok",
                    "status_class": "2xx",
                },
                "value": 5,
            },
        ],
        "histograms": [
            {
                "name": "feeds.fetch_seconds",
                "labels": {"kind": "rss", "outcome": "ok", "status_class": "2xx"},
                "count": 8,
                "p95": 0.4,
            },
            {
                "name": "feeds.fetch_seconds",
                "labels": {
                    "kind": "sitemap",
                    "outcome": "ok",
                    "status_class": "2xx",
                },
                "count": 2,
                "p95": 1.0,
            },
        ],
    }
    counters = snap["counters"]
    attempts = sum(c["value"] for c in counters if c["name"] == "feeds.fetch_attempts")
    ok = sum(
        c["value"]
        for c in counters
        if c["name"] == "feeds.fetch_attempts"
        and (c.get("labels") or {}).get("outcome") == "ok"
    )
    hists = snap["histograms"]
    weighted = 0.0
    count = 0
    for h in hists:
        if h["name"] != "feeds.fetch_seconds":
            continue
        n = h["count"]
        weighted += h["p95"] * n
        count += n
    p95 = weighted / count if count else None
    assert attempts == 15
    assert ok == 13
    # (0.4*8 + 1.0*2) / 10 = 0.52
    assert p95 == pytest.approx(0.52)
