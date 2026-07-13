"""Static + pure-logic checks: dashboard WET parse + JSONL sync SPA KPIs."""

from __future__ import annotations

from pathlib import Path

import pytest

APP_JS = Path("src/awareness/api/web/app.js")
INDEX_HTML = Path("src/awareness/api/web/index.html")


def test_dashboard_html_has_wet_parse_and_jsonl_sync_kpis() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="kpi-dash-wet-parse-p95"' in html
    assert 'id="kpi-dash-wet-dl-attempts"' in html
    assert 'id="kpi-dash-wet-seen"' in html
    assert 'id="kpi-dash-jsonl-syncs"' in html
    assert 'id="kpi-dash-jsonl-sync-p95"' in html
    assert 'id="kpi-dash-jsonl-orphans"' in html
    assert "WET parse p95" in html
    assert "WET download attempts" in html
    assert "WET records seen" in html
    assert "JSONL mid-chunk syncs" in html
    assert "JSONL sync p95" in html
    assert "JSONL orphans recovered" in html


def test_app_js_wires_wet_parse_and_jsonl_sync_kpis() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "function summarizeWetParseMetrics(" in app_js
    assert "cc_wet.records_seen" in app_js
    assert "cc_wet.shard_parse_emitted" in app_js
    assert "cc_wet.shard_download_attempts" in app_js
    assert "cc_wet.shard_parse_seconds" in app_js
    assert "cc_wet.shard_download_seconds" in app_js
    assert "jsonl.syncs" in app_js
    assert "jsonl.sync_seconds" in app_js
    assert "jsonl.orphans_recovered" in app_js
    assert "jsonl.orphans_removed" in app_js
    assert "jsonl.open_records" in app_js
    assert "kpi-dash-wet-parse-p95" in app_js
    assert "kpi-dash-wet-dl-attempts" in app_js
    assert "kpi-dash-wet-seen" in app_js
    assert "kpi-dash-jsonl-syncs" in app_js
    assert "kpi-dash-jsonl-sync-p95" in app_js
    assert "kpi-dash-jsonl-orphans" in app_js


def test_summarize_wet_parse_metrics_contract() -> None:
    """Pin download/parse aggregation + weighted p95 across crawl labels."""
    snap = {
        "counters": [
            {
                "name": "cc_wet.records_seen",
                "labels": {"crawl_id": "c1"},
                "value": 100,
            },
            {
                "name": "cc_wet.records_seen",
                "labels": {"crawl_id": "c2"},
                "value": 50,
            },
            {
                "name": "cc_wet.shard_parse_emitted",
                "labels": {"crawl_id": "c1"},
                "value": 40,
            },
            {
                "name": "cc_wet.shard_parse_emitted",
                "labels": {"crawl_id": "c2"},
                "value": 10,
            },
            {
                "name": "cc_wet.shard_download_attempts",
                "labels": {"crawl_id": "c1", "outcome": "cache_hit"},
                "value": 3,
            },
            {
                "name": "cc_wet.shard_download_attempts",
                "labels": {"crawl_id": "c1", "outcome": "ok"},
                "value": 2,
            },
            {
                "name": "cc_wet.shard_download_attempts",
                "labels": {"crawl_id": "c2", "outcome": "error"},
                "value": 1,
            },
            {"name": "fineweb.rows_admitted", "value": 99},
        ],
        "histograms": [
            {
                "name": "cc_wet.shard_parse_seconds",
                "labels": {"crawl_id": "c1"},
                "count": 3,
                "p95": 0.4,
            },
            {
                "name": "cc_wet.iter_parse_seconds",
                "labels": {"crawl_id": "c2"},
                "count": 1,
                "p95": 0.2,
            },
            {
                "name": "cc_wet.shard_download_seconds",
                "labels": {"outcome": "ok"},
                "count": 2,
                "p95": 1.0,
            },
            {
                "name": "cc_wet.shard_download_seconds",
                "labels": {"outcome": "cache_hit"},
                "count": 3,
                "p95": 0.01,
            },
            {"name": "fineweb.load_seconds", "count": 9, "p95": 9.0},
        ],
    }
    counters = snap["counters"]
    records_seen = sum(c["value"] for c in counters if c["name"] == "cc_wet.records_seen")
    parse_emitted = sum(
        c["value"] for c in counters if c["name"] == "cc_wet.shard_parse_emitted"
    )
    download_attempts = sum(
        c["value"] for c in counters if c["name"] == "cc_wet.shard_download_attempts"
    )
    download_cache_hits = sum(
        c["value"]
        for c in counters
        if c["name"] == "cc_wet.shard_download_attempts"
        and (c.get("labels") or {}).get("outcome") == "cache_hit"
    )
    download_ok = sum(
        c["value"]
        for c in counters
        if c["name"] == "cc_wet.shard_download_attempts"
        and (c.get("labels") or {}).get("outcome") in ("ok", "cache_hit")
    )
    parse_hists = [
        h
        for h in snap["histograms"]
        if h["name"] in ("cc_wet.shard_parse_seconds", "cc_wet.iter_parse_seconds")
    ]
    parse_total = sum(h["count"] for h in parse_hists)
    parse_weighted = sum(h["p95"] * h["count"] for h in parse_hists) / parse_total
    dl_hists = [h for h in snap["histograms"] if h["name"] == "cc_wet.shard_download_seconds"]
    dl_total = sum(h["count"] for h in dl_hists)
    dl_weighted = sum(h["p95"] * h["count"] for h in dl_hists) / dl_total

    assert records_seen == 150
    assert parse_emitted == 50
    assert download_attempts == 6
    assert download_cache_hits == 3
    assert download_ok == 5
    # (0.4*3 + 0.2*1) / 4 = 0.35
    assert parse_weighted == pytest.approx(0.35)
    # (1.0*2 + 0.01*3) / 5 = 0.406
    assert dl_weighted == pytest.approx(0.406)


