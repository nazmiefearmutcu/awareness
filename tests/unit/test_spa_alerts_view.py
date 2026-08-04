"""Static checks: SPA Alerts view (route, rules CRUD, check, firings)."""

from pathlib import Path

APP_JS = Path("src/awareness/api/web/app.js")
INDEX_HTML = Path("src/awareness/api/web/index.html")
STYLE_CSS = Path("src/awareness/api/web/style.css")


def _alerts_slice(app_js: str) -> str:
    start = app_js.index("// ── Alerts")
    end = app_js.index("// ── Settings")
    return app_js[start:end]


def test_spa_alerts_route_registered() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    route_line = next(
        line for line in app_js.splitlines() if line.startswith("const ROUTES")
    )
    assert '"alerts"' in route_line
    # Ordering: alerts between analytics and saved (nav order == shortcut order).
    assert '"analytics", "alerts", "saved", "x", "settings"' in route_line
    assert "if (route === \"alerts\") void initAlerts();" in app_js
    # Number shortcuts cover the new 9th route (settings now 9).
    assert "/^[1-9]$/" in app_js


def test_spa_alerts_view_marks_lazy_load() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "async function initAlerts()" in app_js
    assert "let alertsReady = false;" in app_js
    assert "alertsReady" in _alerts_slice(app_js)


def test_spa_alerts_crud_endpoints() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    alerts = _alerts_slice(app_js)
    assert 'api("/alerts/rules")' in alerts
    assert 'api("/alerts/rules", { method: "POST"' in alerts
    assert 'api("/alerts/rules/" + encodeURIComponent(ruleId),' in alerts
    assert 'method: "PUT"' in alerts
    assert 'method: "DELETE"' in alerts
    assert 'api("/alerts/check", { method: "POST", body: "{}" })' in alerts
    assert 'api("/alerts/status")' in alerts
    assert 'api("/alerts/firings?limit=20")' in alerts


def test_spa_alerts_handlers_exist() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    alerts = _alerts_slice(app_js)
    for fn in (
        "loadAlertsView",
        "renderAlertsRules",
        "updateAlertsStatus",
        "renderAlertsFirings",
        "toggleAlertRule",
        "deleteAlertRule",
        "runAlertsCheck",
        "createAlertRule",
    ):
        assert f"function {fn}(" in alerts
    # Delete is gated behind a confirm; active toggle writes the checkbox state.
    assert "window.confirm" in alerts


def test_spa_alerts_no_inner_html() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "innerHTML" not in _alerts_slice(app_js)


def test_spa_alerts_html_section_and_nav() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'class="view view-alerts" data-view="alerts"' in html
    assert 'id="title-alerts"' in html
    assert 'data-route="alerts"' in html
    assert "Alerts" in html
    # Shortcuts renumbered: alerts = 7, saved = 8, settings = 9.
    alerts_nav = html[html.index('data-route="alerts"'):]
    alerts_nav = alerts_nav[: alerts_nav.index("</button>")]
    assert '<span class="nav-shortcut">7</span>' in alerts_nav
    settings_nav = html[html.index('data-route="settings"'):]
    settings_nav = settings_nav[: settings_nav.index("</button>")]
    assert '<span class="nav-shortcut">10</span>' in settings_nav


def test_spa_alerts_static_markup() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    for needle in (
        'id="al-rules-body"',
        'id="al-form"',
        'id="al-kind"',
        'id="al-term"',
        'id="al-threshold"',
        'id="al-window"',
        'id="al-cooldown"',
        'id="al-webhook"',
        'id="al-active"',
        'id="al-test-panel"',
        'id="al-firings-body"',
        'id="al-status-total"',
        'id="al-refresh"',
    ):
        assert needle in html


def test_spa_alerts_styles_present() -> None:
    css = STYLE_CSS.read_text(encoding="utf-8")
    for needle in (
        ".view-alerts",
        ".al-status-strip",
        ".al-form-grid",
        ".al-table",
        ".al-toggle",
        ".al-test-panel",
    ):
        assert needle in css
