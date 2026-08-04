"""CLI ↔ SPA feed-health score parity (W16 audit note).

The dashboard's ``summarizeFeedHealth`` rounds the health score with
``Math.round`` (half-up). The CLI's ``summarize_feed_health`` used Python's
``round()`` (half-even), which diverged at exact halves — a 62.5 raw score
rendered 62 in the CLI but 63 in the SPA. The CLI now rounds half-up
(``int(math.floor(x + 0.5))``) so both sides agree.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from awareness.cli.main import app, summarize_feed_health

APP_JS = Path("src/awareness/api/web/app.js")
NODE = shutil.which("node")
runner = CliRunner()


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


def _spa_score(snapshot: dict) -> int | None:
    """Run the real SPA summarizer (Math.round) from app.js in node; return score."""
    if NODE is None:
        pytest.skip("node not available")
    app_js = APP_JS.read_text(encoding="utf-8")
    fn = _extract_function("summarizeFeedHealth", app_js)
    harness = (
        "const snap = " + json.dumps(snapshot) + ";\n"
        + fn + "\n"
        + "console.log(JSON.stringify(summarizeFeedHealth(snap).score));\n"
    )
    proc = subprocess.run(  # noqa: S603, PLW1510 — fixed node binary, static harness
        [NODE, "-e", harness], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _snapshot(error_pct: float, non200_pct: float) -> dict:
    """100 attempts: error_pct errors (non-ok outcome), non200_pct non-200s.

    Raw score = 100 - 10*error_pct - 5*non200_pct (clamped 0..100).
    """
    return {
        "counters": [
            {"name": "feeds.fetch_attempts", "labels": {"outcome": "ok"}, "value": 100.0 - error_pct},
            {"name": "feeds.fetch_attempts", "labels": {"outcome": "http_error"}, "value": float(error_pct)},
            {"name": "feeds.fetch_non_200", "labels": {}, "value": float(non200_pct)},
        ],
        "histograms": [],
    }


@pytest.mark.parametrize(
    ("error_pct", "non200_pct", "expected"),
    [
        (2.5, 2.5, 63),  # raw 62.5: half-even round() would give 62 — the W16 gap
        (0.0, 0.0, 100),  # clean run
        (10.0, 0.0, 0),  # error-only clamp floor
        (6.25, 0.05, 37),  # raw 37.25
        (0.0, 20.0, 0),  # non-200 clamp floor
        (3.0, 0.4, 68),  # raw 68.0
    ],
)
def test_feed_health_score_parity_cli_vs_spa(
    error_pct: float, non200_pct: float, expected: int
) -> None:
    snap = _snapshot(error_pct, non200_pct)
    assert summarize_feed_health(snap)["score"] == expected
    assert _spa_score(snap) == expected


def test_feeds_json_score_62_5_is_63(monkeypatch: pytest.MonkeyPatch) -> None:
    """W16 regression: `awareness feeds --json` reports 63 for a 62.5 score."""

    class _FakeMetrics:
        def __init__(self, snap: dict) -> None:
            self._snap = snap

        def snapshot(self, **kwargs: object) -> dict:
            return self._snap

    monkeypatch.setattr(
        "awareness.cli.main.get_metrics",
        lambda: _FakeMetrics(_snapshot(2.5, 2.5)),
    )
    result = runner.invoke(app, ["feeds", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["score"] == 63
