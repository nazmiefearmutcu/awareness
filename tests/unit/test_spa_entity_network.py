"""Static + pure-logic checks: SPA Entity network band (ring layout)."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
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


def _network_slice(app_js: str) -> str:
    start = app_js.index("// ── Entity network")
    end = app_js.index("// ── Alerts", start)
    return app_js[start:end]


def _run_layout(count: int, radius: float) -> list[dict]:
    """Execute the real ring-layout function from app.js in node."""
    if NODE is None:
        pytest.skip("node not available")
    app_js = APP_JS.read_text(encoding="utf-8")
    fn = _extract_function("entityNetworkLayout", app_js)
    harness = (
        fn + "\n"
        + f"console.log(JSON.stringify(entityNetworkLayout({count}, {radius})));\n"
    )
    proc = subprocess.run(
        [NODE, "-e", harness], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_spa_entity_network_band_in_html() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="an-entity-network"' in html
    assert 'id="an-entity-input"' in html
    assert 'id="an-entity-build"' in html
    assert "Entity network" in html
    # Sits right after the Top entities band, before Source intelligence.
    assert html.index('id="an-entities"') < html.index('id="an-entity-network"')
    assert html.index('id="an-entity-network"') < html.index('id="an-domains-table"')


def test_spa_entity_network_js_present() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    net = _network_slice(app_js)
    assert "function entityNetworkLayout(" in net
    assert "async function buildEntityNetwork(" in net
    # Fetches the entities co-occurrence endpoint (limit 12) with the root entity.
    assert 'api(`/entities/co-occurring?entity=${encodeURIComponent(name)}&limit=12`)' in net
    # Concentric layout: root chip + ring around it via absolute positioning.
    assert "an-root-node" in net
    assert "2 * Math.PI * i" in net
    # Node size scales with the co-occurrence count.
    assert "node.count / maxCount" in net
    # Empty results degrade to the placeholder dash.
    assert 'container.textContent = "—"' in net
    # Clicking a node re-builds with that node as the root.
    assert "buildEntityNetwork(node.entity)" in net
    assert "buildEntityNetwork(name)" in net


def test_spa_entity_network_wired_in_init() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert 'netBtn.addEventListener("click", () => void buildEntityNetwork' in app_js
    assert '$("#an-entity-input").addEventListener("keydown"' in app_js
    # Default root = first top entity, and it builds once on load.
    assert "const firstEntity = entities && entities.length ? entities[0].text : \"\"" in app_js
    assert 'void buildEntityNetwork(firstEntity)' in app_js


def test_spa_entity_network_no_innerhtml() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    net = _network_slice(app_js)
    assert "innerHTML" not in net
    # Everything is built with the el() DOM helper / textContent.
    assert "el(" in net
    assert ".textContent" in net


def test_spa_entity_network_styles_present() -> None:
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".an-network" in css
    assert ".an-node" in css
    assert ".an-root-node" in css
    assert ".an-edge" in css


def test_entity_network_layout_ring_geometry() -> None:
    points = _run_layout(8, 100)
    assert len(points) == 8
    for i, p in enumerate(points):
        # Every point sits on the circle of the given radius (≈ 100).
        assert p["x"] ** 2 + p["y"] ** 2 == pytest.approx(100 ** 2, abs=1e-6)
        # Angles are evenly spaced: 2πi/n, in [0, 2π).
        expected = (2 * math.pi * i) / 8
        assert p["angle"] == pytest.approx(expected, abs=1e-9)
        assert 0 <= p["angle"] < 2 * math.pi
        # Coordinates match the angle (x = r·cos, y = r·sin).
        assert p["x"] == pytest.approx(100 * math.cos(p["angle"]), abs=1e-9)
        assert p["y"] == pytest.approx(100 * math.sin(p["angle"]), abs=1e-9)


def test_entity_network_layout_single_point() -> None:
    points = _run_layout(1, 50)
    assert len(points) == 1
    # A lone root has no ring companions — angle 0, still on the circle.
    assert points[0]["angle"] == 0
    assert points[0]["x"] == pytest.approx(50)
    assert points[0]["y"] == pytest.approx(0)


def test_entity_network_layout_radius_scales() -> None:
    small = _run_layout(12, 60)
    large = _run_layout(12, 200)
    for p, q in zip(small, large, strict=True):
        assert q["x"] ** 2 + q["y"] ** 2 == pytest.approx(200 ** 2, abs=1e-6)
        assert p["x"] ** 2 + p["y"] ** 2 == pytest.approx(60 ** 2, abs=1e-6)
