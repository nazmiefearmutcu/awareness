"""Static + pure-logic checks: robots hit ratio + Iceberg rows dashboard KPIs."""

from __future__ import annotations

from pathlib import Path

APP_JS = Path("src/awareness/api/web/app.js")
INDEX_HTML = Path("src/awareness/api/web/index.html")


def test_dashboard_html_has_storage_obs_kpis() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="kpi-dash-robots-hit"' in html
    assert 'id="kpi-dash-iceberg-rows"' in html
    assert 'id="kpi-dash-jsonl-records"' in html
    assert "Robots cache hit" in html
    assert "Iceberg rows" in html
    assert "JSONL records" in html


def test_app_js_summarizes_storage_obs_metrics() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "function summarizeStorageObsMetrics(" in app_js
    assert "function formatHitRatio(" in app_js
    assert "robots.cache.hit_ratio" in app_js
    assert "robots.cache.resolutions" in app_js
    assert "iceberg.appended_rows" in app_js
    assert "iceberg.append_batches" in app_js
    assert "iceberg.append_seconds" in app_js
    assert "jsonl.records_committed" in app_js
    assert "jsonl.chunks_committed" in app_js
    assert "jsonl.commit_seconds" in app_js
    assert "kpi-dash-robots-hit" in app_js
    assert "kpi-dash-iceberg-rows" in app_js
    assert "kpi-dash-jsonl-records" in app_js


def test_summarize_storage_obs_metrics_contract() -> None:
    """Pin expected aggregation: gauges for robots, sum counters for iceberg/jsonl."""
    snap = {
        "gauges": [
            {"name": "robots.cache.hit_ratio", "value": 0.75},
            {"name": "robots.cache.resolutions", "value": 40},
            {"name": "other", "value": 1},
        ],
        "counters": [
            {"name": "iceberg.appended_rows", "value": 120},
            {"name": "iceberg.append_batches", "labels": {"outcome": "ok"}, "value": 3},
            {"name": "iceberg.append_batches", "labels": {"outcome": "ok"}, "value": 1},
            {"name": "jsonl.records_committed", "value": 50},
            {"name": "jsonl.chunks_committed", "value": 2},
            {"name": "http.fetch_attempts", "value": 99},
        ],
        "histograms": [
            {
                "name": "iceberg.append_seconds",
                "labels": {"outcome": "ok"},
                "count": 3,
                "p95": 0.05,
            },
            {
                "name": "iceberg.append_seconds",
                "labels": {"outcome": "error"},
                "count": 1,
                "p95": 0.2,
            },
            {
                "name": "jsonl.commit_seconds",
                "count": 2,
                "p95": 0.01,
            },
        ],
    }
    gauges = {g["name"]: g["value"] for g in snap["gauges"]}
    assert gauges["robots.cache.hit_ratio"] == 0.75
    assert gauges["robots.cache.resolutions"] == 40
    iceberg_rows = sum(
        c["value"] for c in snap["counters"] if c["name"] == "iceberg.appended_rows"
    )
    iceberg_batches = sum(
        c["value"] for c in snap["counters"] if c["name"] == "iceberg.append_batches"
    )
    jsonl_records = sum(
        c["value"] for c in snap["counters"] if c["name"] == "jsonl.records_committed"
    )
    jsonl_chunks = sum(
        c["value"] for c in snap["counters"] if c["name"] == "jsonl.chunks_committed"
    )
    assert iceberg_rows == 120
    assert iceberg_batches == 4
    assert jsonl_records == 50
    assert jsonl_chunks == 2
    hists = [h for h in snap["histograms"] if h["name"] == "iceberg.append_seconds"]
    total = sum(h["count"] for h in hists)
    weighted = sum(h["p95"] * h["count"] for h in hists) / total
    # (0.05*3 + 0.2*1) / 4 = 0.0875
    assert abs(weighted - 0.0875) < 1e-9
    jh = [h for h in snap["histograms"] if h["name"] == "jsonl.commit_seconds"]
    assert sum(h["count"] for h in jh) == 2


def test_format_hit_ratio_contract() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "Math.round(ratio * 100)" in app_js
    assert 'return "—"' in app_js or "return '—'" in app_js
