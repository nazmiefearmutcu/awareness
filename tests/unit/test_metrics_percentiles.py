from __future__ import annotations

import random

from awareness.obs.metrics import MetricsRegistry, _Histogram


def test_percentiles_on_full_sample() -> None:
    h = _Histogram(max_samples=1000)
    for v in range(1, 101):
        h.observe(float(v))
    d = h.as_dict()
    assert d["count"] == 100
    assert d["min"] == 1.0
    assert d["max"] == 100.0
    assert 49.0 <= d["p50"] <= 52.0
    assert 94.0 <= d["p95"] <= 96.0
    assert 98.0 <= d["p99"] <= 100.0


def test_reservoir_is_unbiased_after_capacity() -> None:
    random.seed(12345)
    h = _Histogram(max_samples=256)
    for _ in range(256):
        h.observe(0.0)
    for _ in range(10_000):
        h.observe(100.0)
    d = h.as_dict()
    assert d["count"] == 10_256
    assert d["p50"] == 100.0, "reservoir sampling must not be biased toward the first 256 values"


def test_empty_histogram_percentiles_are_zero() -> None:
    h = _Histogram()
    d = h.as_dict()
    assert d["p50"] == 0.0 and d["p95"] == 0.0 and d["p99"] == 0.0


def test_counter_sum_aggregates_labels() -> None:
    m = MetricsRegistry()
    m.inc("tail.fetch_skipped_seen", labels={"domain": "a.example"})
    m.inc("tail.fetch_skipped_seen", value=2.0, labels={"domain": "b.example"})
    m.inc("other", value=99.0)
    assert m.counter_sum("tail.fetch_skipped_seen") == 3.0
    assert m.counter_sum("missing") == 0.0


def test_render_prometheus_counters_gauges_histograms() -> None:
    m = MetricsRegistry()
    m.inc("tail.fetch_skipped_seen", labels={"domain": "a.example"})
    m.inc("tail.fetch_skipped_seen", value=2.0, labels={"domain": "b.example"})
    m.set("queue.depth", 7.0, labels={"kind": "tail"})
    m.observe("http.latency_ms", 12.5, labels={"route": "/search"})
    m.observe("http.latency_ms", 40.0, labels={"route": "/search"})
    text = m.render_prometheus()
    assert "awareness_uptime_seconds" in text
    assert "tail_fetch_skipped_seen_total" in text
    assert 'domain="a.example"' in text
    assert "queue_depth" in text
    assert 'kind="tail"' in text
    assert "http_latency_ms_count" in text
    assert "http_latency_ms_sum" in text
    assert 'quantile="0.5"' in text
    # Sanitized name: dots → underscores; trailing newline.
    assert text.endswith("\n")
    assert "# TYPE" in text


def test_prom_metric_name_sanitizes() -> None:
    assert MetricsRegistry._prom_metric_name("tail.fetch.skipped") == "tail_fetch_skipped"
    assert MetricsRegistry._prom_metric_name("9bad") == "m_9bad"
    assert MetricsRegistry._prom_metric_name("ok_name") == "ok_name"


def test_prom_label_value_escapes() -> None:
    assert MetricsRegistry._prom_label_value('a"b\\c\nd') == 'a\\"b\\\\c\\nd'


def test_snapshot_and_prometheus_prefix_filter() -> None:
    m = MetricsRegistry()
    m.inc("http.fetch_attempts", value=1.0)
    m.inc("gdelt.urls_discovered", value=2.0)
    m.set("queue.depth", 3.0)
    m.observe("http.fetch_seconds", 0.1)
    m.observe("gdelt.fetch_seconds", 0.5)

    snap = m.snapshot(prefix="http.")
    assert snap["prefix"] == "http."
    assert {c["name"] for c in snap["counters"]} == {"http.fetch_attempts"}
    assert {h["name"] for h in snap["histograms"]} == {"http.fetch_seconds"}
    assert snap["gauges"] == []  # queue.depth filtered out

    full = m.snapshot()
    assert "prefix" not in full
    assert len(full["counters"]) >= 2

    prom = m.render_prometheus(prefix="gdelt.")
    assert "gdelt_urls_discovered_total" in prom or "gdelt_urls_discovered" in prom
    assert "http_fetch_attempts" not in prom
    assert "awareness_uptime_seconds" in prom

    filtered = MetricsRegistry.filter_snapshot(full, prefix="queue.")
    assert all(g["name"].startswith("queue.") for g in filtered["gauges"])
    assert filtered["prefix"] == "queue."
