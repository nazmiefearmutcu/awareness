"""Pytest wrapper for the end-to-end smoke flow.

Runs the exact same stages as ``scripts/e2e_smoke.py`` in-process against a
fresh ``tmp_path`` project root, then asserts every stage's invariants with
pytest asserts. Marked ``smoke`` per pyproject.toml.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from awareness.api import server
from awareness.config import reset_settings

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "e2e_smoke.py"

pytestmark = pytest.mark.smoke


def _load_smoke_module():
    """Import scripts/e2e_smoke.py without relying on sys.path roots."""
    spec = importlib.util.spec_from_file_location("e2e_smoke", _SCRIPT)
    assert spec is not None and spec.loader is not None, f"cannot load {_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_e2e_full_flow(tmp_path: Path) -> None:
    module = _load_smoke_module()
    saved_env = os.environ.copy()
    try:
        results = module.run_e2e_flow(tmp_path)
    finally:
        # Restore the environment so other tests never see our overrides;
        # reset the settings cache and API index singleton as well.
        os.environ.clear()
        os.environ.update(saved_env)
        reset_settings()
        server._State.index = None
        server._close_index()

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
