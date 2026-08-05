"""Static + pure-logic checks: SPA "Today at a glance" briefing band.

Follows the ``test_spa_*`` pattern: HTML band structure + position on the
Dashboard, app.js wiring (the /alerts/firings + /topicx/emerging endpoints),
chip click deep-link into the Analytics lifecycle band (term fill + navigate
+ trigger), the 12-tick refresh guard mirroring the quality/saved bands, the
no-innerHTML rule for the new region, and the pure ``alertActivity``
summarizer executed in node against real firing lists.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

APP_JS = Path("src/awareness/api/web/app.js")
INDEX_HTML = Path("src/awareness/api/web/index.html")
STYLE_CSS = Path("src/awareness/api/web/style.css")

NODE = shutil.which("node")


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


def _glance_slice(app_js: str) -> str:
    start = app_js.index("// ── Today at a glance band")
    end = app_js.index("// ── Live activity feed", start)
    return app_js[start:end]


def _run_alert_activity(firings: list) -> dict:
    """Execute the real alertActivity summarizer from app.js in node."""
    if NODE is None:
        pytest.skip("node not available")
    app_js = APP_JS.read_text(encoding="utf-8")
    fn = _extract_function("alertActivity", app_js)
    harness = (
        "const firings = " + json.dumps(firings) + ";\n"
        + fn + "\n"
        + "console.log(JSON.stringify(alertActivity(firings)));\n"
    )
    proc = subprocess.run(  # noqa: S603 - harness is our own extracted fn + JSON data
        [NODE, "-e", harness], capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _hours_ago_iso(hours: int) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def test_glance_band_in_html_between_quality_and_saved() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "Today at a glance" in html
    for needle in (
        'id="dash-glance-band"',
        'id="dash-glance-title"',
        'id="dash-glance-meta"',
        'id="dash-glance-alerts"',
        'id="dash-glance-topics"',
    ):
        assert needle in html, f"missing {needle}"
    # Band sits under the quality-history band and above the saved band.
    assert html.index('id="dash-quality-band"') < html.index('id="dash-glance-band"')
    assert html.index('id="dash-glance-band"') < html.index('id="dash-saved-band"')


def test_glance_js_present_and_endpoints_used() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    glance = _glance_slice(app_js)
    assert "function alertActivity(" in glance
    assert "function renderDashGlance(" in glance
    assert "async function refreshDashGlance(" in glance
    assert 'api("/alerts/firings?limit=50")' in glance
    assert 'api("/topicx/emerging?limit=6")' in glance


def test_glance_chip_deep_links_into_analytics_lifecycle() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    glance = _glance_slice(app_js)
    # Chips are rendered through the shared renderChips helper.
    assert "renderChips(chips," in glance
    assert 'key: (x) => x.term' in glance
    # Picking a chip fills the analytics term input, navigates to the
    # Analytics view, and triggers the lifecycle query (analytics-band pattern).
    assert '$("#an-term-input").value = x.term' in glance
    assert 'navigate("analytics")' in glance
    assert "void loadLifecycle();" in glance


def test_glance_refresh_hooked_into_dashboard_cadence() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    glance = _glance_slice(app_js)
    # Same 12-tick rebuild guard as the quality/saved bands (60 s at 5 s cadence).
    assert "dashGlanceTick % 12 !== 0" in glance
    assert "if (sig === dashGlanceSig && dashGlanceTick % 12 !== 0) return;" in glance
    # refreshDashboard drives it, non-fatal on failure.
    assert "void refreshDashGlance();" in app_js
    assert "// Today at a glance band (same cadence guard)." in app_js


def test_glance_region_no_innerhtml() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    glance = _glance_slice(app_js)
    assert "innerHTML" not in glance
    assert "el(" in glance


def test_glance_styles_present() -> None:
    css = STYLE_CSS.read_text(encoding="utf-8")
    for needle in (
        ".dash-glance-band",
        ".dash-glance-alerts",
        ".dash-glance-kpi",
        ".dash-glance-kpi-value",
    ):
        assert needle in css


# ── pure alert activity summarizer (executed in node against the real fn) ──

def test_alert_activity_counts_24h_and_top_rule() -> None:
    out = _run_alert_activity(
        [
            {"fired_at": _hours_ago_iso(1), "rule_name": "btc volume"},
            {"fired_at": _hours_ago_iso(2), "rule_name": "btc volume"},
            {"fired_at": _hours_ago_iso(3), "rule_name": "eth spike"},
            # Outside the 24 h window — must be excluded.
            {"fired_at": _hours_ago_iso(30), "rule_name": "stale rule"},
        ]
    )
    assert out["firings24h"] == 3
    assert out["topRule"] == "btc volume"
    assert out["topCount"] == 2


def test_alert_activity_handles_empty_and_bad_rows() -> None:
    empty = _run_alert_activity([])
    assert empty == {"firings24h": 0, "topRule": None, "topCount": 0}
    junk = _run_alert_activity(
        [{"fired_at": None}, {"fired_at": "not-a-date"}, {}, {"fired_at": _hours_ago_iso(40)}]
    )
    assert junk == {"firings24h": 0, "topRule": None, "topCount": 0}
