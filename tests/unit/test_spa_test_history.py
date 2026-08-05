"""Static + pure-logic checks: SPA alerts "Recent tests" history panel.

Tests are not persisted server-side (the /alerts/rules/{id}/test endpoint
never writes), so the panel keeps a client-side record in sessionStorage
under ``awareness:testHistory`` (max 20 entries, newest first). Every test
response is pushed via ``pushTestHistory``; ``renderTestHistory`` fills the
panel's tbody (el()/textContent only — no innerHTML in the alerts region).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path("src/awareness/api/web/app.js")
INDEX_HTML = Path("src/awareness/api/web/index.html")
STYLE_CSS = Path("src/awareness/api/web/style.css")

NODE = shutil.which("node")


def _alerts_slice(app_js: str) -> str:
    start = app_js.index("// ── Alerts")
    end = app_js.index("// ── Settings")
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


def _node(script: str) -> str:
    """Run *script* in node; return the full stdout (line per console.log)."""
    if NODE is None:
        pytest.skip("node not available")
    proc = subprocess.run(  # noqa: S603, PLW1510 — fixed node binary, static harness
        [NODE, "-e", script], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


# Shared node preamble: mocked sessionStorage + a minimal DOM so the real
# el()/clear()/renderTestHistory() run unmodified outside a browser.
_NODE_PREAMBLE = r"""
const store = {};
const sessionStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};
function fakeNode(tag) {
  const node = {
    tagName: String(tag || "div").toUpperCase(),
    children: [],
    className: "",
    _text: null,
    setAttribute() {},
    addEventListener() {},
    get textContent() { return node._text; },
    set textContent(v) { node._text = String(v); node.children = []; },
    get firstChild() { return node.children[0] || null; },
    removeChild(c) {
      const i = node.children.indexOf(c);
      if (i >= 0) node.children.splice(i, 1);
      return c;
    },
    appendChild(c) { node.children.push(c); return c; },
    insertRow() { const r = fakeNode("tr"); node.children.push(r); return r; },
    insertCell() { const c = fakeNode("td"); node.children.push(c); return c; },
  };
  return node;
}
const document = {
  createElement: (t) => fakeNode(t),
  createTextNode: (t) => String(t),
};
function describeRows(container) {
  return (container.children || []).map((row) => ({
    cls: row.className || null,
    cells: (row.children || []).map((c) => ({
      cls: c.className || null,
      text: c._text == null ? "" : String(c._text),
    })),
  }));
}
"""


def _consts(app_js: str) -> str:
    """Top-level TEST_HISTORY_* consts + the fmt() helper, verbatim from app.js."""
    key = re.search(r'const TEST_HISTORY_KEY = "[^"]*";', app_js)
    limit = re.search(r"const TEST_HISTORY_MAX = \d+;", app_js)
    fmt = re.search(r"^const fmt = .*;$", app_js, flags=re.MULTILINE)
    assert key and limit and fmt, "app.js must define the TEST_HISTORY_* consts and fmt()"
    return f"{key.group(0)}\n{limit.group(0)}\n{fmt.group(0)}\n"


def _history_harness(app_js: str, *fns: str) -> str:
    """Node preamble + the real TEST_HISTORY_* consts + extracted *fns*."""
    body = "".join(_extract_function(fn, app_js) for fn in fns)
    return _NODE_PREAMBLE + _consts(app_js) + body


# ── wiring ───────────────────────────────────────────────────────────────────


def test_spa_history_panel_html() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    for needle in (
        'id="al-history-panel"',
        'id="al-history-title"',
        'id="al-history-clear"',
        'id="al-history-body"',
        "Recent tests",
    ):
        assert needle in html
    # Panel sits directly under the test-result panel.
    assert html.index('id="al-history-panel"') > html.index('id="al-test-panel"')


def test_spa_history_functions_and_wiring() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    alerts = _alerts_slice(app_js)
    for needle in (
        "function pushTestHistory(",
        "function renderTestHistory(",
        "function clearTestHistory(",
        "function ruleTestHistoryEntry(",
        'const TEST_HISTORY_KEY = "awareness:testHistory";',
        "const TEST_HISTORY_MAX = 20;",
    ):
        assert needle in alerts
    # Every successful per-rule test records + re-renders the history.
    assert "pushTestHistory(ruleTestHistoryEntry(ruleId, ruleName, res))" in alerts
    assert "renderTestHistory($(\"#al-history-body\"))" in alerts
    # Clear button wiring lives in initAlerts.
    assert '"#al-history-clear"' in alerts
    assert "clearTestHistory();" in alerts


def test_spa_history_text_content_only() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    alerts = _alerts_slice(app_js)
    assert "innerHTML" not in alerts
    assert "textContent" in alerts


def test_spa_history_styles() -> None:
    css = STYLE_CSS.read_text(encoding="utf-8")
    for needle in (
        ".al-history-panel",
        ".al-history-table",
        ".al-history-fired",
        ".al-history-clean",
        ".al-history-row",
    ):
        assert needle in css


# ── pure logic (node-exec) ───────────────────────────────────────────────────


def test_spa_push_test_history_persists_newest_first() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    script = (
        _history_harness(app_js, "readTestHistory", "pushTestHistory")
        + """
