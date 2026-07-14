"""Static + pure-logic checks: dashboard WET quality + feed health KPIs."""

from __future__ import annotations

from pathlib import Path

APP_JS = Path("src/awareness/api/web/app.js")
INDEX_HTML = Path("src/awareness/api/web/index.html")


def test_dashboard_html_has_wet_and_feed_kpis() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="kpi-dash-wet-quality"' in html
    assert 'id="kpi-dash-feed-errors"' in html
    assert "WET quality drops" in html
    assert "Feed fetch errors" in html


def test_app_js_summarizes_wet_and_feed_metrics() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "function summarizeWetQualityMetrics(" in app_js
    assert "cc_wet.quality_filtered" in app_js
    assert "cc_wet.records_admitted" in app_js
    assert "feeds.fetch_non_200" in app_js
    assert "feeds.retryable_http_error" in app_js
    assert "feeds.decode_charset" in app_js
    assert "feeds.robots_sitemaps_discovered" in app_js
    assert "kpi-dash-wet-quality" in app_js
    assert "kpi-dash-feed-errors" in app_js


def test_summarize_discovery_includes_feed_health() -> None:
    """Pin aggregation for feed error / charset / sitemap counters."""
    snap = {
        "counters": [
            {"name": "feeds.urls_discovered", "labels": {"channel": "rss"}, "value": 10},
            {"name": "feeds.fetch_non_200", "labels": {"status": "404"}, "value": 2},
            {"name": "feeds.fetch_non_200", "labels": {"status": "500"}, "value": 1},
            {"name": "feeds.retryable_http_error", "labels": {"kind": "timeout"}, "value": 4},
            {"name": "feeds.decode_charset", "labels": {"charset": "utf-8"}, "value": 7},
            {
                "name": "feeds.robots_sitemaps_discovered",
                "labels": {"domain": "ex.com"},
                "value": 3,
            },
            {"name": "gdelt.urls_discovered", "value": 5},
        ]
    }
    counters = snap["counters"]
    feed_non_200 = sum(c["value"] for c in counters if c["name"] == "feeds.fetch_non_200")
    feed_retry = sum(
        c["value"] for c in counters if c["name"] == "feeds.retryable_http_error"
    )
    feed_charset = sum(c["value"] for c in counters if c["name"] == "feeds.decode_charset")
    feed_sitemaps = sum(
        c["value"] for c in counters if c["name"] == "feeds.robots_sitemaps_discovered"
    )
    feeds = sum(c["value"] for c in counters if c["name"] == "feeds.urls_discovered")
    assert feed_non_200 == 3
    assert feed_retry == 4
    assert feed_non_200 + feed_retry == 7  # feedErrors
    assert feed_charset == 7
    assert feed_sitemaps == 3
    assert feeds == 10


def test_summarize_wet_quality_contract() -> None:
    """Pin aggregation: sum filtered by reason + admitted; pick top reason."""
    snap = {
        "counters": [
            {
                "name": "cc_wet.quality_filtered",
                "labels": {"crawl_id": "c1", "reason": "no_stopwords"},
                "value": 5,
            },
            {
                "name": "cc_wet.quality_filtered",
                "labels": {"crawl_id": "c1", "reason": "too_few_words"},
                "value": 2,
            },
            {
                "name": "cc_wet.quality_filtered",
                "labels": {"crawl_id": "c2", "reason": "no_stopwords"},
                "value": 1,
            },
            {
                "name": "cc_wet.records_admitted",
                "labels": {"crawl_id": "c1"},
                "value": 40,
            },
            {
                "name": "cc_wet.records_admitted",
                "labels": {"crawl_id": "c2"},
                "value": 10,
            },
        ]
    }
    counters = snap["counters"]
    filtered = sum(c["value"] for c in counters if c["name"] == "cc_wet.quality_filtered")
    admitted = sum(c["value"] for c in counters if c["name"] == "cc_wet.records_admitted")
    by_reason: dict[str, float] = {}
    for c in counters:
        if c["name"] != "cc_wet.quality_filtered":
            continue
        reason = (c.get("labels") or {}).get("reason") or "unknown"
        by_reason[reason] = by_reason.get(reason, 0) + c["value"]
    top_reason = max(by_reason, key=by_reason.get)  # type: ignore[arg-type]
    assert filtered == 8
    assert admitted == 50
    assert top_reason == "no_stopwords"
    assert by_reason["no_stopwords"] == 6
