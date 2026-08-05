"""Static + pure-logic checks: SPA Alert trend mini-chart (Alerts view).

Follows the ``test_spa_*`` pattern: HTML band structure above the firings
log, app.js wiring (the /alerts/firings payload feeds the pure
``alertTrendDaily`` aggregator, rendered through the shared bar chart), the
aggregator executed in node against real firing lists, and the no-innerHTML
rule for the new region.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
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


def _alerts_slice(app_js: str) -> str:
    start = app_js.index("// ── Alerts")
    end = app_js.index("// ── Settings")
    return app_js[start:end]


def _day_iso(days_ago: int) -> str:
    """UTC calendar day `days_ago` before now (matches the app's isoDay())."""
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _run_alert_trend_daily(firings: list) -> list:
    """Execute the real alertTrendDaily aggregator from app.js in node."""
    if NODE is None:
        pytest.skip("node not available")
    app_js = APP_JS.read_text(encoding="utf-8")
    fn = _extract_function("alertTrendDaily", app_js)
    harness = (
        # Mirrors the app.js helper alertTrendDaily depends on.
        "const isoDay = (d) => new Date(d).toISOString().slice(0, 10);\n"
        "const firings = " + json.dumps(firings) + ";\n"
        + fn + "\n"
        + "console.log(JSON.stringify(alertTrendDaily(firings)));\n"
    )
    proc = subprocess.run(  # noqa: S603 - harness is our own extracted fn + JSON data
        [NODE, "-e", harness], capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_alert_trend_band_in_html_above_firings_log() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "Alert trend" in html
    for needle in (
        'id="al-trend-title"',
        'id="al-trend-meta"',
        'id="al-trend-chart"',
    ):
        assert needle in html, f"missing {needle}"
    # The band sits above the firings log (and below the test panel).
    assert html.index('id="al-test-panel"') < html.index('id="al-trend-chart"')
    assert html.index('id="al-trend-chart"') < html.index('id="al-firings-title"')


def test_alert_trend_js_present_and_endpoint_used() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    alerts = _alerts_slice(app_js)
    assert "function alertTrendDaily(" in alerts
    assert "function renderAlertTrend(" in alerts
    assert "renderBarChart(box, daily, { color: \"#d97757\" })" in alerts
    assert 'api("/alerts/firings?limit=50")' in alerts


def test_alert_trend_wired_into_alerts_loads() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    # Both the full view load and the firings-log refresh feed the chart.
    assert app_js.count("renderAlertTrend(alertTrendDaily(firings || []))") >= 2


def test_alert_trend_region_no_innerhtml() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    alerts = _alerts_slice(app_js)
    assert "innerHTML" not in alerts


def test_alert_trend_styles_present() -> None:
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".al-trend .an-chart" in css


# ── pure daily aggregator (executed in node against the real function) ──

def test_alert_trend_daily_aggregates_firings_by_day() -> None:
    # 50 firings across 3 days → 14 buckets, 3 non-zero, correct counts.
    d0, d1, d2 = _day_iso(0), _day_iso(1), _day_iso(2)
    firings = (
        [{"fired_at": d0 + "T08:00:00Z", "rule_name": "btc"} for _ in range(20)]
        + [{"fired_at": d1 + "T12:30:00Z", "rule_name": "eth"} for _ in range(20)]
        + [{"fired_at": d2 + "T18:45:00Z", "rule_name": "sol"} for _ in range(10)]
    )
    out = _run_alert_trend_daily(firings)
    assert len(out) == 14
    assert sum(b["count"] for b in out) == 50
    non_zero = {b["ts"]: b["count"] for b in out if b["count"] > 0}
    assert non_zero == {d0: 20, d1: 20, d2: 10}
    # Buckets are ordered oldest → newest and carry day-precision timestamps.
    assert all(b["ts"][:10] == b["ts"] and len(b["ts"]) == 10 for b in out)


def test_alert_trend_daily_zero_fills_empty() -> None:
    for empty in ([], None):
        out = _run_alert_trend_daily(empty)
        assert len(out) == 14
        assert all(b["count"] == 0 for b in out)


def test_alert_trend_daily_ignores_outside_window_and_bad_dates() -> None:
    firings = [
        {"fired_at": _day_iso(30) + "T09:00:00Z"},
        {"fired_at": None},
        {"fired_at": "not-a-date"},
        {},
    ]
    out = _run_alert_trend_daily(firings)
    assert len(out) == 14
    assert all(b["count"] == 0 for b in out)
