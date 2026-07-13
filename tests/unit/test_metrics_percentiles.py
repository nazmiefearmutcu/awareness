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
