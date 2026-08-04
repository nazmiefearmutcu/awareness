"""Static + pure-logic checks: SPA firings-log expandable detail panel.

The firings log (alerts view) gained per-row expandable detail rows (full
untruncated detail text, rule_id, count vs threshold, fired_at local + UTC, and
a "View rule" link that highlights/scrolls the rule row), a Refresh button, and
a limit bump from 20 to 50. All DOM is built with el()/textContent — no
innerHTML in the region.
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

SAMPLE_FIRING = {
    "id": 7,
    "rule_id": "r-btc-volume",
    "rule_name": "BTC volume alert",
    "kind": "term_count",
    "term": "bitcoin",
    "count": 12,
    "threshold": 10,
    "fired_at": "2026-08-04T10:00:00.000Z",
    "detail": 'term "bitcoin" count 12 exceeded threshold 10 in the last 24h window — '
    "this is the full untruncated detail text that never reaches an HTML parser.",
}


def _alerts_slice(app_js: str) -> str:
    start = app_js.index("// ── Alerts")
    end = app_js.index("// ── Settings")
    return app_js[start:end]


def _firings_slice(app_js: str) -> str:
    """Firings region: from the first firing-detail helper to Saved searches."""
    start = app_js.index("function firingDetailFields(")
    end = app_js.index("// ── Saved searches")
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


def _run_firing_detail_fields(firing: dict) -> dict:
    """Execute the real firingDetailFields from app.js in node against *firing*."""
    if NODE is None:
        pytest.skip("node not available")
    app_js = APP_JS.read_text(encoding="utf-8")
    fn = _extract_function("firingDetailFields", app_js)
    harness = (
        "const f = " + json.dumps(firing) + ";\n"
        + fn + "\n"
        + "console.log(JSON.stringify(firingDetailFields(f)));\n"
    )
    proc = subprocess.run(  # noqa: S603, PLW1510 — fixed node binary, static harness
        [NODE, "-e", harness], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_spa_firings_fetch_limit_50() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    alerts = _alerts_slice(app_js)
    # Both the view load and the log Refresh hit the backend with limit=50
    # (backend clamps 1..500); the old hardcoded 20 is gone.
    assert 'api("/alerts/firings?limit=50")' in alerts
    assert "firings?limit=20" not in alerts


def test_spa_firings_rows_expandable() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    firings = _firings_slice(app_js)
    # Every firing row gets its own detail row + click toggle.
    assert "function buildFiringDetailRow(" in firings
    assert 'addEventListener("click", toggle)' in firings
    assert "detailRow.hidden = !open" in firings
    assert 'classList.toggle("is-open"' in firings
    # Keyboard toggle (Enter/Space) for focusable rows.
    assert 'addEventListener("keydown"' in firings
    assert 'ev.key === "Enter" || ev.key === " "' in firings


def test_spa_firing_detail_text_content_only() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    firings = _firings_slice(app_js)
    assert "innerHTML" not in firings
    # Full untruncated detail rendered as a text node (el() text: → textContent).
    assert 'class: "al-firing-detail-text"' in firings
    assert "text: fields.detail" in firings


def test_spa_firing_detail_fields_pure() -> None:
    out = _run_firing_detail_fields(SAMPLE_FIRING)
    assert out["detail"] == SAMPLE_FIRING["detail"]  # full, untruncated
    assert out["ruleId"] == "r-btc-volume"
    assert out["count"] == 12
    assert out["threshold"] == 10
    assert out["utc"] == "2026-08-04T10:00:00.000Z"


def test_spa_firing_detail_fields_local_utc() -> None:
    if NODE is None:
        pytest.skip("node not available")
    app_js = APP_JS.read_text(encoding="utf-8")
    fn = _extract_function("firingDetailFields", app_js)
    harness = (
        "const f = " + json.dumps(SAMPLE_FIRING) + ";\n"
        + fn + "\n"
        + "const t = new Date(f.fired_at);\n"
        + "console.log(JSON.stringify({"
        + "local: firingDetailFields(f).local,"
        + "expectLocal: t.toLocaleString(),"
        + "utc: firingDetailFields(f).utc,"
        + "expectUtc: t.toISOString()}));\n"
    )
    proc = subprocess.run(  # noqa: S603, PLW1510 — fixed node binary, static harness
        [NODE, "-e", harness], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["local"] == out["expectLocal"]
    assert out["utc"] == out["expectUtc"] == "2026-08-04T10:00:00.000Z"


def test_spa_firing_detail_fields_missing_optional() -> None:
    out = _run_firing_detail_fields({"rule_id": "r-x", "detail": ""})
    assert out["detail"] == "—"
    assert out["local"] == "—"
    assert out["utc"] == "—"
    assert out["count"] is None
    assert out["threshold"] is None


def test_spa_firing_view_rule_wiring() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    alerts = _alerts_slice(app_js)
    firings = _firings_slice(app_js)
    # Detail panel carries the link-styled View rule button wired to the rule id.
    assert 'text: "View rule"' in firings
    assert "focusAlertRule(f.rule_id)" in firings
    # Rules rows are tagged with their id so the rules table can be navigated.
    assert "row.dataset.ruleId = r.id;" in alerts
    # focusAlertRule highlights the matching row and scrolls it into view.
    assert "function focusAlertRule(" in alerts
    assert "scrollIntoView" in alerts
    assert 'classList.add("is-focused")' in alerts


def test_spa_firings_refresh_button() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    app_js = APP_JS.read_text(encoding="utf-8")
    alerts = _alerts_slice(app_js)
    assert 'id="al-firings-refresh"' in html
    assert (
        '$("#al-firings-refresh")?.addEventListener("click", () => void loadFiringsLog())'
        in alerts
    )
    assert "async function loadFiringsLog(" in alerts
    # The refresh path re-fetches the log (50 newest), not the whole view.
    assert 'api("/alerts/firings?limit=50")' in alerts
    assert "renderAlertsFirings(firings || [])" in alerts


def test_spa_firings_detail_styles() -> None:
    css = STYLE_CSS.read_text(encoding="utf-8")
    for needle in (
        ".al-firing-detail-row",
        ".al-firing-detail-text",
        ".al-firing-view-rule",
        "tr.is-focused",
    ):
        assert needle in css
