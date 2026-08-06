"""Static + pure-logic checks: SPA alert rule editing & duplication.

Each rule row now has Edit (fills the create form and switches it into edit
mode; submit PUTs to ``/alerts/rules/{id}`` with the full field set) and
Duplicate (POSTs a copy named "<name> (copy)" through the create path) buttons.
A Cancel affordance returns the form to create mode. Form state lives in the
module-level ``editingRuleId`` (null = create mode). All DOM is built with
el()/textContent — no innerHTML in the region.
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


def _run_pure(fn_name: str, call: str) -> dict:
    """Execute the real *fn_name* from app.js in node with *call* (a JS expr)."""
    if NODE is None:
        pytest.skip("node not available")
    app_js = APP_JS.read_text(encoding="utf-8")
    fn = _extract_function(fn_name, app_js)
    harness = (
        fn + "\n"
        + f"console.log(JSON.stringify({fn_name}({call})));\n"
    )
    proc = subprocess.run(  # noqa: S603, PLW1510 — fixed node binary, static harness
        [NODE, "-e", harness], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


RULE = {
    "id": "r-btc",
    "name": "BTC volume alert",
    "kind": "term_count",
    "term": "bitcoin",
    "threshold": 10.0,
    "window_hours": 24.0,
    "webhooks": ["https://hooks.example.com/a", "https://hooks.example.com/b"],
    "webhook_url": "https://hooks.example.com/a",
    "webhook_format": "json",
    "cooldown_minutes": 30.0,
    "active": True,
    "created_at": "2026-08-01T00:00:00Z",
    "updated_at": "2026-08-01T00:00:00Z",
}

FORM = {
    "name": "BTC volume alert",
    "kind": "term_count",
    "term": "bitcoin",
    "threshold": "10",
    "window_hours": "24",
    "cooldown_minutes": "30",
    "webhook": "https://hooks.example.com/a, https://hooks.example.com/b",
    "active": True,
}


# ── pure functions (node-exec) ───────────────────────────────────────────────


def test_spa_rule_to_form_maps_fields() -> None:
    out = _run_pure("ruleToForm", json.dumps(RULE))
    assert out["name"] == "BTC volume alert"
    assert out["kind"] == "term_count"
    assert out["term"] == "bitcoin"
    assert out["threshold"] == "10"
    assert out["window_hours"] == "24"
    assert out["cooldown_minutes"] == "30"
    # Multiple webhooks join into the single form input.
    assert out["webhook"] == "https://hooks.example.com/a, https://hooks.example.com/b"
    assert out["active"] is True


def test_spa_rule_to_form_falls_back() -> None:
    out = _run_pure("ruleToForm", json.dumps({
        "id": "r-x",
        "kind": "term_spike",
        "webhook_url": "https://hooks.example.com/a",
        "active": False,
    }))
    assert out["name"] == ""
    assert out["kind"] == "term_spike"
    assert out["term"] == ""
    assert out["threshold"] == ""
    assert out["webhook"] == "https://hooks.example.com/a"
    assert out["active"] is False


def test_spa_rule_to_form_empty() -> None:
    out = _run_pure("ruleToForm", "{}")
    assert out["name"] == ""
    assert out["kind"] == "term_count"
    assert out["webhook"] == ""
    assert out["active"] is True


def test_spa_form_to_payload_create_shape() -> None:
    out = _run_pure("formToPayload", json.dumps(FORM) + ', "create"')
    assert out == {
        "name": "BTC volume alert",
        "kind": "term_count",
        "term": "bitcoin",
        "threshold": 10.0,
        "window_hours": 24.0,
        "cooldown_minutes": 30.0,
        "webhooks": ["https://hooks.example.com/a", "https://hooks.example.com/b"],
        "active": True,
    }
    assert "id" not in out


def test_spa_form_to_payload_update_sends_all_fields() -> None:
    # Update is a full patch: the same complete field set as create.
    out = _run_pure("formToPayload", json.dumps(FORM) + ', "update"')
    assert out == {
        "name": "BTC volume alert",
        "kind": "term_count",
        "term": "bitcoin",
        "threshold": 10.0,
        "window_hours": 24.0,
        "cooldown_minutes": 30.0,
        "webhooks": ["https://hooks.example.com/a", "https://hooks.example.com/b"],
        "active": True,
    }


def test_spa_form_to_payload_webhook_split_and_trim() -> None:
    out = _run_pure("formToPayload", json.dumps({
        **FORM,
        "webhook": " https://a.example.com , , https://b.example.com ",
    }) + ', "create"')
    assert out["webhooks"] == ["https://a.example.com", "https://b.example.com"]


# ── wiring ───────────────────────────────────────────────────────────────────


def test_spa_rule_edit_state_and_helpers() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    alerts = _alerts_slice(app_js)
    # Module-level edit state (null = create mode).
    assert "let editingRuleId = null;" in alerts
    assert "function setRuleFormMode(" in alerts
    assert "editingRuleId = ruleId || null" in alerts
    assert "function fillRuleForm(" in alerts
    assert "function resetRuleForm(" in alerts
    assert "function ruleToForm(" in alerts
    assert "function formToPayload(" in alerts


def test_spa_edit_button_fills_form() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    alerts = _alerts_slice(app_js)
    assert "text: \"Edit\"" in alerts
    assert "fillRuleForm(r)" in alerts
    # The row handler scrolls the form into view after filling it.
    assert "$(\"#al-form\")?.scrollIntoView(" in alerts
    # fillRuleForm maps via ruleToForm and switches mode to the rule id.
    fill = _extract_function("fillRuleForm", app_js)
    assert "ruleToForm(rule)" in fill
    assert "setRuleFormMode(rule && rule.id ? rule.id : null)" in fill
    assert '"#al-name"' in fill
    assert '"#al-active"' in fill
    # Mode switch swaps the submit label + cancel visibility + band title.
    mode = _extract_function("setRuleFormMode", app_js)
    assert "editingRuleId = ruleId || null" in mode
    assert '"Update rule"' in mode
    assert '"Create rule"' in mode
    assert '"Edit rule"' in mode
    assert '"New rule"' in mode
    assert "cancel.hidden" in mode


def test_spa_edit_submit_puts_to_rule_url() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    alerts = _alerts_slice(app_js)
    # In edit mode the submit handler PUTs the full payload to the rule URL.
    assert "formToPayload(readRuleForm(), editingRuleId ? \"update\" : \"create\")" in alerts
    assert (
        'api("/alerts/rules/" + encodeURIComponent(editingRuleId),' in alerts
    )
    assert 'method: "PUT"' in alerts
    assert " updated`" in alerts  # toast template: `rule "..." updated`
    # Create mode still POSTs; refresh happens for both paths.
    assert 'api("/alerts/rules", { method: "POST", body: JSON.stringify(body) })' in alerts
    assert "void loadAlertsView()" in alerts
    assert "function createAlertRule(" in alerts


def test_spa_cancel_resets_to_create_mode() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    alerts = _alerts_slice(app_js)
    assert '$("#al-cancel")?.addEventListener("click", () => void resetRuleForm())' in alerts
    reset = _extract_function("resetRuleForm", app_js)
    assert "form.reset()" in reset
    assert "setRuleFormMode(null)" in reset


def test_spa_duplicate_uses_create_path() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    alerts = _alerts_slice(app_js)
    assert "text: \"Duplicate\"" in alerts
    assert "duplicateAlertRule(r)" in alerts
    assert "function duplicateAlertRule(" in alerts
    dup = _extract_function("duplicateAlertRule", app_js)
    # Same payload builder, create mode, name suffixed with " (copy)".
    assert 'formToPayload({ ...values, name: (values.name || "") + " (copy)" }, "create")' in dup
    assert 'api("/alerts/rules", { method: "POST", body: JSON.stringify(body) })' in dup
    assert '" (copy)"' in alerts
    assert "void loadAlertsView()" in dup
    # Duplicating never touches edit mode.
    assert "editingRuleId" not in dup


def test_spa_rule_edit_no_inner_html() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "innerHTML" not in _alerts_slice(app_js)


def test_spa_rule_edit_markup() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="al-cancel"' in html
    assert ">Cancel</button>" in html
    # Static default stays create; JS swaps the label in edit mode.
    assert '>Create rule</button>' in html


def test_spa_rule_edit_styles() -> None:
    css = STYLE_CSS.read_text(encoding="utf-8")
    for needle in (".al-cancel-btn", ".al-dup-btn"):
        assert needle in css
