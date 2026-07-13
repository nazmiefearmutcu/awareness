"""Static checks: SPA empty-search diagnostics surface mode/corpus/phrase."""

from pathlib import Path


APP_JS = Path("src/awareness/api/web/app.js")
STYLE_CSS = Path("src/awareness/api/web/style.css")


def test_spa_empty_diagnostics_shows_mode_corpus_and_phrase_fallback() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")

    assert "function renderCapsDiagnostics(data, isSearch)" in app_js
    # Meta line mirrors CLI empty-state (mode / corpus / window).
    assert "mode=" in app_js and "formatSearchModeLabel(modeUsed" in app_js
    assert "corpus=" in app_js
    assert "window=" in app_js
    assert "caps-diagnostics-meta" in app_js
    # Phrase mode must remain informative even if hints are empty.
    assert 'modeUsed.toLowerCase() === "phrase"' in app_js
    assert "No exact phrase matches" in app_js
    # Do not hide the panel solely because hints[] is empty when meta exists.
    assert "if (!metaParts.length && !hints.length)" in app_js


def test_spa_diagnostics_meta_style_present() -> None:
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".caps-diagnostics-meta" in css
