"""Static + pure-logic checks: SPA GDELT comparison band (Analytics view).

Follows the ``test_spa_*`` pattern: HTML band structure, app.js wiring, the
pure ``gdeltStatsText`` formatter executed in node against fake comparisons
(including the degraded / empty-``gdelt_series`` path), and the no-innerHTML
rule for the new region.
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


def _gdelt_slice(app_js: str) -> str:
    start = app_js.index("// ── GDELT comparison band")
    end = app_js.index("// ── Entity network", start)
    return app_js[start:end]


def _run_gdelt_stats_text(comparison: dict) -> str:
    """Execute the real gdeltStatsText formatter from app.js in node."""
    if NODE is None:
        pytest.skip("node not available")
    app_js = APP_JS.read_text(encoding="utf-8")
    fn = _extract_function("gdeltStatsText", app_js)
    harness = (
        "const comparison = " + json.dumps(comparison) + ";\n"
        + fn + "\n"
        + "console.log(gdeltStatsText(comparison));\n"
    )
    proc = subprocess.run(  # noqa: S603 - harness executes only our own extracted function + JSON data
        [NODE, "-e", harness], capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip().splitlines()[-1]


def test_gdelt_band_in_html_under_term_frequency() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "GDELT comparison" in html
    for needle in (
        'id="an-gdelt-go"',
        'id="an-gdelt-charts"',
        'id="an-gdelt-local"',
        'id="an-gdelt-ext"',
        'id="an-gdelt-stats"',
    ):
        assert needle in html, f"missing {needle}"
    assert html.index('id="an-term-chart"') < html.index('id="an-gdelt-go"')
    assert html.index('id="an-gdelt-go"') < html.index('id="an-top-title"')
    # The compare button reuses the term-frequency inputs (no own term input).
    assert "GDELT comparison" in html[html.index('id="an-gdelt-title"'):]


def test_gdelt_js_present_and_endpoint_used() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    gdelt = _gdelt_slice(app_js)
    assert "function gdeltStatsText(" in gdelt
    assert "function renderGdeltComparison(" in gdelt
    assert "async function compareWithGdelt(" in gdelt
    assert 'api(`/gdelt/compare?term=${encodeURIComponent(term)}&window_days=${gdeltDays}`)' in gdelt
    # The window select goes to 90d but /gdelt/compare caps at 60 — clamped.
    assert "Math.min(windowDays, 60)" in gdelt
    # Loading state on the button during the (possibly slow) bridge call.
    assert 'btn.disabled = true' in gdelt
    assert 'btn.disabled = false' in gdelt


def test_gdelt_wired_in_init_analytics() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert 'gdeltBtn.addEventListener("click", compareWithGdelt)' in app_js
    assert 'const gdeltBtn = $("#an-gdelt-go")' in app_js


def test_gdelt_band_reuses_render_bar_chart() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    gdelt = _gdelt_slice(app_js)
    # Reuses the existing inline bar chart helper (not a new chart renderer).
    assert "renderBarChart(localBox, comparison.local_series || [])" in gdelt
    assert "renderBarChart(extBox, extSeries, { color:" in gdelt


def test_gdelt_degraded_path_shows_dim_notice() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    gdelt = _gdelt_slice(app_js)
    # Empty gdelt_series → dim "GDELT unavailable" notice instead of a chart,
    # and the backend note is rendered verbatim through the stats line.
    assert "const extSeries = comparison.gdelt_series || []" in gdelt
    assert "extSeries.length" in gdelt
    assert 'el("span", { class: "an-gdelt-offline", text: "GDELT unavailable" })' in gdelt
    assert "gdeltStatsText(comparison)" in gdelt
    # The note is backend-controlled; the stats line is a text node, never HTML.
    assert "document.createTextNode(gdeltStatsText(comparison))" in gdelt


def test_gdelt_region_no_innerhtml() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    gdelt = _gdelt_slice(app_js)
    assert "innerHTML" not in gdelt
    assert "el(" in gdelt
    assert ".textContent" in gdelt


def test_gdelt_styles_present() -> None:
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".an-gdelt-charts" in css
    assert ".an-gdelt-offline" in css
    assert ".an-gdelt-stats" in css


# ── pure formatter checks (executed in node against the real function) ──

def test_gdelt_stats_text_full_comparison() -> None:
    out = _run_gdelt_stats_text(
        {
            "term": "bitcoin",
            "local_count": 12,
            "gdelt_count": 340,
            "correlation_r": 0.452,
            "n_days": 14,
            "note": "",
        }
    )
    assert out == "local 12 · GDELT 340 · r 0.45"


def test_gdelt_stats_text_includes_backend_note_verbatim() -> None:
    out = _run_gdelt_stats_text(
        {
            "term": "bitcoin",
            "local_count": 12,
            "gdelt_count": 0,
            "correlation_r": 0.0,
            "n_days": 14,
            "note": "gdelt API unavailable; gdelt_series empty",
        }
    )
    assert "gdelt API unavailable; gdelt_series empty" in out
    assert out.startswith("local 12 · GDELT 0 · r 0.00")


def test_gdelt_stats_text_tolerates_nullable_fields() -> None:
    out = _run_gdelt_stats_text(
        {"local_count": None, "gdelt_count": None, "correlation_r": None, "note": None}
    )
    assert out == "local 0 · GDELT 0 · r —"
