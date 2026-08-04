"""Static + node-exec checks: SPA X sessions view (route, /x/sessions CRUD,
simulate / analysis / tweets endpoints, analysis panel rendering, no innerHTML)."""

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


def test_spa_x_route_registered() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    route_line = next(
        line for line in app_js.splitlines() if line.startswith("const ROUTES")
    )
    assert '"x"' in route_line
    # Ordering: x between saved and settings (nav order == shortcut order).
    assert '"saved", "x", "settings"' in route_line
    assert "if (route === \"x\") void initXView();" in app_js
    # Number shortcuts still cover the routes (1..9; settings is the 10th).
    assert "/^[1-9]$/" in app_js


def test_spa_x_view_marks_lazy_load() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "async function initXView()" in app_js
    assert "let xViewReady = false;" in app_js
    assert "xViewReady" in _x_slice(app_js)


def test_spa_x_sessions_endpoints() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    x = _x_slice(app_js)
    # List + create.
    assert 'api("/x/sessions")' in x
    assert 'api("/x/sessions", { method: "POST", body: JSON.stringify(body) })' in x
    # Per-session simulate / analysis / tweets.
    assert '"/x/sessions/" + encodeURIComponent(sessionId) + "/simulate"' in x
    assert 'body: JSON.stringify({ n_tweets: nTweets })' in x
    assert '"/x/sessions/" + encodeURIComponent(sessionId) + "/analysis"' in x
    assert '"/x/sessions/" + encodeURIComponent(sessionId) + "/tweets"' in x


def test_spa_x_handlers_exist() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    x = _x_slice(app_js)
    for fn in (
        "initXView",
        "loadXView",
        "renderXSessionList",
        "createXSession",
        "startXSimulate",
        "simulateXSession",
        "analyzeXSession",
        "renderXAnalysis",
        "showXSessionTweets",
        "renderXTweetList",
    ):
        assert f"function {fn}(" in x
    # Simulate uses an inline count input (default 20), not prompt().
    assert "window.prompt" not in x
    assert 'value: "20"' in x


def test_spa_x_no_inner_html() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "innerHTML" not in _x_slice(app_js)


def test_spa_x_html_section_and_nav() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'class="view view-x" data-view="x"' in html
    assert 'id="title-x"' in html
    assert 'data-route="x"' in html
    assert 'id="x-sessions-body"' in html
    assert 'id="x-form"' in html
    assert 'id="x-analysis"' in html
    assert 'id="x-an-root"' in html
    assert 'id="x-tweets-list"' in html
    # Shortcuts renumbered: saved = 8, x = 9, settings = 10.
    saved_nav = html[html.index('data-route="saved"'):]
    saved_nav = saved_nav[: saved_nav.index("</button>")]
    assert '<span class="nav-shortcut">8</span>' in saved_nav
    x_nav = html[html.index('data-route="x"'):]
    x_nav = x_nav[: x_nav.index("</button>")]
    assert '<span class="nav-shortcut">9</span>' in x_nav
    settings_nav = html[html.index('data-route="settings"'):]
    settings_nav = settings_nav[: settings_nav.index("</button>")]
    assert '<span class="nav-shortcut">10</span>' in settings_nav


def test_spa_x_styles_present() -> None:
    css = STYLE_CSS.read_text(encoding="utf-8")
    for needle in (
        ".view-x",
        ".x-form-grid",
        ".x-sim-inline",
        ".x-an-sentiment",
        ".x-an-chip",
        ".x-an-author-row",
        ".x-an-timeline",
        ".x-an-engagement",
        ".x-tweet",
    ):
        assert needle in css


def _run_render_x_analysis(analysis: dict | None) -> dict:
    """Execute the real renderXAnalysis from app.js in node against a stub DOM.

    el() builds plain objects; renderBarChart/renderChips record what they
    were handed so we can count bars/chips; findClass() walks the tree.
    """
    if NODE is None:
        pytest.skip("node not available")
    app_js = APP_JS.read_text(encoding="utf-8")
    fn = _extract_function("renderXAnalysis", app_js)
    harness = (
        "const analysis = " + json.dumps(analysis) + ";\n"
        "function el(tag, props = {}, ...children) {\n"
        "  const node = { tag, className: '', textContent: '', title: '', children: [] };\n"
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
        "function fmt(n) { return n == null ? '—' : String(n); }\n"
        "function renderBarChart(el, points) { el.bars = (points || []).length; }\n"
        "function renderChips(el, items) { el.chips = (items || []).length; }\n"
        "function findClass(node, cls) {\n"
        "  const out = [];\n"
        "  const walk = (n) => {\n"
        "    if (n && n.className && String(n.className).includes(cls)) out.push(n);\n"
        "    (n.children || []).forEach(walk);\n"
        "  };\n"
        "  walk(node);\n"
        "  return out;\n"
        "}\n"
        + fn + "\n"
        "const root = el('div');\n"
        "renderXAnalysis(root, analysis);\n"
        "console.log(JSON.stringify({\n"
        "  sentimentChips: findClass(root, 'x-an-chip').length,\n"
        "  authorRows: findClass(root, 'x-an-author-row').length,\n"
        "  termChips: findClass(root, 'x-an-terms')[0].chips,\n"
        "  timelineBars: findClass(root, 'x-an-timeline')[0].bars,\n"
        "  engagement: findClass(root, 'x-an-engagement')[0].textContent,\n"
        "}));\n"
    )
    proc = subprocess.run(  # noqa: S603, PLW1510 — node on our own harness script
        [NODE, "-e", harness], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


# Synthetic /x/sessions/{id}/analysis payload mirroring the backend shape.
ANALYSIS = {
    "session_id": "s1",
    "tweet_count": 42,
    "authors": [
        {"username": "alice", "count": 20},
        {"username": "bob", "count": 22},
    ],
    "top_terms": [
        {"term": "bitcoin", "count": 7},
        {"term": "ethereum", "count": 4},
    ],
    "sentiment": {"positive": 12, "negative": 3, "neutral": 27, "avg_score": 0.3125},
    "timeline": [
        {"date": "2026-08-01", "count": 10},
        {"date": "2026-08-02", "count": 20},
        {"date": "2026-08-03", "count": 12},
    ],
    "engagement": {"total_likes": 1200, "total_retweets": 34, "avg_likes": 28.57},
}


def test_render_x_analysis_panel() -> None:
    out = _run_render_x_analysis(ANALYSIS)
    # 4 sentiment chips (positive / negative / neutral / avg score).
    assert out["sentimentChips"] == 4
    assert out["authorRows"] == 2
    assert out["termChips"] == 2
    assert out["timelineBars"] == 3
    assert "42 tweets" in out["engagement"]
    assert "1200 likes" in out["engagement"]
    assert "34 retweets" in out["engagement"]
    assert "28.57 avg likes" in out["engagement"]


def test_render_x_analysis_empty_payload() -> None:
    out = _run_render_x_analysis(None)
    # Zeroed payload still renders all four chips; no rows/bars; no throw.
    assert out["sentimentChips"] == 4
    assert out["authorRows"] == 0
    assert out["termChips"] == 0
    assert out["timelineBars"] == 0
    assert "0 tweets" in out["engagement"]
