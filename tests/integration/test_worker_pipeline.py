"""End-to-end pipeline test using the local fixture adapter.

Exercises: planner → worker → adapter → dedup → JSONL staging → DuckDB query.
This is the test that proves the whole framework runs without external deps.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from awareness.config import get_settings
from awareness.planner.planner import Planner
from awareness.schemas.doc import SourceKind
from awareness.schemas.jobs import BackfillRequest
from awareness.sources import get_adapter_registry
from awareness.sources.local_fixture import LocalFixtureAdapter
from awareness.storage.duckdb_index import DuckDbIndex
from awareness.storage.state import StateDB
from awareness.workers.engine import WorkerEngine

pytestmark = pytest.mark.integration


_DOCS = [
    {
        "id": 1,
        "url": "https://news.example/a-very-long-article",
        "title": "An article about a real topic",
        "text": (
            "This is a thoroughly fictitious article that nonetheless exceeds the "
            "minimum character requirement to qualify as a real document in the "
            "awareness corpus. It contains enough text to be language-detected and "
            "hashed. " * 6
        ),
        "fetch_ts": "2024-06-01T10:00:00+00:00",
        "language": "en",
    },
    {
        "id": 2,
        "url": "https://news.example/another-long-article",
        "title": "Another piece on something else",
        "text": (
            "Another fully formed article about a distinct topic, with quite different "
            "vocabulary covering interesting subjects in detail to ensure it isn't a "
            "near-duplicate of the previous record. " * 6
        ),
        "fetch_ts": "2024-06-01T11:00:00+00:00",
        "language": "en",
    },
    {
        "id": 3,
        # Same text as id=1 → must be deduplicated.
        "url": "https://mirror.example/a-very-long-article",
        "title": "An article about a real topic",
        "text": (
            "This is a thoroughly fictitious article that nonetheless exceeds the "
            "minimum character requirement to qualify as a real document in the "
            "awareness corpus. It contains enough text to be language-detected and "
            "hashed. " * 6
        ),
        "fetch_ts": "2024-06-01T12:00:00+00:00",
        "language": "en",
    },
    {
        "id": 4,
        # Re-capture of id=1 same URL & content.
        "url": "https://news.example/a-very-long-article",
        "title": "An article about a real topic",
        "text": (
            "This is a thoroughly fictitious article that nonetheless exceeds the "
            "minimum character requirement to qualify as a real document in the "
            "awareness corpus. It contains enough text to be language-detected and "
            "hashed. " * 6
        ),
        "fetch_ts": "2024-06-02T08:00:00+00:00",
        "language": "en",
    },
]


def _install_fixture_adapter() -> LocalFixtureAdapter:
    reg = get_adapter_registry()
    fixture = LocalFixtureAdapter(rows=_DOCS)
    reg.register(fixture)
    return fixture


@pytest.mark.asyncio
async def test_pipeline_runs_dedup_and_writes_storage(tmp_project: Path) -> None:
    settings = get_settings()
    state = StateDB(settings.state_db_url or f"sqlite:///{tmp_project / 'state.db'}")
    state.init()
    planner = Planner(state)
    fixture = _install_fixture_adapter()

    req = BackfillRequest(
        start=datetime(2024, 6, 1, tzinfo=UTC),
        end=datetime(2024, 6, 2, tzinfo=UTC),
        sources=[SourceKind.LOCAL_FIXTURE],
        max_tasks=10,
    )
    job_id = planner.submit_backfill(req)

    engine = WorkerEngine(state, planner, concurrency=2)
    try:
        await engine.run_job(job_id, poll_seconds=0.05)
    finally:
        await engine.aclose()

    assert engine._total_docs_processed >= len(_DOCS)
    assert engine._total_bytes_processed > 0

    status = planner.status(job_id)
    # EXACT_DUP and REVISION are not stored; only NEW docs count as emitted.
    # Fixture: id=1 NEW, id=2 NEW, id=3 EXACT_DUP, id=4 REVISION → 2 emitted.
    assert status["docs_emitted"] >= 2
    # Dedup should flag at least the two duplicates (id=3 EXACT_DUP, id=4 REVISION).
    assert status["docs_dedup_dropped"] >= 2

    # Read back via DuckDB. EXACT_DUP + REVISION rows are skipped at storage time.
    idx = DuckDbIndex(
        db_path=settings.duckdb_path(),
        jsonl_dir=settings.staging_jsonl_dir(),
        iceberg_warehouse=settings.iceberg_warehouse,
    )
    rows = idx.execute("SELECT count(*) AS n FROM captures WHERE source_type = $st", {"st": "local_fixture"})
    assert rows[0]["n"] >= 2  # id=1 NEW, id=2 NEW
    assert rows[0]["n"] < len(_DOCS) + 5  # sanity: not exploding

    # Range filter works (day-1 window: id=1 and id=2; id=3 EXACT_DUP not stored).
    rng = idx.execute(
        "SELECT count(*) AS n FROM captures WHERE fetch_ts BETWEEN $a AND $b AND source_type = $st",
        {
            "a": datetime(2024, 6, 1, tzinfo=UTC),
            "b": datetime(2024, 6, 1, 23, 59, 59, tzinfo=UTC),
            "st": "local_fixture",
        },
    )
    assert rng[0]["n"] >= 2  # docs 1, 2 fall in this window; 3 skipped as EXACT_DUP
