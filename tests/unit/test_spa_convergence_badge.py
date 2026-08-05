"""Static + node-exec checks: convergence badge in the SPA "X ↔ News" band.

Follows the ``test_spa_crossx_view`` pattern: pure ``convergenceClass`` /
``convergenceLabel`` / ``lowOverlap`` helpers executed in node against the
real functions from app.js, badge wiring inside ``renderCrossView`` (grep),
the tooltip title set via ``setAttribute`` (never innerHTML), a node run of
the real ``renderCrossView`` against a stub DOM, and CSS presence.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path("src/awareness/api/web/app.js")
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


# ── pure helpers (executed in node against the real functions) ──

def test_convergence_class_maps_all_verdicts() -> None:
    assert _run_pure("convergenceClass", "aligned bullish") == '"is-aligned-bullish"'
    assert _run_pure("convergenceClass", "aligned bearish") == '"is-aligned-bearish"'
    assert _run_pure("convergenceClass", "divergence") == '"is-divergence"'
    assert _run_pure("convergenceClass", "neutral") == '"is-neutral"'
    # Unknown input falls back to the gray neutral class.
    assert _run_pure("convergenceClass", "bogus") == '"is-neutral"'
    assert _run_pure("convergenceClass", None) == '"is-neutral"'


def test_convergence_class_is_case_and_whitespace_tolerant() -> None:
    assert _run_pure("convergenceClass", "ALIGNED BULLISH") == '"is-aligned-bullish"'
    assert _run_pure("convergenceClass", "  Divergence  ") == '"is-divergence"'


def test_convergence_label_matches_verdicts() -> None:
    assert _run_pure("convergenceLabel", "aligned bullish") == '"aligned bullish"'
    assert _run_pure("convergenceLabel", "aligned bearish") == '"aligned bearish"'
    assert _run_pure("convergenceLabel", "divergence") == '"divergence"'
    assert _run_pure("convergenceLabel", "neutral") == '"neutral"'
    assert _run_pure("convergenceLabel", "???") == '"neutral"'
    assert _run_pure("convergenceLabel", None) == '"neutral"'


def test_low_overlap_logic() -> None:
    # r == 0 with real sentiment on both sides → overlap is low.
    assert _run_pure("lowOverlap", 0, 0.5, -0.3) == "true"
    assert _run_pure("lowOverlap", 0, 0.0, 0.0) == "false"
    # Either side silent → nothing to compare.
    assert _run_pure("lowOverlap", 0, 0, 0.5) == "false"
    assert _run_pure("lowOverlap", 0, 0.5, 0) == "false"
    # Non-zero r → real (anti)correlation, no hint.
    assert _run_pure("lowOverlap", 0.4, 0.5, -0.3) == "false"


# ── wiring in renderCrossView (static) ────────────────────────

def test_convergence_badge_wired_in_render_cross_view() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    x = _x_slice(app_js)
    rcv = _extract_function("renderCrossView", app_js)
    for needle in (
        "function convergenceClass(",
        "function convergenceLabel(",
        "function lowOverlap(",
        '"x-conv-badge "',
        '"x-conv-hint"',
    ):
        assert needle in x, f"missing {needle}"
    # The badge class + label come from the pure helpers, not a string switch
    # inside the renderer.
    assert "convergenceClass(conv)" in rcv
    assert "convergenceLabel(conv)" in rcv
    assert "lowOverlap(" in rcv
    # Tooltip carries r + note, set through the XSS-safe attribute API.
    assert 'setAttribute("title", tip)' in rcv
    assert '" · " + note' in rcv


def test_convergence_region_no_innerhtml() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    x = _x_slice(app_js)
    assert "innerHTML" not in x
    assert "el(" in x
    assert "document.createTextNode(" in x


def test_convergence_styles_present() -> None:
    css = STYLE_CSS.read_text(encoding="utf-8")
    for needle in (
        ".x-conv-badge",
        ".x-conv-badge.is-aligned-bullish",
        ".x-conv-badge.is-aligned-bearish",
        ".x-conv-badge.is-divergence",
        ".x-conv-badge.is-neutral",
        ".x-conv-hint",
    ):
        assert needle in css, f"missing {needle}"


# ── renderCrossView executed in node against a stub DOM ───────

VIEW_NEUTRAL = {
    "term": "bitcoin",
    "news_phase": "STABLE",
    "news_series": [],
    "news_sentiment": [],
    "x_sentiment": [],
    "news_avg_score": 0.0,
    "x_avg_score": 0.0,
    "correlation_r": 0.0,
    "convergence": "neutral",
    "note": "",
}

VIEW_LOW_OVERLAP = {
    "term": "eth",
    "news_phase": "EMERGING",
    "news_series": [],
    "news_sentiment": [],
    "x_sentiment": [],
    "news_avg_score": -0.3,
    "x_avg_score": 0.5,
    "correlation_r": 0,
    "convergence": "divergence",
    "note": "x session unknown — news side only",
}


def _run_render_cross_view(view: dict) -> dict:
    """Run the real renderCrossView from app.js in node with a stub DOM."""
    if NODE is None:
        pytest.skip("node not available")
    app_js = APP_JS.read_text(encoding="utf-8")
    fn = _extract_function("renderCrossView", app_js)
    bars_fn = _extract_function("sentimentBars", app_js)
    helpers = (
        _extract_function("convergenceClass", app_js) + "\n"
        + _extract_function("convergenceLabel", app_js) + "\n"
        + _extract_function("lowOverlap", app_js) + "\n"
    )
    harness = (
        "const view = " + json.dumps(view) + ";\n"
        "function el(tag, props = {}, ...children) {\n"
        "  const node = { tag, className: '', textContent: '', children: [], attrs: {} };\n"
        "  node.setAttribute = (k, v) => { node.attrs[k] = v; };\n"
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
        + helpers + "\n"
        + fn + "\n"
        "const root = el('div');\n"
        "renderCrossView(root, view);\n"
        "const badge = findClass(root, 'x-conv-badge')[0];\n"
        "const hint = findClass(root, 'x-conv-hint');\n"
        "console.log(JSON.stringify({\n"
        "  badgeClass: badge ? badge.className : null,\n"
        "  badgeText: badge ? badge.textContent : null,\n"
        "  badgeTitle: badge ? badge.attrs.title : null,\n"
        "  hintCount: hint.length,\n"
        "}));\n"
    )
    out = _run_js(harness)
    return json.loads(out)


def test_render_cross_view_badge_neutral() -> None:
    out = _run_render_cross_view(VIEW_NEUTRAL)
    assert out["badgeClass"] == "x-conv-badge is-neutral"
    assert out["badgeText"] == "neutral"
    assert out["badgeTitle"] == "r 0.00"
    assert out["hintCount"] == 0


def test_render_cross_view_badge_low_overlap_hint() -> None:
    out = _run_render_cross_view(VIEW_LOW_OVERLAP)
    # r == 0 + both sides have sentiment → divergence with a low-overlap hint.
    assert out["badgeClass"] == "x-conv-badge is-divergence"
    assert out["badgeText"] == "divergence"
    # Tooltip carries the correlation plus the backend note.
    assert out["badgeTitle"] == "r 0.00 · x session unknown — news side only"
    assert out["hintCount"] == 1
