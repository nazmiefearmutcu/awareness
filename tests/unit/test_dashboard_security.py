"""Static security checks for the dashboard client."""

from pathlib import Path


APP_JS = Path("src/awareness/api/web/app.js")


def test_capture_source_links_validate_allowed_protocols() -> None:
    app_js = APP_JS.read_text()

    assert "function safeHrefAttribute(value)" in app_js
    assert "function safeOutboundHref(value)" in app_js
    assert 'url.protocol === "http:" || url.protocol === "https:"' in app_js
    assert 'else if (k === "href")' in app_js
    assert "const href = safeHrefAttribute(v);" in app_js
    assert "const href = safeOutboundHref(value);" in app_js
    assert 'el("a", { href, target: "_blank", rel: "noopener" })' in app_js
    assert 'el("a", { href: value, target: "_blank", rel: "noopener" })' not in app_js
