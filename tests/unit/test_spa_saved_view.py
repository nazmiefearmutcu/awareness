"""Static checks: SPA Saved-searches view (route, CRUD/pin/run endpoints,
bookmark control in the captures search bar, no innerHTML)."""

from pathlib import Path

APP_JS = Path("src/awareness/api/web/app.js")
INDEX_HTML = Path("src/awareness/api/web/index.html")
STYLE_CSS = Path("src/awareness/api/web/style.css")


def _saved_slice(app_js: str) -> str:
    start = app_js.index("// ── Saved searches")
    end = app_js.index("// ── Settings")
    return app_js[start:end]


def test_spa_saved_route_registered() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    route_line = next(
        line for line in app_js.splitlines() if line.startswith("const ROUTES")
    )
    assert '"saved"' in route_line
    # Ordering: saved between alerts and settings (nav order == shortcut order).
    assert '"alerts", "saved", "settings"' in route_line
    assert "if (route === \"saved\") void initSaved();" in app_js
    # Number shortcuts cover the new 9th route (settings now 9).
    assert "/^[1-9]$/" in app_js


def test_spa_saved_view_marks_lazy_load() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "async function initSaved()" in app_js
    assert "let savedReady = false;" in app_js
    assert "savedReady" in _saved_slice(app_js)


def test_spa_saved_crud_endpoints() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    saved = _saved_slice(app_js)
    assert 'api("/saved")' in saved
    assert 'api("/saved/" + encodeURIComponent(' in saved
    assert 'method: "PUT"' in saved
    assert 'method: "DELETE"' in saved
    assert 'api("/saved/" + encodeURIComponent(savedId) + "/pin",' in saved
    assert 'api("/saved/" + encodeURIComponent(s.id) + "/run")' in saved
    # Bookmarking POST lives in the captures search-bar wiring (full file).
    assert 'api("/saved", { method: "POST", body: JSON.stringify(body) })' in app_js


def test_spa_saved_handlers_exist() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    saved = _saved_slice(app_js)
    for fn in (
        "initSaved",
        "loadSavedView",
        "renderSavedList",
        "toggleSavedPin",
        "runSaved",
        "editSavedName",
        "deleteSaved",
    ):
        assert f"function {fn}(" in saved
    # Delete is gated behind a confirm; run reuses the shared results renderer.
    assert "window.confirm" in saved
    assert "renderCaps(list, rows, { search:" in saved


def test_spa_saved_no_inner_html() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "innerHTML" not in _saved_slice(app_js)


def test_spa_saved_search_bar_save_control() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    # Button + inline name input live next to the captures search box.
    assert 'id="saved-save-btn"' in html
    assert 'id="saved-save-name"' in html
    assert 'id="saved-save-ok"' in html
    assert 'id="saved-save-cancel"' in html
    # The button only opens the control when a query is present.
    assert 'api("/saved", { method: "POST", body: JSON.stringify(body) })' in app_js
    assert "async function saveCurrentSearch()" in app_js
    assert "limit: caps.limit" in app_js


def test_spa_saved_html_section_and_nav() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'class="view view-saved" data-view="saved"' in html
    assert 'id="title-saved"' in html
    assert 'data-route="saved"' in html
    assert 'id="saved-list"' in html
    assert 'id="saved-run-list"' in html
    # Nav ordering: saved after alerts; shortcuts renumbered (settings = 9).
    alerts_nav = html[html.index('data-route="alerts"'):]
    alerts_nav = alerts_nav[: alerts_nav.index("</button>")]
    assert '<span class="nav-shortcut">7</span>' in alerts_nav
    saved_nav = html[html.index('data-route="saved"'):]
    saved_nav = saved_nav[: saved_nav.index("</button>")]
    assert '<span class="nav-shortcut">8</span>' in saved_nav
    settings_nav = html[html.index('data-route="settings"'):]
    settings_nav = settings_nav[: settings_nav.index("</button>")]
    assert '<span class="nav-shortcut">9</span>' in settings_nav


def test_spa_saved_styles_present() -> None:
    css = STYLE_CSS.read_text(encoding="utf-8")
    for needle in (
        ".view-saved",
        ".saved-list",
        ".saved-card",
        ".saved-pin",
        ".saved-query",
        ".saved-actions",
        ".saved-save-btn",
        ".saved-save-control",
        ".saved-run-meta",
        ".sv-run-band",
    ):
        assert needle in css
