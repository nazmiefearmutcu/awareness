"""End-to-end smoke harness for the awareness pipeline.

Runs the full stack against a throwaway project root using only the
LocalFixtureAdapter (no network):

    1. init       — CLI ``init`` via Typer CliRunner → data tree exists
    2. ingest     — Planner.submit_backfill + WorkerEngine.run_job to completion
    3. query      — DuckDbIndex health_snapshot / search (hit + miss terms)
    4. analytics  — TermFrequencyEngine / detect_spikes / SentimentEngine
    5. api        — TestClient(create_app()): healthz / captures / search /
                    analytics/top-terms / corpus/quality / alerts/rules
    6. alerts     — create term_count rule → POST /alerts/check → firings
    7. digest     — generate_digest + render_digest_markdown
    8. export     — export_llm_dataset (dedupe fold keeps one row per doc)

Usage:
    AW_PROJECT_ROOT=/tmp/aware-root .venv/bin/python scripts/e2e_smoke.py
    .venv/bin/python scripts/e2e_smoke.py            # uses a temp dir

Exits non-zero with a clear message on the first failing stage. When
``AW_PROJECT_ROOT`` is not set, a ``tempfile.mkdtemp`` root is created and
removed at the end. The stage functions are importable so the pytest wrapper
(``tests/smoke/test_e2e_full_flow.py``) runs the exact same flow in-process.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from awareness.analytics.engine import TermFrequencyEngine
from awareness.api import server
from awareness.cli.main import app as cli_app
from awareness.config import get_settings, reset_settings
from awareness.consume.digest import generate_digest, render_digest_markdown
from awareness.consume.llm_export import export_llm_dataset
from awareness.planner.planner import Planner
from awareness.schemas.doc import SourceKind
from awareness.schemas.jobs import BackfillRequest
from awareness.sentiment.engine import SentimentEngine
from awareness.sources import get_adapter_registry
from awareness.sources.local_fixture import LocalFixtureAdapter
from awareness.storage.duckdb_index import DuckDbIndex
from awareness.storage.state import StateDB
from awareness.workers.engine import WorkerEngine

# ────────────────────────────────────────────────────────────────────────
# Fixture corpus. Bodies are deliberately DISTINCT per doc: the dedup engine
# clusters near-duplicates by simhash128 (default threshold 32/128), so docs
# that share boilerplate collapse into one dup group and downstream
# assertions (search rows, export count) would see fewer rows than emitted.
# ────────────────────────────────────────────────────────────────────────

CORPUS_TERM = "climate"
ABSENT_TERM = "zzzqzx"

FIXTURE_DOCS: list[dict[str, Any]] = [
    {
        "id": 1,
        "url": "https://x.example/climate-policy",
        "title": "Climate policy shifts",
        "text": (
            "Global climate negotiations advanced today as delegations from forty nations "
            "debated emissions targets for the coming decade. Ministers outlined plans to "
            "phase out coal generation and invest heavily in wind and solar farms. Analysts "
            "said the proposed timelines could reshape energy markets worldwide and force "
            "utilities to accelerate their transition away from fossil fuels."
        ),
        "language": "en",
    },
    {
        "id": 2,
        "url": "https://x.example/football-transfer",
        "title": "Football transfer news",
        "text": (
            "The football season kicked off with surprises as the leading club finalized a "
            "record transfer for a young striker. The deal ended weeks of speculation and "
            "reshaped the league title odds overnight. Coaches praised the new arrival pace "
            "and finishing, while rival clubs scrambled to reinforce their defenses before "
            "the window closed."
        ),
        "language": "en",
    },
    {
        "id": 3,
        "url": "https://x.example/carbon-levels",
        "title": "Carbon and the climate",
        "text": (
            "Researchers measured atmospheric carbon levels at twenty remote stations and "
            "found concentrations climbing faster than prior projections. The fieldwork "
            "combined satellite readings with ground-based sampling across the tropics. "
            "Scientists warned that the trend, if unchanged, would overwhelm efforts to "
            "hold warming below the limits agreed in previous accords."
        ),
        "language": "en",
    },
    {
        "id": 4,
        "url": "https://x.example/pasta-recipe",
        "title": "A new pasta recipe",
        "text": (
            "This recipe pairs basil with tomatoes beautifully, yielding a bright sauce that "
            "clings to al dente pasta. The chef recommends crushing the garlic gently and "
            "simmering the mixture over low heat to deepen the flavor. Served with shaved "
            "parmesan and olive oil, the dish is ready in under half an hour."
        ),
        "language": "en",
    },
    {
        "id": 5,
        "url": "https://x.example/climate-summit",
        "title": "Climate summit wrap-up",
        "text": (
            "Delegates signed the new climate accord overnight after marathon sessions that "
            "stretched past midnight. The agreement pledges financial support for vulnerable "
            "regions and establishes a review process every two years. Observers noted that "
            "enforcement remains voluntary, but the accord keeps momentum alive for stricter "
            "rules later this decade."
        ),
        "language": "en",
    },
    {
        "id": 6,
        "url": "https://x.example/quantum-chip",
        "title": "Quantum chip milestone",
        "text": (
            "Engineers cooled the quantum chip to millikelvin temperatures and sustained a "
            "record number of stable qubits for several minutes. The breakthrough relied on "
            "new error correction routines and a redesigned cryostat. Researchers caution "
            "that practical quantum computers remain years away, but the milestone validates "
            "the architecture."
        ),
        "language": "en",
    },
    {
        "id": 7,
        "url": "https://x.example/climate-funding",
        "title": "Climate funding round",
        "text": (
            "Investors poured money into climate startups this week, closing a record funding "
            "round for battery storage and grid software companies. Venture funds competed "
            "for stakes in firms developing long-duration storage and smart charging "
            "platforms. Industry analysts say the surge reflects growing confidence that "
            "clean energy markets will expand rapidly over the next five years."
        ),
        "language": "en",
    },
    {
        "id": 8,
        "url": "https://x.example/rocket-launch",
        "title": "Rocket launch window",
        "text": (
            "The rocket slipped its launch window to Friday after weather forced a two-day "
            "postponement at the coastal range. Engineers completed the final propellant "
            "loading tests and cleared the vehicle for liftoff. The mission will deploy "
            "three observation satellites into low orbit and test a reusable upper stage "
            "for the first time."
        ),
        "language": "en",
    },
]

# fetch_ts spread backwards from now so every stage sees the corpus inside
# its rolling window: alerts (window_hours), digest (7d), sentiment (14d).
_now = datetime.now(UTC)
for _i, _doc in enumerate(FIXTURE_DOCS):
    _doc["fetch_ts"] = (_now - timedelta(hours=6 * _i)).isoformat()

# Environment leaks from `tail start` / `start` that must not leak across
# runs (mirrors tests/conftest.py's tmp_project fixture).
_ENV_LEAK_KEYS = (
    "AW_ENABLE_ICEBERG",
    "AW_ENABLE_JSONL_STAGING",
    "AW_ENABLE_GDRIVE",
    "AW_ICEBERG_WAREHOUSE",
    "AW_DATA_DIR",
)


class SmokeError(RuntimeError):
    """Raised on the first failed assertion of a stage."""


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise SmokeError(msg)


def configure_env(project_root: Path) -> None:
    """Point the Settings singleton at *project_root* (env + cache reset)."""
    os.environ["AW_PROJECT_ROOT"] = str(project_root)
    os.environ.pop("AW_CONFIG_FILE", None)
    for leak in _ENV_LEAK_KEYS:
        os.environ.pop(leak, None)
    os.environ["AW_LOG_JSON"] = "false"
    os.environ["AW_LOG_LEVEL"] = "WARNING"
    reset_settings()


def _get_settings():
    return get_settings()


def _state_db(root: Path):
    settings = _get_settings()
    return StateDB(settings.state_db_url or f"sqlite:///{root / 'state.db'}")


# ── 1. init ─────────────────────────────────────────────────────────────


def stage_init(root: Path) -> dict[str, Any]:
    """Run ``awareness init`` via CliRunner; assert exit 0 + data tree."""
    result = CliRunner().invoke(cli_app, ["init", "--no-interactive"])
    if result.exit_code != 0:
        raise SmokeError(f"init exited {result.exit_code}: {result.output[-500:]}")
    settings = _get_settings()
    _check(settings.data_dir is not None, "settings.data_dir is None")
    state_db = settings.data_dir / "state" / "awareness.sqlite"
    _check(state_db.exists(), f"state db not created: {state_db}")
    _check(settings.staging_jsonl_dir().is_dir(), f"jsonl dir not created: {settings.staging_jsonl_dir()}")
    _check(
        (settings.data_dir / "duckdb").is_dir(),
        f"duckdb dir not created: {settings.data_dir / 'duckdb'}",
    )
    return {"state_db": state_db, "data_dir": settings.data_dir}


# ── 2. ingest ───────────────────────────────────────────────────────────


def stage_ingest(root: Path) -> dict[str, Any]:
    """Backfill via LocalFixtureAdapter; run the worker to completion."""
    state = _state_db(root)
    state.init()
    planner = Planner(state)
    get_adapter_registry().register(LocalFixtureAdapter(rows=list(FIXTURE_DOCS)))

    request = BackfillRequest(
        start=datetime(2024, 6, 1, tzinfo=UTC),
        end=datetime(2024, 6, 2, tzinfo=UTC),
        sources=[SourceKind.LOCAL_FIXTURE],
        max_tasks=10,
    )
    job_id = planner.submit_backfill(request)

    engine = WorkerEngine(state, planner, concurrency=2)
    try:
        asyncio.run(engine.run_job(job_id, poll_seconds=0.05))
    finally:
        asyncio.run(engine.aclose())

    status = planner.status(job_id)
    _check(status["status"] == "completed", f"job not completed: {status.get('status')}")
    emitted = int(status["docs_emitted"])
    _check(emitted > 0, "docs_emitted == 0")
    _check(emitted == len(FIXTURE_DOCS), f"docs_emitted {emitted} != {len(FIXTURE_DOCS)} fixture docs")
    counts = status.get("task_status_counts") or {}
    _check(int(counts.get("completed", 0)) > 0, "no tasks COMPLETED in state db")

    settings = _get_settings()
    chunks = sorted(settings.staging_jsonl_dir().rglob("*.jsonl*"))
    _check(len(chunks) > 0, "no JSONL chunk files written")
    return {"job_id": job_id, "docs_emitted": emitted, "chunks": [str(c) for c in chunks]}


# ── 3. query ────────────────────────────────────────────────────────────


def _open_index():
    settings = _get_settings()
    return DuckDbIndex(
        db_path=settings.duckdb_path(),
        jsonl_dir=settings.staging_jsonl_dir(),
        iceberg_warehouse=settings.iceberg_warehouse,
    )


def stage_query(root: Path, expected_captures: int) -> dict[str, Any]:
    """DuckDbIndex health + search (hit term ≥ 1 row, absent term 0 rows)."""
    idx = _open_index()
    snap = idx.health_snapshot()
    _check(bool(snap.get("ready")), f"index not ready: {snap}")
    captures = int(snap.get("captures", -1))
    _check(captures == expected_captures, f"captures {captures} != emitted {expected_captures}")

    hit = idx.search(CORPUS_TERM, limit=10)
    _check(int(hit["total"]) >= 1, f"search({CORPUS_TERM!r}) returned 0 rows")
    miss = idx.search(ABSENT_TERM, limit=10)
    _check(int(miss["total"]) == 0, f"search({ABSENT_TERM!r}) unexpectedly matched")

    settings = _get_settings()
    _check(settings.duckdb_path().exists(), f"duckdb file missing: {settings.duckdb_path()}")
    return {"captures": captures, "hit_total": int(hit["total"]), "miss_total": int(miss["total"])}


# ── 4. analytics ────────────────────────────────────────────────────────


def stage_analytics(root: Path) -> dict[str, Any]:
    """Term frequency series, spike detection, sentiment series."""
    idx = _open_index()
    tfe = TermFrequencyEngine(idx)
    tf = tfe.term_frequency_over_time(CORPUS_TERM)
    _check(len(tf) > 0, f"term_frequency_over_time({CORPUS_TERM!r}) empty")

    spikes = tfe.detect_spikes(CORPUS_TERM)
    _check(isinstance(spikes, list), "detect_spikes did not return a list")

    se = SentimentEngine(idx)
    senti = se.term_sentiment_over_time(CORPUS_TERM)
    _check(len(senti) > 0, f"term_sentiment_over_time({CORPUS_TERM!r}) empty")
    _check(sum(b.doc_count for b in senti) > 0, "sentiment series has zero matching docs")

    return {"tf_buckets": len(tf), "spikes": len(spikes), "sentiment_buckets": len(senti)}


# ── 5. api ──────────────────────────────────────────────────────────────


def _build_app():
    server._State.index = None
    server._close_index()
    return server.create_app()


def stage_api(root: Path, expected_captures: int) -> dict[str, Any]:
    """FastAPI TestClient surface: healthz / captures / search / analytics /
    corpus quality / alerts rules."""
    app = _build_app()
    with TestClient(app) as client:
        r = client.get("/healthz")
        _check(r.status_code == 200, f"/healthz -> {r.status_code}")
        body = r.json()
        _check(body.get("ok") is True, "/healthz ok != true")
        _check(body.get("index_ready") is True, "/healthz index_ready != true")

        r = client.get("/captures")
        _check(r.status_code == 200, f"/captures -> {r.status_code}")
        caps = r.json()
        _check(int(caps["total"]) > 0, "/captures total == 0")
        _check(len(caps["rows"]) > 0, "/captures rows empty")

        r = client.get("/search", params={"q": CORPUS_TERM})
        _check(r.status_code == 200, f"/search -> {r.status_code}")
        _check(int(r.json()["total"]) >= 1, f"/search?q={CORPUS_TERM} total == 0")

        r = client.get("/analytics/top-terms")
        _check(r.status_code == 200, f"/analytics/top-terms -> {r.status_code}")

        r = client.get("/corpus/quality")
        _check(r.status_code == 200, f"/corpus/quality -> {r.status_code}")
        _check(int(r.json().get("total_captures", 0)) > 0, "/corpus/quality total_captures == 0")

        r = client.get("/alerts/rules")
        _check(r.status_code == 200, f"/alerts/rules -> {r.status_code}")
    return {"healthz_ok": True, "captures_total": int(caps["total"])}


# ── 6. alerts ───────────────────────────────────────────────────────────


def stage_alerts(root: Path) -> dict[str, Any]:
    """Create a term_count rule, run /alerts/check, read firings back."""
    app = _build_app()
    with TestClient(app) as client:
        payload = {
            "name": "smoke climate watch",
            "kind": "term_count",
            "term": CORPUS_TERM,
            "threshold": 1,
            "window_hours": 72,
            "cooldown_minutes": 0,
            "active": True,
        }
        r = client.post("/alerts/rules", json=payload)
        _check(r.status_code == 201, f"POST /alerts/rules -> {r.status_code}: {r.text[:300]}")
        rule = r.json()
        _check(rule.get("id"), "created rule missing id")

        r = client.post("/alerts/check")
        _check(r.status_code == 200, f"POST /alerts/check -> {r.status_code}")
        firings = r.json().get("firings") or []
        _check(len(firings) >= 1, f"alerts check produced no firings (corpus term {CORPUS_TERM!r})")

        r = client.get("/alerts/firings")
        _check(r.status_code == 200, f"GET /alerts/firings -> {r.status_code}")
        rows = r.json()
        _check(len(rows) >= 1, "GET /alerts/firings returned 0 rows")
    return {"rule_id": rule["id"], "firings": len(firings), "firing_rows": len(rows)}


# ── 7. digest ───────────────────────────────────────────────────────────


def stage_digest(root: Path) -> dict[str, Any]:
    """generate_digest totals > 0 and the markdown mentions the corpus term."""
    idx = _open_index()
    digest = generate_digest(idx, days=7)
    _check(int(digest.total_captures) > 0, "digest total_captures == 0")
    md = render_digest_markdown(digest)
    _check(CORPUS_TERM in md.lower(), f"digest markdown missing corpus term {CORPUS_TERM!r}")
    return {"total_captures": int(digest.total_captures), "markdown_len": len(md)}


# ── 8. export ───────────────────────────────────────────────────────────


def stage_export(root: Path) -> dict[str, Any]:
    """export_llm_dataset writes exactly min(limit, total) rows to disk."""
    idx = _open_index()
    out_dir = root / "export"
    result = export_llm_dataset(idx, out_dir, limit=100)
    total = int(idx.health_snapshot().get("captures", 0))
    expected = min(100, total)
    _check(result.count == expected, f"export rows {result.count} != min(100, total)={expected}")
    files = [Path(f) for f in result.files]
    _check(
        len(files) > 0 and all(f.exists() and f.stat().st_size > 0 for f in files),
        "export files missing/empty",
    )
    return {"count": result.count, "total": total, "files": [f.name for f in files]}


# ── orchestrator ────────────────────────────────────────────────────────


def run_e2e_flow(project_root: Path) -> dict[str, Any]:
    """Run every stage in order; raise SmokeError with the stage name on
    the first failure. Returns per-stage results keyed by stage name."""
    configure_env(project_root)
    results: dict[str, Any] = {}
    results["init"] = stage_init(project_root)
    results["ingest"] = stage_ingest(project_root)
    emitted = results["ingest"]["docs_emitted"]
    results["query"] = stage_query(project_root, emitted)
    results["analytics"] = stage_analytics(project_root)
    results["api"] = stage_api(project_root, emitted)
    results["alerts"] = stage_alerts(project_root)
    results["digest"] = stage_digest(project_root)
    results["export"] = stage_export(project_root)
    return results


def main() -> int:
    """CLI entry point for ``python scripts/e2e_smoke.py``."""
    own_root = False
    root_env = os.environ.get("AW_PROJECT_ROOT")
    if root_env:
        root = Path(root_env).resolve()
        root.mkdir(parents=True, exist_ok=True)
    else:
        root = Path(tempfile.mkdtemp(prefix="aware-e2e-"))
        own_root = True
    try:
        results = run_e2e_flow(root)
    except SmokeError as exc:
        print(f"[E2E SMOKE FAIL] {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # unexpected failure — still fail loudly
        print(f"[E2E SMOKE ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    else:
        print("[E2E SMOKE PASS]")
        for name, res in results.items():
            print(f"  {name:<10} {res}")
        return 0
    finally:
        if own_root:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
