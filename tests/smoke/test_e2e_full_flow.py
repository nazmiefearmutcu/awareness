"""Pytest wrapper for the end-to-end smoke flow.

Runs the exact same stages as ``scripts/e2e_smoke.py`` in-process against a
fresh ``tmp_path`` project root, then asserts every stage's invariants with
pytest asserts. Marked ``smoke`` per pyproject.toml.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path

import pytest

from awareness.api import server
from awareness.config import reset_settings
from awareness.obs import logging as obs_logging

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "e2e_smoke.py"

pytestmark = pytest.mark.smoke


def _load_smoke_module():
    """Import scripts/e2e_smoke.py without relying on sys.path roots."""
    spec = importlib.util.spec_from_file_location("e2e_smoke", _SCRIPT)
    assert spec is not None and spec.loader is not None, f"cannot load {_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_extra_stages(results: dict) -> None:
    """Assertions for the stages added on top of the original eight (9-11)."""
    # ── 9. saved: create 201, listed, run total > 0, delete 204 ──────────
    saved = results["saved"]
    assert saved["saved_id"]
    assert saved["run_total"] > 0

    # ── 10. x: session created, 10 simulated tweets, analysis + tweets ───
    x = results["x"]
    assert x["session_id"]
    assert x["inserted"] == 10
    assert x["tweet_count"] == 10

    # ── 11. report: CLI JSON digest + quality, total_captures > 0 ────────
    report = results["report"]
    assert report["total_captures"] > 0


def _assert_new_stages(results: dict) -> None:
    """Assertions for the awareness stages added on top of the original eleven (12-14)."""
    # ── 12. topicx: lifecycle phase + non-empty counts, list endpoints ────
    topicx = results["topicx"]
    assert topicx["phase"] in {"EMERGING", "EXPANDING", "PEAKING", "DECLINING", "DORMANT"}
    assert topicx["counts"] >= 1
    assert isinstance(topicx["emerging"], int) and topicx["emerging"] >= 0
    assert isinstance(topicx["impact"], int) and topicx["impact"] >= 0

    # ── 13. qualityx: history series (first point > 0) + current snapshot ─
    qualityx = results["qualityx"]
    assert qualityx["history_points"] >= 1
    assert qualityx["first_total"] > 0
    assert qualityx["total_captures"] > 0

    # ── 14. briefing: saved file exists + parses, stdout parses as JSON ──
    briefing = results["briefing"]
    assert Path(briefing["saved_path"]).exists()
    assert isinstance(briefing["movers"], int) and briefing["movers"] >= 0
    assert isinstance(briefing["top_terms"], int) and briefing["top_terms"] >= 0


def test_e2e_full_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_smoke_module()
    saved_env = os.environ.copy()
    saved_logging = obs_logging._CONFIGURED
    # The harness must never depend on the live GDELT API.
    import awareness.gdeltx.engine as gdeltx_engine  # noqa: PLC0415

    monkeypatch.setattr(
        gdeltx_engine.GdeltBridge,
        "gdelt_query",
        lambda self, term, start, end, granularity="day": [],
    )
    try:
        results = module.run_e2e_flow(tmp_path)
    finally:
        # Restore the environment so other tests never see our overrides;
        # reset the settings cache and API index singleton as well.
        os.environ.clear()
        os.environ.update(saved_env)
        reset_settings()
        # _close_index() FIRST: it both closes the DuckDbIndex and clears
        # the singleton — nulling _State.index beforehand would make it a
        # no-op and leak an open connection in DuckDbIndex._instances.
        server._close_index()
        # Restore logging: the init stage's configure_logging() binds a
        # StreamHandler to Click's ephemeral capture stream, which is closed
        # after CliRunner.invoke — any later WARNING would print
        # "--- Logging error ---" garbage into the suite. Drop every root
        # handler and reconfigure from a clean slate with safe values.
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
        obs_logging._CONFIGURED = None
        if saved_logging is not None:
            obs_logging.configure_logging(
                level=saved_logging[0], json=saved_logging[1], log_dir=None
            )
        else:
            obs_logging.configure_logging(level="WARNING", json=False, log_dir=None)

    # ── 1. init: exit 0 (CliRunner inside stage), data tree exists ──────
    init = results["init"]
    assert init["state_db"].exists(), "state db missing after init"
    assert init["data_dir"].is_dir(), "data dir missing after init"

    # ── 2. ingest: job COMPLETED, docs emitted, JSONL chunks, tasks done ─
    ingest = results["ingest"]
    assert ingest["docs_emitted"] == len(module.FIXTURE_DOCS)
    assert ingest["docs_emitted"] > 0
    assert len(ingest["chunks"]) > 0

    # ── 3. query: index counts match, hit term ≥ 1, absent term 0 ────────
    query = results["query"]
    assert query["captures"] == ingest["docs_emitted"]
    assert query["hit_total"] >= 1
    assert query["miss_total"] == 0

    # ── 4. analytics: term frequency + sentiment non-empty, spikes safe ─
    analytics = results["analytics"]
    assert analytics["tf_buckets"] > 0
    assert analytics["sentiment_buckets"] > 0
    assert isinstance(analytics["spikes"], int) and analytics["spikes"] >= 0

    # ── 5. api: every endpoint 200 with the expected payload shapes ──────
    api = results["api"]
    assert api["healthz_ok"] is True
    assert api["captures_total"] > 0

    # ── 6. alerts: rule 201, check fires, firings list ≥ 1 ───────────────
    alerts = results["alerts"]
    assert alerts["rule_id"]
    assert alerts["firings"] >= 1
    assert alerts["firing_rows"] >= 1

    # ── 7. digest: totals > 0 and markdown carries the corpus term ───────
    digest = results["digest"]
    assert digest["total_captures"] > 0
    assert digest["markdown_len"] > 0

    # ── 8. export: rows == min(100, total), files on disk ────────────────
    export = results["export"]
    assert export["count"] == min(100, export["total"])
    assert len(export["files"]) > 0

    # ── 9-11. saved / x / report stages (see helper) ─────────────────────
    _assert_extra_stages(results)

    # ── 12-14. topicx / qualityx / briefing stages (see helper) ──────────
    _assert_new_stages(results)
