"""Static + pure-logic checks: SPA Topic lifecycle band (Analytics view).

Follows the ``test_spa_*`` pattern: HTML band structure, app.js wiring, the
pure ``phaseClass`` / ``lifecycleStatsText`` functions executed in node
against the real code, the no-innerHTML rule for the new region, and the
button/chip wiring (chip fills the term input + triggers the lifecycle).
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


def _lifecycle_slice(app_js: str) -> str:
    start = app_js.index("// ── Topic lifecycle band")
    end = app_js.index("// ── Alerts", start)
    return app_js[start:end]


def _extract_const(name: str, source: str) -> str:
    """Brace-counting extractor for a top-level `const <name> = {...};`."""
    start = source.index(f"const {name} = {{")
    depth = 0
    i = start
    while i < len(source):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 2]
        i += 1
    raise AssertionError(f"could not extract const {name}")


def _run_node(harness: str) -> str:
    """Execute the extracted app.js function in node and return stdout."""
    if NODE is None:
        pytest.skip("node not available")
    proc = subprocess.run(  # noqa: S603 - harness is our own extracted fn + JSON data
        [NODE, "-e", harness], capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip().splitlines()[-1]


def _run_phase_class(phase: str) -> str:
    app_js = APP_JS.read_text(encoding="utf-8")
    const = _extract_const("PHASE_CLASSES", app_js)
    fn = _extract_function("phaseClass", app_js)
    return _run_node(f"{const}\n{fn}\nconsole.log(phaseClass({json.dumps(phase)}));\n")


def _run_lifecycle_stats_text(lifecycle: dict) -> str:
    app_js = APP_JS.read_text(encoding="utf-8")
    fn = _extract_function("lifecycleStatsText", app_js)
    return _run_node(
        "const lifecycle = " + json.dumps(lifecycle) + ";\n"
        + "const fmt = (n) => (n == null ? \"—\" : new Intl.NumberFormat(\"en-US\").format(n));\n"
        + fn + "\n"
        + "console.log(lifecycleStatsText(lifecycle));\n"
    )


def test_lifecycle_band_in_html_under_gdelt() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "Topic lifecycle" in html
    for needle in (
        'id="an-life-go"',
        'id="an-life-phase"',
        'id="an-life-stats"',
        'id="an-life-chart"',
        'id="an-emerging-chips"',
        'id="an-impact-table"',
        'id="an-impact-body"',
    ):
        assert needle in html, f"missing {needle}"
    # Band sits under the GDELT band and above Top terms.
    assert html.index('id="an-gdelt-stats"') < html.index('id="an-life-go"')
    assert html.index('id="an-life-go"') < html.index('id="an-top-title"')
    # The Lifecycle button reuses the term-frequency inputs (no own term input).
    assert "Topic lifecycle" in html[html.index('id="an-life-title"'):]


def test_lifecycle_js_present_and_endpoints_used() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    life = _lifecycle_slice(app_js)
    assert "function phaseClass(" in life
    assert "function lifecycleStatsText(" in life
    assert "function renderLifecycle(" in life
    assert "async function loadLifecycle(" in life
    assert "async function loadEmerging(" in life
    assert "async function loadImpact(" in life
    assert 'api(`/topicx/lifecycle?term=${encodeURIComponent(term)}&window_days=${windowDays}`)' in life
    assert 'api("/topicx/emerging?limit=12")' in life
    assert 'api("/topicx/impact?limit=10")' in life


def test_lifecycle_wired_in_init_analytics() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert 'lifeBtn.addEventListener("click", loadLifecycle)' in app_js
    assert 'const lifeBtn = $("#an-life-go")' in app_js
    # Band data that does not depend on the term loads with the view.
    assert "void loadEmerging();" in app_js
    assert "void loadImpact();" in app_js


def test_lifecycle_loading_state_on_button() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    life = _lifecycle_slice(app_js)
    assert "btn.disabled = true" in life
    assert "btn.disabled = false" in life


def test_lifecycle_reuses_render_bar_chart() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    life = _lifecycle_slice(app_js)
    assert "renderBarChart(chart, lifecycle.counts || [])" in life
    # Phase badge is a text node with a color class — never innerHTML.
    assert 'class: "an-life-badge " + phaseClass(lifecycle.phase)' in life


def test_emerging_chip_fills_term_and_runs_lifecycle() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    life = _lifecycle_slice(app_js)
    assert '$("#an-term-input").value = x.term' in life
    assert "void loadLifecycle();" in life
    # Tooltip carries first_seen + domains_covered.
    assert "first ${String(x.first_seen || \"\").slice(0, 10)}" in life
    assert "${x.domains_covered} domains" in life


def test_lifecycle_region_no_innerhtml() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    life = _lifecycle_slice(app_js)
    assert "innerHTML" not in life
    assert "el(" in life
    assert "document.createTextNode(lifecycleStatsText(lifecycle))" in life


def test_lifecycle_styles_present() -> None:
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".an-life-badge" in css
    assert ".an-life-stats" in css
    assert ".an-life-subhead" in css
    assert "an-emerging-pulse" in css


# ── pure phaseClass mapping (executed in node against the real function) ──

def test_phase_class_maps_all_five_phases() -> None:
    phases = ["EMERGING", "EXPANDING", "PEAKING", "DECLINING", "DORMANT"]
    expected = {
        "EMERGING": "is-emerging",
        "EXPANDING": "is-expanding",
        "PEAKING": "is-peaking",
        "DECLINING": "is-declining",
        "DORMANT": "is-dormant",
    }
    classes = [_run_phase_class(p) for p in phases]
    assert classes == [expected[p] for p in phases]
    assert len(set(classes)) == 5


def test_phase_class_normalizes_case_and_falls_back() -> None:
    assert _run_phase_class("emerging") == "is-emerging"
    # Unknown / missing phase degrades to the neutral dormant badge.
    assert _run_phase_class("SOMETHING_ELSE") == "is-dormant"
    assert _run_phase_class("") == "is-dormant"


# ── pure stats formatter (executed in node against the real function) ──

def test_lifecycle_stats_text_full_payload() -> None:
    out = _run_lifecycle_stats_text(
        {
            "term": "bitcoin",
            "phase": "EMERGING",
            "slope_7d": 0.125,
            "peak_count": 42,
            "peak_date": "2026-08-01T00:00:00",
            "first_seen": "2026-07-20T00:00:00",
            "last_seen": "2026-08-02T00:00:00",
        }
    )
    assert out == "slope 7d 0.125 · peak 42 on 2026-08-01 · first 2026-07-20 · last 2026-08-02"


def test_lifecycle_stats_text_tolerates_nullable_fields() -> None:
    out = _run_lifecycle_stats_text(
        {"slope_7d": None, "peak_count": None, "peak_date": None, "first_seen": None, "last_seen": None}
    )
    assert out == "slope 7d — · peak — · first — · last —"
