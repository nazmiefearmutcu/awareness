"""Static + pure-logic checks: dashboard Feed health band summarizer."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path("src/awareness/api/web/app.js")
INDEX_HTML = Path("src/awareness/api/web/index.html")
STYLE_CSS = Path("src/awareness/api/web/style.css")

NODE = shutil.which("node")


def _extract_function(name: str, source: str) -> str:
    """Brace-counting extractor: returns the full `function <name>(...) {...}`."""
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    i = brace
    while i < len(source):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
        i += 1
    raise AssertionError(f"could not extract function {name}")


def _run_summarize_feed_health(snapshot: dict) -> dict:
    """Execute the real summarizer from app.js in node against *snapshot*."""
    if NODE is None:
        pytest.skip("node not available")
    app_js = APP_JS.read_text(encoding="utf-8")
    fn = _extract_function("summarizeFeedHealth", app_js)
    harness = (
        "const snap = " + json.dumps(snapshot) + ";\n"
        + fn + "\n"
        + "console.log(JSON.stringify(summarizeFeedHealth(snap)));\n"
    )
    proc = subprocess.run(
        [NODE, "-e", harness], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_dashboard_html_has_feed_health_band() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="feed-health-band"' in html
    assert 'id="feed-health-score"' in html
    assert 'id="feed-health-kpis"' in html
    assert "Feed health" in html


def test_app_js_has_feed_health_summarizer() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "function summarizeFeedHealth(" in app_js
    assert "function renderFeedHealth(" in app_js
    for needle in (
        "feeds.fetch_attempts",
        "retry_exhausted",
        "feeds.fetch_non_200",
        "feeds.fetch_seconds",
        "tail.fetch_non_200",
    ):
        assert needle in app_js
    # The dashboard refresh path must call it with the /metrics snapshot.
    assert "renderFeedHealth(summarizeFeedHealth(metricsSnap))" in app_js


def test_feed_health_score_formula_constants() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    fn = _extract_function("summarizeFeedHealth", app_js)
    assert "100 - 10 * errorRate - 5 * non200Rate" in fn
    assert "Math.max(0, Math.min(100" in fn


def test_feed_health_styles_present() -> None:
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".feed-health-score" in css
    assert ".feed-health .kpi-grid" in css


# Synthetic /metrics snapshot mirroring the metrics registry shape.
SNAPSHOT = {
    "counters": [
        {"name": "feeds.fetch_attempts", "labels": {"kind": "rss", "outcome": "ok"}, "value": 90},
        {"name": "feeds.fetch_attempts", "labels": {"kind": "rss", "outcome": "http_error"}, "value": 5},
        {"name": "feeds.fetch_attempts", "labels": {"kind": "sitemap", "outcome": "retry_exhausted"}, "value": 5},
        {"name": "feeds.fetch_non_200", "labels": {"kind": "rss"}, "value": 10},
        {"name": "tail.fetch_non_200", "labels": {"domain": "example.com"}, "value": 3},
    ],
    "histograms": [
        {"name": "feeds.fetch_seconds", "labels": {"kind": "rss", "outcome": "ok"}, "count": 90, "p95": 0.5},
        {"name": "feeds.fetch_seconds", "labels": {"kind": "sitemap", "outcome": "ok"}, "count": 10, "p95": 1.5},
    ],
}


def test_summarize_feed_health_outcome_buckets() -> None:
    out = _run_summarize_feed_health(SNAPSHOT)
    assert out["attempts"] == 100
    assert out["ok"] == 90
    assert out["error"] == 5  # http_error + transport_error + blocked → error bucket
    assert out["retryExhausted"] == 5
    assert out["non200"] == 10
    assert out["tailNon200"] == 3
    # (0.5*90 + 1.5*10) / 100 = 0.6
    assert out["p95Sec"] == pytest.approx(0.6)
    assert out["samples"] == 100


def test_summarize_feed_health_score_formula() -> None:
    # error_rate = 5%, non200_rate = 10% → 100 - 10*5 - 5*10 = 0 (clamped floor).
    assert _run_summarize_feed_health(SNAPSHOT)["score"] == 0

    # error_rate = 2%, non200_rate = 2% → 100 - 20 - 10 = 70.
    mild = {
        "counters": [
            {"name": "feeds.fetch_attempts", "labels": {"outcome": "ok"}, "value": 96},
            {"name": "feeds.fetch_attempts", "labels": {"outcome": "http_error"}, "value": 2},
            {"name": "feeds.fetch_attempts", "labels": {"outcome": "retry_exhausted"}, "value": 2},
            {"name": "feeds.fetch_non_200", "labels": {}, "value": 2},
        ],
        "histograms": [],
    }
    assert _run_summarize_feed_health(mild)["score"] == 70

    # Clean run → 100.
    clean = {
        "counters": [
            {"name": "feeds.fetch_attempts", "labels": {"outcome": "ok"}, "value": 40},
            {"name": "feeds.fetch_non_200", "labels": {}, "value": 0},
        ],
        "histograms": [],
    }
    assert _run_summarize_feed_health(clean)["score"] == 100

    # No attempts → no score (badge shows "—"), not a bogus 100.
    empty = {"counters": [], "histograms": []}
    out = _run_summarize_feed_health(empty)
    assert out["attempts"] == 0
    assert out["score"] is None
    # And the empty-object guard never throws.
    assert _run_summarize_feed_health(None)["score"] is None