def test_summarize_jsonl_sync_metrics_contract() -> None:
    """Pin mid-chunk sync / orphan / open-gauge aggregation for storage KPIs."""
    snap = {
        "gauges": [
            {"name": "jsonl.open_records", "value": 12},
            {"name": "jsonl.open_bytes", "value": 4096},
            {"name": "robots.cache.hit_ratio", "value": 0.5},
        ],
        "counters": [
            {"name": "jsonl.syncs", "labels": {"outcome": "ok"}, "value": 8},
            {"name": "jsonl.syncs", "labels": {"outcome": "error"}, "value": 1},
            {"name": "jsonl.orphans_recovered", "value": 2},
            {"name": "jsonl.orphans_removed", "value": 3},
            {"name": "jsonl.records_committed", "value": 50},
            {"name": "jsonl.chunks_committed", "value": 2},
        ],
        "histograms": [
            {"name": "jsonl.sync_seconds", "count": 8, "p95": 0.002},
            {"name": "jsonl.sync_seconds", "count": 1, "p95": 0.01},
            {"name": "jsonl.commit_seconds", "count": 2, "p95": 0.05},
        ],
    }
    gauges = {g["name"]: g["value"] for g in snap["gauges"]}
    assert gauges["jsonl.open_records"] == 12
    counters = snap["counters"]
    syncs = sum(c["value"] for c in counters if c["name"] == "jsonl.syncs")
    sync_ok = sum(
        c["value"]
        for c in counters
        if c["name"] == "jsonl.syncs" and (c.get("labels") or {}).get("outcome") == "ok"
    )
    orphans_recovered = sum(
        c["value"] for c in counters if c["name"] == "jsonl.orphans_recovered"
    )
    orphans_removed = sum(
        c["value"] for c in counters if c["name"] == "jsonl.orphans_removed"
    )
    sync_hists = [h for h in snap["histograms"] if h["name"] == "jsonl.sync_seconds"]
    sync_total = sum(h["count"] for h in sync_hists)
    sync_weighted = sum(h["p95"] * h["count"] for h in sync_hists) / sync_total

    assert syncs == 9
    assert sync_ok == 8
    assert orphans_recovered == 2
    assert orphans_removed == 3
    # (0.002*8 + 0.01*1) / 9 ≈ 0.002888...
    assert sync_weighted == pytest.approx((0.002 * 8 + 0.01 * 1) / 9)
