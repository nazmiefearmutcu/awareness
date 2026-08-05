"""Static checks: SPA dashboard "Saved briefings" band.

Follows the ``test_spa_*`` pattern: HTML band structure + position (directly
under the "Today at a glance" band), app.js wiring (the /briefings list and
per-date read endpoints), the click-to-view collapsible viewer, renderChips
reuse with the analytics deep-link, the no-innerHTML rule for the region, and
the 12-tick refresh guard mirroring the glance/quality/saved bands.
"""

from __future__ import annotations

from pathlib import Path

APP_JS = Path("src/awareness/api/web/app.js")
INDEX_HTML = Path("src/awareness/api/web/index.html")
STYLE_CSS = Path("src/awareness/api/web/style.css")


def _briefings_slice(app_js: str) -> str:
    start = app_js.index("// ── Saved briefings band")
    end = app_js.index("// ── Live activity feed", start)
    return app_js[start:end]


def test_briefings_band_in_html_under_glance_band() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "Saved briefings" in html
    for needle in (
        'id="dash-briefings-band"',
        'id="dash-briefings-title"',
        'id="dash-briefings-meta"',
        'id="dash-briefings-list"',
        'id="dash-briefings-viewer"',
        'id="dash-briefings-movers"',
        'id="dash-briefings-terms"',
        'id="dash-briefings-domains"',
    ):
        assert needle in html, f"missing {needle}"
    # Band sits under the glance band and above the saved band.
    assert html.index('id="dash-glance-band"') < html.index('id="dash-briefings-band"')
    assert html.index('id="dash-briefings-band"') < html.index('id="dash-saved-band"')
    # The viewer is a natively collapsible details/summary panel.
    assert "<details" in html
    assert "<summary" in html


def test_briefings_js_present_and_endpoints_used() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    band = _briefings_slice(app_js)
    assert "function renderDashBriefings(" in band
    assert "async function openDashBriefing(" in band
    assert "function renderDashBriefingDetail(" in band
    assert "async function refreshDashBriefings(" in band
    assert 'api("/briefings")' in band
    assert 'api("/briefings/" + encodeURIComponent(date))' in band


def test_briefings_click_to_view_wiring() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    band = _briefings_slice(app_js)
    # Date chips open the collapsible viewer for their date and fetch it.
    assert "() => void openDashBriefing(b.date)" in band
    assert '$("#dash-briefings-viewer")' in band
    assert "viewer.open = true" in band
    assert 'api("/briefings/" + encodeURIComponent(date))' in band
    assert "renderDashBriefingDetail(res)" in band


def test_briefings_terms_chips_reuse_render_chips() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    band = _briefings_slice(app_js)
    # Top-term chips are rendered through the shared renderChips helper.
    assert 'renderChips($("#dash-briefings-terms"),' in band
    assert "key: (x) => (x && x.term) || x" in band
    # Picking a chip fills the analytics term input, navigates to the
    # Analytics view, and triggers the lifecycle query (glance-band pattern).
    assert '$("#an-term-input").value = term' in band
    assert 'navigate("analytics")' in band
    assert "void loadLifecycle();" in band


def test_briefings_refresh_hooked_into_dashboard_cadence() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    band = _briefings_slice(app_js)
    # Same 12-tick rebuild guard as the glance/quality/saved bands (60 s at
    # a 5 s cadence).
    assert "dashBriefingsTick % 12 !== 0" in band
    assert "if (sig === dashBriefingsSig && dashBriefingsTick % 12 !== 0) return;" in band
    # refreshDashboard drives it, non-fatal on failure.
    assert "void refreshDashBriefings();" in app_js
    assert "// Saved briefings band (same cadence guard)." in app_js


def test_briefings_region_no_innerhtml() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    band = _briefings_slice(app_js)
    assert "innerHTML" not in band
    assert "el(" in band


def test_briefings_styles_present() -> None:
    css = STYLE_CSS.read_text(encoding="utf-8")
    for needle in (
        ".dash-briefings-band",
        ".dash-briefings-chip",
        ".dash-briefings-viewer",
        ".dash-briefings-viewer-summary",
        ".dash-briefings-mover",
    ):
        assert needle in css
