"""Static checks: capture reader related panel is collapsible."""

from pathlib import Path


APP_JS = Path("src/awareness/api/web/app.js")
CSS = Path("src/awareness/api/web/style.css")


def test_related_panel_uses_details_summary() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert 'el("details", { class: "reader-related" })' in app_js
    assert 'el("summary", { class: "related-summary" })' in app_js
    assert "related-summary-count" in app_js
    assert "AUTO_OPEN_MAX" in app_js
    assert "detailsEl.open" in app_js
    # loadRelated receives details + count nodes for collapse control.
    assert "void loadRelated(cid, relatedBody, relatedDetails, relatedCount)" in app_js
    assert "async function loadRelated(cid, host, detailsEl, countEl)" in app_js


def test_related_panel_collapse_styles() -> None:
    css = CSS.read_text(encoding="utf-8")
    assert ".related-summary" in css
    assert ".reader-related[open]" in css
    assert "max-height" in css
    assert ".related-body" in css
