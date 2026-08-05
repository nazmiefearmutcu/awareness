"""Static + node-exec checks: quality latest-point mini-card (Dashboard view).

Follows the ``test_spa_quality_history`` pattern: HTML card presence in the
quality band, pure ``latestQualityPoint`` / ``qualityMiniStats`` executed in
node against the real functions from app.js (reusing the table's pct
formatting), a node run of the real ``renderQualityMini`` against a stub DOM
(KPI texts + 14-point sparkline), the re-render hook inside the band's
12-tick guarded refresh, and the no-innerHTML rule for the region.
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


def _run_js(harness: str) -> str:
    if NODE is None:
        pytest.skip("node not available")
    proc = subprocess.run(  # noqa: S603 - harness is our own extracted fns + JSON data
        [NODE, "-e", harness], capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip().splitlines()[-1]


def _run_pure(fn_name: str, *args) -> str:
    """Execute a single extracted pure function from app.js in node."""
    app_js = APP_JS.read_text(encoding="utf-8")
    fn = _extract_function(fn_name, app_js)
    call = f"{fn_name}({', '.join(json.dumps(a) for a in args)})"
    return _run_js(fn + "\nconsole.log(JSON.stringify(" + call + "));\n")


def _run_quality_mini_stats(*args) -> str:
    """Execute qualityMiniStats with its qualityPct helper in node."""
    app_js = APP_JS.read_text(encoding="utf-8")
    fns = (
        _extract_function("qualityPct", app_js) + "\n"
        + _extract_function("qualityMiniStats", app_js)
    )
    call = f"qualityMiniStats({', '.join(json.dumps(a) for a in args)})"
    return _run_js(fns + "\nconsole.log(JSON.stringify(" + call + "));\n")


# ── card presence in HTML ─────────────────────────────────────

def test_quality_mini_card_in_html() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    for needle in (
        'id="dash-quality-mini"',
        'id="dash-quality-mini-kpis"',
        'id="qmini-total"',
        'id="qmini-dup"',
        'id="qmini-near"',
        'id="qmini-capture"',
        'id="dash-quality-mini-spark"',
    ):
        assert needle in html, f"missing {needle}"
    # The card lives inside the quality-history band, above the charts grid.
    assert html.index('id="dash-quality-band"') < html.index('id="dash-quality-mini"')
    assert html.index('id="dash-quality-mini"') < html.index('id="dash-quality-grid"')


# ── pure helpers (executed in node against the real functions) ──

def test_latest_quality_point_returns_last_point() -> None:
    points = [
        {"ts": "2026-08-01", "total": 100},
        {"ts": "2026-08-02", "total": 250},
        {"ts": "2026-08-03", "total": 500},
    ]
    assert json.loads(_run_pure("latestQualityPoint", points)) == {"ts": "2026-08-03", "total": 500}
    assert json.loads(_run_pure("latestQualityPoint", [{"ts": "2026-08-01"}])) == {"ts": "2026-08-01"}


def test_latest_quality_point_empty_series_returns_null() -> None:
    assert json.loads(_run_pure("latestQualityPoint", [])) is None
    assert json.loads(_run_pure("latestQualityPoint", None)) is None


def test_quality_mini_stats_formats_strings() -> None:
    point = {
        "total": 1234,
        "duplicate_ratio": 0.1234,
        "near_duplicate_ratio": 0.04,
        "capture_rate": 0.982,
    }
    stats = json.loads(_run_quality_mini_stats(point))
    assert stats == {
        "total": "1234",
        "dupPct": "12.3%",
        "nearDupPct": "4.0%",
        "captureRate": "98.2%",
    }


def test_quality_mini_stats_tolerates_missing_fields() -> None:
    stats = json.loads(_run_quality_mini_stats({}))
    assert stats == {
        "total": "—",
        "dupPct": "—",
        "nearDupPct": "—",
        "captureRate": "—",
    }
    assert json.loads(_run_quality_mini_stats(None))["dupPct"] == "—"


def test_quality_mini_stats_reuses_table_pct_formatting() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    q = _quality_slice(app_js)
    # Same one-decimal percent format as the table rows.
    assert 'qualityPct(p.duplicate_ratio)' in _extract_function("qualityMiniStats", q)
    assert '(v * 100).toFixed(1) + "%"' in _extract_function("qualityPct", q)
    # Sanity: the mini-card and the table agree on the same ratio.
    mini = json.loads(_run_quality_mini_stats({"duplicate_ratio": 0.1234}))
    assert mini["dupPct"] == "12.3%"


# ── wiring: re-render with the quality band ───────────────────

def test_quality_mini_rerenders_with_band() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    q = _quality_slice(app_js)
    for fn in ("latestQualityPoint", "qualityMiniStats", "renderQualityMini"):
        assert f"function {fn}(" in q, f"missing {fn}"
    rq = _extract_function("renderQualityHistory", q)
    # The mini-card re-renders on the same call path as the dup/domains charts
    # and the table, which is guarded by the 12-tick cadence in refreshDashQuality.
    assert "renderQualityMini(points)" in rq
    assert "dashQualityTick % 12 !== 0" in q
    assert "void refreshDashQuality();" in app_js


def test_quality_mini_region_no_innerhtml() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    q = _quality_slice(app_js)
    assert "innerHTML" not in q
    assert "textContent" in q


def test_quality_mini_styles_present() -> None:
    css = STYLE_CSS.read_text(encoding="utf-8")
    for needle in (
        ".dash-quality-mini",
        ".dash-quality-mini-kpis",
        ".qmini-stat",
        ".qmini-label",
        ".qmini-value",
        ".qmini-spark",
    ):
        assert needle in css, f"missing {needle}"


# ── renderQualityMini executed in node against a stub DOM ─────

def _run_render_quality_mini(points: list) -> dict:
    """Run the real renderQualityMini from app.js with a stub $ + bar chart."""
    if NODE is None:
        pytest.skip("node not available")
    app_js = APP_JS.read_text(encoding="utf-8")
    fns = (
        _extract_function("qualityPct", app_js) + "\n"
        + _extract_function("latestQualityPoint", app_js) + "\n"
        + _extract_function("qualityMiniStats", app_js) + "\n"
        + _extract_function("renderQualityMini", app_js) + "\n"
    )
    harness = (
        "const points = " + json.dumps(points) + ";\n"
        "const els = {};\n"
        "function $(q) { if (!els[q]) els[q] = { textContent: '', bars: null }; return els[q]; }\n"
        "function renderBarChart(el, pts) { el.bars = (pts || []).length; }\n"
        + fns + "\n"
        "renderQualityMini(points);\n"
        "console.log(JSON.stringify({\n"
        "  total: els['#qmini-total'].textContent,\n"
        "  dup: els['#qmini-dup'].textContent,\n"
        "  near: els['#qmini-near'].textContent,\n"
        "  capture: els['#qmini-capture'].textContent,\n"
        "  sparkBars: els['#dash-quality-mini-spark'].bars,\n"
        "}));\n"
    )
    return json.loads(_run_js(harness))


def _points(n: int) -> list:
    return [
        {"ts": f"2026-08-{i + 1:02d}", "total": 100 + i, "duplicate_ratio": 0.1 + 0.01 * i,
         "near_duplicate_ratio": 0.05, "capture_rate": 0.97}
        for i in range(n)
    ]


def test_render_quality_mini_shows_latest_kpis_and_14_bar_sparkline() -> None:
    out = _run_render_quality_mini(_points(20))
    assert out["total"] == "119"  # last of the 20 points
    assert out["dup"] == "29.0%"  # 0.1 + 0.01 * 19 = 0.29
    assert out["near"] == "5.0%"
    assert out["capture"] == "97.0%"
    # Sparkline is capped at the last 14 points regardless of series length.
    assert out["sparkBars"] == 14


def test_render_quality_mini_short_series_and_empty() -> None:
    out = _run_render_quality_mini(_points(5))
    assert out["total"] == "104"
    assert out["sparkBars"] == 5
    empty = _run_render_quality_mini([])
    assert empty["total"] == "—"
    assert empty["dup"] == "—"
    assert empty["sparkBars"] == 0
