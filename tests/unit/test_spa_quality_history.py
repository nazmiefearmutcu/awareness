"""Static + pure-logic checks: SPA Quality history band (Dashboard view).

Follows the ``test_spa_*`` pattern: HTML band structure, app.js wiring, the
pure ``qualityHistoryRows`` table builder executed in node against fake
points, the 12-tick refresh guard (mirroring the saved band), and the
no-innerHTML rule for the new region.
"""

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


def _quality_slice(app_js: str) -> str:
    start = app_js.index("// ── Quality history band")
    end = app_js.index("// ── Dashboard saved widgets", start)
    return app_js[start:end]


def _run_quality_history_rows(points: list) -> list:
    """Execute the real qualityHistoryRows table builder from app.js in node."""
    if NODE is None:
        pytest.skip("node not available")
    app_js = APP_JS.read_text(encoding="utf-8")
    fn = _extract_function("qualityHistoryRows", app_js)
    harness = (
        "const points = " + json.dumps(points) + ";\n"
        + fn + "\n"
        + "console.log(JSON.stringify(qualityHistoryRows(points)));\n"
    )
    proc = subprocess.run(  # noqa: S603 - harness is our own extracted fn + JSON data
        [NODE, "-e", harness], capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_quality_band_in_html_under_feed_health() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "Quality history" in html
    for needle in (
        'id="dash-quality-band"',
        'id="dash-quality-meta"',
        'id="dash-quality-grid"',
        'id="dash-quality-dup"',
        'id="dash-quality-domains"',
        'id="dash-quality-table"',
        'id="dash-quality-body"',
    ):
        assert needle in html, f"missing {needle}"
    # Band sits under the feed-health band and above the saved band.
    assert html.index('id="feed-health-band"') < html.index('id="dash-quality-band"')
    assert html.index('id="dash-quality-band"') < html.index('id="dash-saved-band"')


def test_quality_js_present_and_endpoint_used() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    q = _quality_slice(app_js)
    assert "function qualityHistoryRows(" in q
    assert "function renderQualityHistory(" in q
    assert "async function refreshDashQuality(" in q
    assert 'api("/qualityx/history?days=30")' in q


def test_quality_refresh_hooked_into_dashboard_cadence() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    q = _quality_slice(app_js)
    # Same 12-tick rebuild guard as the saved band (60 s at the 5 s cadence).
    assert "dashQualityTick % 12 !== 0" in q
    assert "if (sig === dashQualitySig && dashQualityTick % 12 !== 0) return;" in q
    # refreshDashboard drives it, non-fatal on failure.
    assert "void refreshDashQuality();" in app_js
    assert "// Corpus-quality history band (same cadence guard)." in app_js


def test_quality_dup_chart_uses_percents_via_bar_chart() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    q = _quality_slice(app_js)
    # duplicate_ratio is multiplied by 100 to display as a percentage.
    assert "(p.duplicate_ratio || 0) * 100" in q
    assert "renderBarChart(dupBox" in q
    assert "renderBarChart(domBox" in q


def test_quality_region_no_innerhtml() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    q = _quality_slice(app_js)
    assert "innerHTML" not in q
    # Rows are built via table APIs with textContent — never HTML strings.
    assert "body.insertRow()" in q
    assert "row.insertCell().textContent" in q


def test_quality_styles_present() -> None:
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".dash-quality-grid" in css
    assert ".dash-quality-grid .an-chart" in css


# ── pure table builder (executed in node against the real function) ──

def test_quality_rows_formats_ratios_as_percents() -> None:
    rows = _run_quality_history_rows(
        [
            {"ts": "2026-08-01", "total": 100, "duplicate_ratio": 0.123, "near_duplicate_ratio": 0.04, "new_domains": 3},
            {"ts": "2026-08-02", "total": 250, "duplicate_ratio": 0.5, "near_duplicate_ratio": 0.0, "new_domains": 0},
        ]
    )
    assert rows[0] == ["2026-08-01", 100, "12.3%", "4.0%", 3]
    assert rows[1] == ["2026-08-02", 250, "50.0%", "0.0%", 0]


def test_quality_rows_tolerates_nullable_fields() -> None:
    rows = _run_quality_history_rows(
        [{"ts": "2026-07-31", "total": None, "duplicate_ratio": None, "near_duplicate_ratio": None, "new_domains": None}]
    )
    assert rows[0] == ["2026-07-31", "—", "—", "—", "—"]


def test_quality_rows_handles_empty_points() -> None:
    assert _run_quality_history_rows([]) == []
    assert _run_quality_history_rows(None) == []
