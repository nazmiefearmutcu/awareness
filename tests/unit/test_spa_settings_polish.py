"""Static checks: SPA Settings dirty state, sticky hero, Cmd/Ctrl+S save."""

from pathlib import Path


APP_JS = Path("src/awareness/api/web/app.js")
STYLE_CSS = Path("src/awareness/api/web/style.css")
INDEX_HTML = Path("src/awareness/api/web/index.html")


def test_spa_settings_dirty_state_and_badge() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    css = STYLE_CSS.read_text(encoding="utf-8")

    assert "function setSettingsDirty(" in app_js
    assert "function initSettingsDirtyWatchers(" in app_js
    assert "settingsDirty" in app_js
    assert "Unsaved changes" in app_js or "unsaved" in app_js.lower()
    assert 'id="set-dirty-badge"' in html
    assert ".set-dirty-badge" in css
    assert "has-unsaved" in app_js and "has-unsaved" in css


def test_spa_settings_cmd_s_saves() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert 'e.key === "s"' in app_js or 'e.key === "S"' in app_js
    assert "saveAllSettings" in app_js
    # Shortcut is gated to the settings route / view.
    assert 'currentRoute === "settings"' in app_js
    assert "Ctrl+S" in INDEX_HTML.read_text(encoding="utf-8") or "⌘S" in INDEX_HTML.read_text(
        encoding="utf-8"
    )


def test_spa_settings_sticky_hero() -> None:
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert "position: sticky" in css
    # Hero is the sticky target so Save stays reachable while scrolling knobs.
    assert ".set-hero" in css


def test_spa_settings_beforeunload_when_dirty() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "beforeunload" in app_js
    assert "settingsDirty" in app_js


def test_spa_settings_clear_dirty_after_successful_save() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    # Successful path clears dirty; error path keeps it.
    assert "setSettingsDirty(false)" in app_js
    assert "setSettingsDirty(true)" in app_js
