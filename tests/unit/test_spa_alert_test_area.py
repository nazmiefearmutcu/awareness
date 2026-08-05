"""Static + pure-logic checks: SPA alerts test area.

Each rule row's Test button now POSTs to the per-rule test endpoint
(``/alerts/rules/{id}/test`` — ignores cooldown, never persists) and renders a
result panel: fired or not, count vs threshold, a cooldown note, and the firing
detail (term, count, fired_at) when it fired. The previous all-rules trigger
survives as the renamed "Run all rules" button wired to ``/alerts/check``. All
DOM is built with el()/textContent — no innerHTML in the region.
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


def _run_rule_test_summary(payload: dict) -> dict:
    """Execute the real ruleTestSummary from app.js in node against *payload*."""
    if NODE is None:
        pytest.skip("node not available")
    app_js = APP_JS.read_text(encoding="utf-8")
    fn = _extract_function("ruleTestSummary", app_js)
    harness = (
        "const res = " + json.dumps(payload) + ";\n"
        + fn + "\n"
        + "console.log(JSON.stringify(ruleTestSummary(res)));\n"
    )
    proc = subprocess.run(  # noqa: S603, PLW1510 — fixed node binary, static harness
        [NODE, "-e", harness], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── wiring ───────────────────────────────────────────────────────────────────


def test_spa_per_rule_test_wiring() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    alerts = _alerts_slice(app_js)
    # The row Test button now runs the per-rule test, not the whole check.
    assert "testAlertRule(r.id)" in alerts
    assert "function testAlertRule(" in alerts
    # The per-rule test hits the single-rule endpoint.
    assert (
        'api("/alerts/rules/" + encodeURIComponent(ruleId) + "/test",'
        ' { method: "POST", body: "{}" })' in alerts
    )
    assert "rule does not fire right now" in alerts
    assert "rule fires right now" in alerts


def test_spa_run_all_renamed_and_kept() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    app_js = APP_JS.read_text(encoding="utf-8")
    alerts = _alerts_slice(app_js)
    # The all-rules trigger is an explicit, renamed button.
    assert 'id="al-run-all"' in html
    assert "Run all rules" in html
    assert "Run all" in html
    # Wired to the existing /alerts/check runner in initAlerts.
    assert (
        '$("#al-run-all")?.addEventListener("click", () => void runAlertsCheck())'
        in alerts
    )
    assert 'api("/alerts/check", { method: "POST", body: "{}" })' in alerts
    assert "function runAlertsCheck(" in alerts


# ── result panel ─────────────────────────────────────────────────────────────


def test_spa_result_panel_render_helpers() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    alerts = _alerts_slice(app_js)
    assert "function ruleTestSummary(" in alerts
    assert "function renderRuleTestResult(" in alerts
    # Fired vs not-fired status text.
    assert 'text: s.fired ? "FIRED" : "Not fired"' in alerts
    # Count vs threshold line.
    assert "count " in alerts and "vs threshold" in alerts
    # Cooldown note when the rule would be suppressed in a real run.
    assert "suppressed" in alerts
    assert "cooldown" in alerts
    # Firing detail: term, count, fired_at.
    assert "al-test-firing-term" in alerts
    assert "al-test-firing-count" in alerts
    assert "al-test-firing-at" in alerts
    assert "fired_at" in alerts


def test_spa_test_area_text_content_only() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    alerts = _alerts_slice(app_js)
    assert "innerHTML" not in alerts
    assert "textContent" in alerts


def test_spa_test_area_markup() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    for needle in (
        'id="al-test-panel"',
        'id="al-test-body"',
        'id="al-run-all"',
        'id="al-refresh"',
    ):
        assert needle in html


def test_spa_test_area_styles() -> None:
    css = STYLE_CSS.read_text(encoding="utf-8")
    for needle in (
        ".al-test-panel",
        ".al-test-fired",
        ".al-test-clean",
        ".al-test-cooldown",
        ".al-test-firing",
        ".al-test-firing-term",
    ):
        assert needle in css


# ── pure logic (node-exec) ───────────────────────────────────────────────────


def test_spa_rule_test_summary_fired() -> None:
    out = _run_rule_test_summary(
        {
            "fired": True,
            "firing": {
                "id": 0,
                "rule_id": "r-btc",
                "rule_name": "BTC volume alert",
                "kind": "term_count",
                "term": "bitcoin",
                "count": 12,
                "threshold": 10,
                "fired_at": "2026-08-04T10:00:00.000Z",
                "detail": "12 docs matched 'bitcoin'",
            },
            "count": 12,
            "threshold": 10,
            "suppressed_by_cooldown": True,
        }
    )
    assert out["fired"] is True
    assert out["count"] == 12
    assert out["threshold"] == 10
    assert out["suppressed"] is True
    assert out["term"] == "bitcoin"
    assert out["firingCount"] == 12
    assert out["firedAt"] == "2026-08-04T10:00:00.000Z"


def test_spa_rule_test_summary_not_fired() -> None:
    out = _run_rule_test_summary(
        {"fired": False, "firing": None, "count": 3, "threshold": 10, "suppressed_by_cooldown": False}
    )
    assert out["fired"] is False
    assert out["count"] == 3
    assert out["threshold"] == 10
    assert out["suppressed"] is False
    assert out["term"] is None
    assert out["firingCount"] is None
    assert out["firedAt"] is None


def test_spa_rule_test_summary_empty_payload() -> None:
    out = _run_rule_test_summary({})
    assert out["fired"] is False
    assert out["count"] is None
    assert out["threshold"] is None
    assert out["suppressed"] is False
    assert out["term"] is None
