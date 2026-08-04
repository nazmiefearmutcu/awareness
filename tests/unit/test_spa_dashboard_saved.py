"""Static checks: dashboard Saved-search widgets (band markup after the
feed-health band, /saved + /saved/{id}/run wiring, chip rendering via
textContent, refresh hook with rebuild guard)."""

from pathlib import Path

APP_JS = Path("src/awareness/api/web/app.js")
INDEX_HTML = Path("src/awareness/api/web/index.html")
STYLE_CSS = Path("src/awareness/api/web/style.css")


def _dash_saved_slice(app_js: str) -> str:
    start = app_js.index("// ── Dashboard saved widgets")
    end = app_js.index("// ── Live activity feed")
    return app_js[start:end]


def test_spa_dash_saved_band_markup() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="dash-saved-band"' in html
    assert "Saved searches" in html
    assert 'id="dash-saved-chips"' in html
    assert 'id="dash-saved-results"' in html
    assert 'id="dash-saved-results-list"' in html
    # The band sits right after the feed-health band, before the dash split.
    assert html.index('id="dash-saved-band"') > html.index('id="feed-health-band"')
    assert html.index('id="dash-saved-band"') < html.index("dash-split")


def test_spa_dash_saved_endpoints() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    dash = _dash_saved_slice(app_js)
    assert 'api("/saved")' in dash
    assert 'api("/saved/" + encodeURIComponent(s.id) + "/run")' in dash


def test_spa_dash_saved_chips_via_text_content() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    dash = _dash_saved_slice(app_js)
    # Chip label = name + query built from textContent, never innerHTML.
    assert "text: " in dash
    assert "${s.name || \"—\"} · ${truncateText(s.query || \"\", 60)}" in dash
    assert "innerHTML" not in dash


def test_spa_dash_saved_refresh_hook_present() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    dash = _dash_saved_slice(app_js)
    assert "async function refreshDashSaved()" in dash
    # Called from the dashboard refresh cadence (every 5 s), non-blocking.
    assert "void refreshDashSaved();" in app_js
    # Rebuild guard: re-render only when the list signature changed or on
    # a slow tick (every 12th = 60 s).
    assert "dashSavedSig" in dash
    assert "dashSavedTick" in dash
    assert "dashSavedTick % 12 !== 0" in dash
    # Results render inline in a compact list (title link, domain, date).
    assert "function renderDashSavedResults(" in dash
    assert "dash-saved-result-title" in dash


def test_spa_dash_saved_styles_present() -> None:
    css = STYLE_CSS.read_text(encoding="utf-8")
    for needle in (
        ".dash-saved-band",
        ".dash-saved-item",
        ".dash-saved-chip",
        ".dash-saved-run",
        ".dash-saved-results",
        ".dash-saved-result",
        ".dash-saved-result-title",
        ".dash-saved-result-meta",
    ):
        assert needle in css
