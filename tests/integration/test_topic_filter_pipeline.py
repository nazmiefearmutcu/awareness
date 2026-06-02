"""Integration test: the ingest-time topic filter actually drops non-matching
documents in the worker pipeline (proves --match is wired end to end)."""

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

_BODY = "This is a sufficiently long article body so it survives normalisation and language detection. " * 4

_DOCS = [
    {"id": 1, "url": "https://x.example/climate-1", "title": "Climate policy shifts",
     "text": "Global climate negotiations advanced today. " + _BODY, "fetch_ts": "2024-06-01T10:00:00+00:00", "language": "en"},
    {"id": 2, "url": "https://x.example/football-1", "title": "Football transfer news",
     "text": "The football season kicked off with surprises. " + _BODY, "fetch_ts": "2024-06-01T10:05:00+00:00", "language": "en"},
    {"id": 3, "url": "https://x.example/climate-2", "title": "Carbon and the climate",
     "text": "Researchers measured atmospheric climate carbon levels. " + _BODY, "fetch_ts": "2024-06-01T10:10:00+00:00", "language": "en"},
    {"id": 4, "url": "https://x.example/cooking-1", "title": "A new pasta recipe",
     "text": "This recipe pairs basil with tomatoes beautifully. " + _BODY, "fetch_ts": "2024-06-01T10:15:00+00:00", "language": "en"},
]


@pytest.mark.asyncio
async def test_match_filter_drops_non_matching(tmp_project: Path) -> None:
    settings = get_settings()
    state = StateDB(settings.state_db_url or f"sqlite:///{tmp_project / 'state.db'}")
    state.init()
    planner = Planner(state)
    get_adapter_registry().register(LocalFixtureAdapter(rows=_DOCS))

    req = BackfillRequest(
        start=datetime(2024, 6, 1, tzinfo=UTC),
        end=datetime(2024, 6, 2, tzinfo=UTC),
        sources=[SourceKind.LOCAL_FIXTURE],
        max_tasks=10,
        match=["climate"],
    )
    job_id = planner.submit_backfill(req)

    engine = WorkerEngine(state, planner, concurrency=2)
    try:
        await engine.run_job(job_id, poll_seconds=0.05)
    finally:
        await engine.aclose()

    # Only the two climate docs survive; football + cooking are filtered out.
    assert engine._total_docs_filtered == 2
    status = planner.status(job_id)
    assert status["docs_emitted"] == 2

    idx = DuckDbIndex(
        db_path=settings.duckdb_path(),
        jsonl_dir=settings.staging_jsonl_dir(),
        iceberg_warehouse=settings.iceberg_warehouse,
    )
    rows = idx.execute("SELECT title FROM captures WHERE source_type = 'local_fixture'")
    assert len(rows) == 2
    assert all("climate" in (r["title"] or "").lower() or "carbon" in (r["title"] or "").lower() for r in rows)