console.log(JSON.stringify(pushTestHistory({rule_id: "r1", fired: true, at: "2026-08-05T07:00:00Z"})));
console.log(JSON.stringify(pushTestHistory({rule_id: "r2", fired: false, at: "2026-08-05T08:00:00Z"})));
console.log(store["awareness:testHistory"]);
"""
    )
    first, second, stored = _node(script).splitlines()
    assert json.loads(first)[0]["rule_id"] == "r1"
    assert json.loads(second)[0]["rule_id"] == "r2"
    persisted = json.loads(stored)
    assert [e["rule_id"] for e in persisted] == ["r2", "r1"]  # newest first


def test_spa_push_test_history_max_20_eviction() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    pushes = "".join(
        f'pushTestHistory({{rule_id: "r{i}", fired: {(i % 2 == 0 and "true") or "false"}}});\n'
        for i in range(25)
    )
    script = (
        _history_harness(app_js, "readTestHistory", "pushTestHistory")
        + "\n"
        + pushes
        + 'console.log(store["awareness:testHistory"]);\n'
    )
    persisted = json.loads(_node(script).splitlines()[-1])
    assert len(persisted) == 20
    # Newest kept (r24 first), oldest evicted (r0..r4 gone).
    assert persisted[0]["rule_id"] == "r24"
    assert persisted[-1]["rule_id"] == "r5"
    assert {e["rule_id"] for e in persisted} == {f"r{i}" for i in range(5, 25)}


def test_spa_push_test_history_survives_malformed_storage() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    script = (
        _history_harness(app_js, "readTestHistory", "pushTestHistory")
        + """
store["awareness:testHistory"] = "{not json";
console.log(JSON.stringify(pushTestHistory({rule_id: "r1", fired: true})));
console.log(store["awareness:testHistory"]);
"""
    )
    out, stored = _node(script).splitlines()
    assert json.loads(out)[0]["rule_id"] == "r1"
    assert json.loads(stored) == [{"rule_id": "r1", "fired": True}]


def test_spa_history_entry_shape() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    script = (
        _history_harness(app_js, "ruleTestSummary", "ruleTestHistoryEntry")
        + """
const res = {
  fired: true,
  firing: { term: "bitcoin", count: 12, fired_at: "2026-08-05T07:00:00.000Z" },
  count: 12,
  threshold: 10,
  suppressed_by_cooldown: true,
};
console.log(JSON.stringify(ruleTestHistoryEntry("r-btc", "BTC alert", res)));
const clean = { fired: false, firing: null, count: 2, threshold: 10 };
console.log(JSON.stringify(ruleTestHistoryEntry("r-eth", "", clean)));
"""
    )
    fired, clean = _node(script).splitlines()
    entry = json.loads(fired)
    assert entry["rule_id"] == "r-btc"
    assert entry["rule_name"] == "BTC alert"
    assert entry["term"] == "bitcoin"
    assert entry["fired"] is True
    assert entry["count"] == 12
    assert entry["threshold"] == 10
    assert "T" in entry["at"] and entry["at"].endswith("Z")  # ISO timestamp
    no_name = json.loads(clean)
    assert no_name["rule_name"] == "r-eth"  # falls back to the rule id
    assert no_name["fired"] is False


def test_spa_render_test_history_empty_state() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    script = (
        _history_harness(app_js, "readTestHistory", "clear", "renderTestHistory")
        + """
const container = fakeNode("tbody");
renderTestHistory(container);
console.log(JSON.stringify(describeRows(container)));
"""
    )
    rows = json.loads(_node(script).splitlines()[-1])
    assert len(rows) == 1
    assert rows[0]["cells"][0]["text"] == (
        "No tests yet — press Test on a rule to record it here."
    )


def test_spa_render_test_history_rows() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    script = (
        _history_harness(app_js, "readTestHistory", "clear", "renderTestHistory")
        + """
store["awareness:testHistory"] = JSON.stringify([
  {rule_id: "r-btc", rule_name: "BTC alert", term: "bitcoin", fired: true,
   count: 12, threshold: 10, at: "2026-08-05T07:00:00.000Z"},
  {rule_id: "r-eth", rule_name: "ETH alert", term: "ether", fired: false,
   count: 2, threshold: 10, at: "2026-08-05T06:00:00.000Z"},
]);
const container = fakeNode("tbody");
renderTestHistory(container);
console.log(JSON.stringify(describeRows(container)));
"""
    )
    rows = json.loads(_node(script).splitlines()[-1])
    assert len(rows) == 2
    fired_row, clean_row = rows
    assert fired_row["cls"] == "al-history-row is-fired"
    assert [c["text"] for c in fired_row["cells"]] == [
        "2026-08-05 07:00:00",
        "BTC alert",
        "bitcoin",
        "FIRED",
        "12",
        "10",
    ]
    # Fired result cell carries the fired style class.
    assert fired_row["cells"][3]["cls"] == "al-history-fired"
    assert clean_row["cls"] == "al-history-row"
    assert clean_row["cells"][3] == {"cls": "al-history-clean", "text": "clean"}


def test_spa_clear_test_history_wires_clear_and_rerender() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    alerts = _alerts_slice(app_js)
    # The Clear button handler drops the store and re-renders the empty table.
    assert (
        '$("#al-history-clear")?.addEventListener("click", () => {' in alerts
    )
    assert "clearTestHistory();" in alerts
    assert "renderTestHistory($(\"#al-history-body\"));" in alerts
    # And the panel is seeded from sessionStorage on view init.
    assert "renderTestHistory($(\"#al-history-body\"));" in alerts
