"""Static + node-exec checks: SPA "X ↔ News" cross-view band (X sessions view).

Follows the ``test_spa_x_view`` / ``test_spa_topic_lifecycle`` pattern:
HTML band structure in the X view, app.js wiring (select populated from the
loaded sessions, /crossx/view endpoint, phaseClass reuse, renderBarChart
reuse), the no-innerHTML rule for the new region, CSS presence, and a node
execution of the real ``renderCrossView`` against a stub DOM.
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


def _x_slice(app_js: str) -> str:
    start = app_js.index("// ── X sessions")
    end = app_js.index("// ── Dashboard saved widgets")
    return app_js[start:end]


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


def test_spa_cross_band_in_html_under_analysis() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "X ↔ News" in html
    for needle in (
        'class="band x-cross-band"',
        'id="x-cross"',
        'id="x-cross-title"',
        'id="x-cross-session"',
        'id="x-cross-term"',
        'id="x-cross-go"',
        'id="x-cross-root"',
    ):
        assert needle in html, f"missing {needle}"
    # Band sits between the Analysis panel and the Tweets panel.
    assert html.index('id="x-an-root"') < html.index('id="x-cross"')
    assert html.index('id="x-cross"') < html.index('id="x-tweets-list"')
    # The session select is fed by the loaded sessions, so no hardcoded option.
    assert '<option' not in html[html.index('id="x-cross-session"'):html.index('id="x-cross-root"')]


def test_spa_cross_js_present_and_endpoint_used() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    x = _x_slice(app_js)
    for fn in (
        "renderCrossSessionSelect",
        "sentimentBars",
        "loadCrossView",
        "renderCrossView",
    ):
        assert f"function {fn}(" in x, f"missing {fn}"
    assert '"/crossx/view?term=" + encodeURIComponent(term)' in x
    assert 'renderCrossView(root, view)' in x


def test_spa_cross_wired_in_init_x_view() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert 'renderCrossSessionSelect(sessions || [])' in app_js
    assert '$("#x-cross-go")?.addEventListener("click", () => void loadCrossView())' in app_js
    assert '$("#x-cross-term")?.addEventListener("keydown"' in app_js
    assert 'if (e.key === "Enter") void loadCrossView();' in app_js


def test_spa_cross_reuses_phase_class_and_bar_chart() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    x = _x_slice(app_js)
    # Phase badge reuses the analytics view's global phaseClass helper.
    assert 'class: "an-life-badge " + phaseClass(v.news_phase)' in x
    assert "renderBarChart(newsBox," in x
    assert "renderBarChart(xBox," in x
    assert "sentimentBars(p.avg_score)" in x
    # Loading state guards the button.
    assert "go.disabled = true" in x
    assert "go.disabled = false" in x
    # Empty/unknown session handled client-side with a note, no fetch.
    assert "No session selected" in x


def test_spa_cross_region_no_innerhtml() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    x = _x_slice(app_js)
    assert "innerHTML" not in x
    assert "el(" in x
    assert "document.createTextNode(" in x


def test_spa_cross_styles_present() -> None:
    css = STYLE_CSS.read_text(encoding="utf-8")
    for needle in (
        ".x-cross-controls",
        ".x-cross-head",
        ".x-cross-term",
        ".x-cross-note",
        ".x-cross-charts",
        ".x-cross-chart",
        ".x-cross-foot",
    ):
        assert needle in css, f"missing {needle}"


# ── pure sentimentBars mapping (executed in node against the real function) ──

def _run_sentiment_bars(score: float) -> str:
    app_js = APP_JS.read_text(encoding="utf-8")
    fn = _extract_function("sentimentBars", app_js)
    harness = fn + f"\nconsole.log(sentimentBars(Number({json.dumps(score)})));\n"
    if NODE is None:
        pytest.skip("node not available")
    proc = subprocess.run(  # noqa: S603 - harness is our own extracted fn
        [NODE, "-e", harness], capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip().splitlines()[-1]


def test_sentiment_bars_maps_score_range() -> None:
    assert _run_sentiment_bars(1.0) == "100"
    assert _run_sentiment_bars(0.0) == "50"
    assert _run_sentiment_bars(-1.0) == "0"
    assert _run_sentiment_bars(0.5) == "75"
    # Out-of-range / non-finite values clamp to the 0..100 band.
    assert _run_sentiment_bars(2.0) == "100"
    assert _run_sentiment_bars("NaN") == "0"


# ── renderCrossView executed in node against a stub DOM ────────────────────

VIEW = {
    "term": "bitcoin",
    "news_phase": "EMERGING",
    "news_series": [{"ts": "2026-08-01T00:00:00Z", "count": 3}],
    "news_sentiment": [
        {"ts": "2026-08-01T00:00:00Z", "avg_score": 1.0},
        {"ts": "2026-08-02T00:00:00Z", "avg_score": 0.0},
    ],
    "x_sentiment": [
        {"ts": "2026-08-01T00:00:00Z", "avg_score": 0.5},
        {"ts": "2026-08-02T00:00:00Z", "avg_score": -1.0},
    ],
    "news_avg_score": 0.5,
    "x_avg_score": -0.25,
    "correlation_r": -1.0,
    "convergence": "divergence",
    "note": "",
}

VIEW_X_NONE = {
    "term": "bitcoin",
    "news_phase": "STABLE",
    "news_series": [],
    "news_sentiment": [],
    "x_sentiment": None,
    "news_avg_score": 0.0,
    "x_avg_score": 0.0,
    "correlation_r": 0.0,
    "convergence": "neutral",
    "note": "x session unknown — news side only",
}


def _run_render_cross_view(view: dict | None) -> dict:
    """Execute the real renderCrossView from app.js in node against a stub DOM."""
    if NODE is None:
        pytest.skip("node not available")
    app_js = APP_JS.read_text(encoding="utf-8")
    fn = _extract_function("renderCrossView", app_js)
    bars_fn = _extract_function("sentimentBars", app_js)
    harness = (
        "const view = " + json.dumps(view) + ";\n"
        "function el(tag, props = {}, ...children) {\n"
        "  const node = { tag, className: '', textContent: '', children: [] };\n"
        "  for (const [k, v] of Object.entries(props || {})) {\n"
        "    if (v == null || v === false) continue;\n"
        "    if (k === 'class') node.className = v;\n"
        "    else if (k === 'text') node.textContent = v;\n"
        "    else if (k === 'style' || k === 'dataset' || typeof v === 'function') {}\n"
        "    else node[k] = v;\n"
        "  }\n"
        "  node.appendChild = (c) => { node.children.push(typeof c === 'string' ? { text: c } : c); return c; };\n"
        "  return node;\n"
        "}\n"
        "function clear(node) { if (node) node.children = []; }\n"
        "function phaseClass(p) { return 'is-' + String(p || '').toLowerCase(); }\n"
        "function renderBarChart(el, points) { el.bars = (points || []).length; }\n"
        "function findClass(node, cls) {\n"
        "  const out = [];\n"
        "  const walk = (n) => {\n"
        "    if (n && n.className && String(n.className).split(/\\s+/).includes(cls)) out.push(n);\n"
        "    (n.children || []).forEach(walk);\n"
        "  };\n"
        "  walk(node);\n"
        "  return out;\n"
        "}\n"
        "const document = { createTextNode: (t) => ({ text: t }) };\n"
        + bars_fn + "\n"
        + fn + "\n"
        "const root = el('div');\n"
        "renderCrossView(root, view);\n"
        "const charts = findClass(root, 'x-cross-chart');\n"
        "const foot = findClass(root, 'x-cross-foot')[0];\n"
        "console.log(JSON.stringify({\n"
        "  badges: findClass(root, 'an-life-badge').length,\n"
        "  badgeText: findClass(root, 'an-life-badge')[0].textContent,\n"
        "  term: findClass(root, 'x-cross-term')[0].textContent,\n"
        "  newsBars: charts[0].bars,\n"
        "  xBars: charts.length > 1 ? charts[1].bars : null,\n"
        "  footText: foot.textContent + (foot.children.length ? foot.children.map((c) => c.textContent || c.text || '').join('') : ''),\n"
        "  notes: findClass(root, 'x-cross-note').length,\n"
        "}));\n"
    )
    proc = subprocess.run(  # noqa: S603, PLW1510 - node on our own harness script
        [NODE, "-e", harness], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_render_cross_view_full_payload() -> None:
    out = _run_render_cross_view(VIEW)
    assert out["badges"] == 1
    assert out["badgeText"] == "EMERGING"
    assert out["term"] == "bitcoin"
    assert out["newsBars"] == 2
    assert out["xBars"] == 2
    assert out["notes"] == 0
    assert "r -1.00" in out["footText"]
    assert "divergence" in out["footText"]


def test_render_cross_view_x_none_payload() -> None:
    out = _run_render_cross_view(VIEW_X_NONE)
    assert out["badges"] == 1
    assert out["badgeText"] == "STABLE"
    assert out["newsBars"] == 0
    assert out["xBars"] == 0
    assert out["notes"] == 1
    assert "neutral" in out["footText"]


def test_render_cross_view_empty_payload() -> None:
    out = _run_render_cross_view(None)
    # Zeroed payload still renders the badge + foot; no throw.
    assert out["badges"] == 1
    assert out["badgeText"] == "—"
    assert out["newsBars"] == 0
    assert "r —" in out["footText"]
    assert "neutral" in out["footText"]
