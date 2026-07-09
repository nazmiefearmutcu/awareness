"""FastAPI HTTP control surface.

Endpoints:
    GET  /                       — web dashboard (static SPA)
    GET  /healthz                — liveness
    GET  /status                 — overall status
    GET  /metrics                — counters/histograms snapshot
    POST /backfill               — submit
    POST /backfill/{id}/run      — run pending tasks (non-blocking task)
    GET  /backfill/{id}          — status
    GET  /jobs                   — list jobs
    POST /tail/start             — start tail (background task)
    POST /tail/stop              — stop tail
    GET  /tail                   — tail state
    GET  /inspect                — date/domain/source range query
    GET  /captures               — paginated capture listing for UI
    GET  /captures/{capture_id}  — full capture (incl. text) for UI detail view
    GET  /captures/{id}/related  — sibling captures in the same dup_group
    GET  /search                 — BM25-ranked full-text search w/ snippets
    GET  /counts                 — counts grouped by source & domain
    GET  /dedup-stats            — dedup index stats

Run with ``awareness-api`` script or ``uvicorn awareness.api.server:create_app``.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from awareness.config import get_settings
from awareness.obs.logging import configure_logging, get_logger
from awareness.obs.metrics import get_metrics
from awareness.planner.planner import Planner
from awareness.schemas.doc import SourceKind
from awareness.schemas.jobs import BackfillRequest
from awareness.storage.duckdb_index import DuckDbIndex
from awareness.storage.state import StateDB
from awareness.tail.engine import TailEngine
from awareness.util.timeutil import coerce_relative_end, to_utc
from awareness.workers.engine import WorkerEngine

logger = get_logger("api")


class BackfillBody(BaseModel):
    start: datetime
    end: datetime | None = None
    end_str: str | None = None  # accept "now"
    sources: list[str] = []
    domains: list[str] | None = None
    languages: list[str] | None = None
    max_tasks: int | None = None
    notes: str | None = None
    match: list[str] = []
    match_all: bool = False
    match_regex: bool = False
    match_field: str = "both"


class TailStartBody(BaseModel):
    gdelt: bool | None = None  # None → fall back to config (tail_gdelt)
    gdelt_max_urls: int = 0  # 0 → config default
    match: list[str] = []
    match_all: bool = False
    match_regex: bool = False
    match_field: str = "both"


class _State:
    state: StateDB | None = None
    planner: Planner | None = None
    tail: TailEngine | None = None
    background_tasks: set[asyncio.Task[Any]] = set()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(level=settings.log_level, json=settings.log_json, log_dir=settings.log_dir)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        state = StateDB(settings.state_db_url or "sqlite:///awareness.sqlite")
        state.init()
        # Reconcile phantom "running" tail state from a previous process that
        # didn't clean up. Whatever was running in another Python process is
        # not running here. Mark it cancelled so the UI doesn't lie.
        # get_tail(reconcile=True) clears phantom running rows; then sweep
        # any TAIL jobs still stuck in RUNNING that aren't a live owner.
        state.get_tail(reconcile=True)
        try:
            state.reconcile_orphan_tail_jobs()
        except Exception as exc:  # noqa: BLE001
            logger.warning("orphan_tail_reconcile_failed", error=str(exc))
        _State.state = state
        _State.planner = Planner(state)
        _State.tail = TailEngine(state, _State.planner)

        reaper = None
        if settings.reaper_enabled:
            from awareness.workers.engine import DatabaseReaper
            reaper = DatabaseReaper(state)
            await reaper.start()

        try:
            yield
        finally:
            if reaper:
                await reaper.stop()
            if _State.tail and _State.tail.running:
                await _State.tail.stop(drain_seconds=10.0)
            for t in list(_State.background_tasks):
                t.cancel()

    app = FastAPI(title="Awareness", version="0.1.0", lifespan=lifespan)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        s = get_settings()
        return {
            "ok": True,
            "state_db": _State.state.url if _State.state else None,
            "data_dir": str(s.data_dir),
        }

    @app.get("/status")
    def status() -> dict[str, Any]:
        st = _State.state
        if st is None:
            raise HTTPException(500, "not initialized")
        jobs = [j.model_dump(mode="json") for j in st.list_jobs(limit=10)]
        return {"tail": st.get_tail(), "jobs": jobs}

    @app.get("/metrics")
    def metrics() -> dict[str, Any]:
        return get_metrics().snapshot()

    @app.get("/dedup-stats")
    def dedup_stats() -> dict[str, Any]:
        if _State.state is None:
            raise HTTPException(500, "not initialized")
        return _State.state.dedup_stats()

    @app.post("/backfill")
    def submit_backfill(body: BackfillBody) -> dict[str, Any]:
        if _State.planner is None:
            raise HTTPException(500, "not initialized")
        end = body.end or (coerce_relative_end(body.end_str or "now"))
        srcs = [SourceKind(s) for s in body.sources] if body.sources else []
        start_dt = to_utc(body.start)
        if start_dt is None:
            raise HTTPException(400, "Invalid start date")
        end_dt = to_utc(end)
        if end_dt is None:
            raise HTTPException(400, "Invalid end date")
        if body.match_field not in ("title", "text", "both"):
            raise HTTPException(400, "match_field must be one of: title, text, both")
        req = BackfillRequest(
            start=start_dt,
            end=end_dt,
            sources=srcs,
            domains=body.domains,
            languages=body.languages,
            max_tasks=body.max_tasks,
            notes=body.notes,
            match=body.match,
            match_all=body.match_all,
            match_regex=body.match_regex,
            match_field=body.match_field,
        )
        job_id = _State.planner.submit_backfill(req)
        return _State.planner.status(job_id)

    @app.post("/backfill/{job_id}/run")
    async def run_backfill(job_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
        if _State.state is None or _State.planner is None:
            raise HTTPException(500, "not initialized")
        engine = WorkerEngine(_State.state, _State.planner)

        async def _runner() -> None:
            try:
                await engine.run_job(job_id)
            finally:
                await engine.aclose()

        task = asyncio.create_task(_runner())
        _State.background_tasks.add(task)
        task.add_done_callback(_State.background_tasks.discard)
        return _State.planner.status(job_id)

    @app.get("/backfill/{job_id}")
    def backfill_status(job_id: str) -> dict[str, Any]:
        if _State.planner is None:
            raise HTTPException(500, "not initialized")
        return _State.planner.status(job_id)

    @app.get("/jobs")
    def list_jobs(limit: int = 20) -> list[dict[str, Any]]:
        if _State.state is None:
            raise HTTPException(500, "not initialized")
        return [j.model_dump(mode="json") for j in _State.state.list_jobs(limit=limit)]

    @app.post("/tail/start")
    async def tail_start(body: TailStartBody | None = None) -> dict[str, Any]:
        if _State.tail is None or _State.state is None:
            raise HTTPException(500, "not initialized")
        if _State.tail.running:
            return _State.state.get_tail()
        body = body or TailStartBody()
        if body.match_field not in ("title", "text", "both"):
            raise HTTPException(400, "match_field must be one of: title, text, both")
        if body.gdelt_max_urls < 0:
            raise HTTPException(400, "gdelt_max_urls must be >= 0")
        s = get_settings()
        use_gdelt = s.tail_gdelt if body.gdelt is None else body.gdelt
        await _State.tail.start(
            match_config={
                "match": body.match,
                "match_all": body.match_all,
                "match_regex": body.match_regex,
                "match_field": body.match_field,
            },
            gdelt=use_gdelt,
            gdelt_max_urls=body.gdelt_max_urls or s.tail_gdelt_max_urls,
        )
        return _State.state.get_tail()

    @app.post("/tail/stop")
    async def tail_stop() -> dict[str, Any]:
        if _State.tail is None or _State.state is None:
            raise HTTPException(500, "not initialized")
        await _State.tail.stop()
        return _State.state.get_tail()

    @app.get("/tail")
    def tail_get() -> dict[str, Any]:
        if _State.state is None:
            raise HTTPException(500, "not initialized")
        return _State.state.get_tail()

    @app.get("/tail/status")
    def tail_status() -> dict[str, Any]:
        """Rich tail status for the UI: counters + running tasks + recent
        chunks + per-seed progress + reseed cadence. Returns empty fields
        when no tail job has ever been started."""
        if _State.state is None or _State.planner is None:
            raise HTTPException(500, "not initialized")
        state = _State.state
        tail_info = state.get_tail()
        engine_info = _State.tail.info() if _State.tail else {}
        job_id = tail_info.get("job_id")
        settings = get_settings()
        base = {
            "tail": tail_info,
            "engine": engine_info,
            "tail_poll_seconds": settings.tail_poll_seconds,
        }
        if not job_id:
            return {
                **base,
                "job": None,
                "task_status_counts": {},
                "running_tasks": [],
                "recent_completed": [],
                "per_seed": {"feeds": [], "fetch": {}},
                "recent_chunks": [],
            }
        job = state.get_job(job_id)
        # When tail is not live, never present PENDING/RUNNING tasks as
        # "now fetching" — those rows are leftovers from a crashed process.
        live = bool(tail_info.get("running")) or bool(engine_info.get("in_process_running"))
        if not live:
            # Best-effort cleanup so subsequent reads stay quiet.
            try:
                state.abandon_inflight_tasks(job_id, note="stale-tasks-on-stopped-tail")
            except Exception as exc:  # noqa: BLE001
                logger.warning("abandon_inflight_on_tail_status_failed", error=str(exc))
        counts = state.task_status_counts(job_id)
        if not live:
            # UI queue panel should not show zombie in-flight buckets.
            for k in ("pending", "running"):
                counts.pop(k, None)
        return {
            **base,
            "job": job.model_dump(mode="json") if job else None,
            "task_status_counts": counts,
            "running_tasks": state.list_running_tasks(job_id, limit=12) if live else [],
            "recent_completed": state.list_recent_completed_tasks(job_id, limit=10),
            "per_seed": state.per_seed_progress(job_id) if live else {"feeds": [], "fetch": {}},
            "recent_chunks": state.list_recent_manifests(limit=8),
        }

    @app.get("/inspect")
    def inspect(
        start: datetime = Query(...),
        end: datetime | None = Query(None),
        limit: int = Query(20, ge=1, le=500),
        domain: str | None = Query(None),
        source: str | None = Query(None),
    ) -> list[dict[str, Any]]:
        s = get_settings()
        idx = DuckDbIndex(
            db_path=s.duckdb_path(),
            jsonl_dir=s.staging_jsonl_dir(),
            iceberg_warehouse=s.iceberg_warehouse,
        )
        end_dt = to_utc(end) if end else coerce_relative_end("now")
        where = ["fetch_ts >= $start", "fetch_ts <= $end"]
        params: dict[str, Any] = {"start": to_utc(start), "end": end_dt}
        if domain:
            where.append("domain = $dom")
            params["dom"] = domain
        if source:
            where.append("source_type = $src")
            params["src"] = source
        sql = f"""
            SELECT doc_id, capture_id, source_type, source_name, fetch_ts,
                   domain, title, length(text) AS text_len, language
            FROM captures
            WHERE {' AND '.join(where)}
            ORDER BY fetch_ts DESC
            LIMIT {int(limit)}
        """
        return idx.execute(sql, params)

    @app.get("/counts")
    def counts(start: datetime, end: datetime | None = None) -> dict[str, Any]:
        s = get_settings()
        idx = DuckDbIndex(
            db_path=s.duckdb_path(),
            jsonl_dir=s.staging_jsonl_dir(),
            iceberg_warehouse=s.iceberg_warehouse,
        )
        end_dt = to_utc(end) if end else coerce_relative_end("now")
        p = {"start": to_utc(start), "end": end_dt}
        total = idx.execute("SELECT COUNT(*) AS n FROM captures WHERE fetch_ts BETWEEN $start AND $end", p)
        by_source = idx.execute(
            "SELECT source_type, COUNT(*) AS n FROM captures WHERE fetch_ts BETWEEN $start AND $end GROUP BY source_type",
            p,
        )
        by_domain = idx.execute(
            "SELECT domain, COUNT(*) AS n FROM captures WHERE fetch_ts BETWEEN $start AND $end AND domain IS NOT NULL GROUP BY domain ORDER BY n DESC LIMIT 25",
            p,
        )
        return {"total": total, "by_source": by_source, "by_domain": by_domain}

    @app.get("/captures")
    def list_captures(
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        start: datetime | None = Query(None),
        end: datetime | None = Query(None),
        domain: str | None = Query(None),
        source: str | None = Query(None),
        search: str | None = Query(None),
    ) -> dict[str, Any]:
        s = get_settings()
        idx = DuckDbIndex(
            db_path=s.duckdb_path(),
            jsonl_dir=s.staging_jsonl_dir(),
            iceberg_warehouse=s.iceberg_warehouse,
        )
        where: list[str] = []
        params: dict[str, Any] = {}
        if start is not None:
            where.append("fetch_ts >= $start")
            params["start"] = to_utc(start)
        if end is not None:
            where.append("fetch_ts <= $end")
            params["end"] = to_utc(end)
        if domain:
            where.append("domain = $dom")
            params["dom"] = domain
        if source:
            where.append("source_type = $src")
            params["src"] = source
        if search:
            where.append("(title ILIKE $q OR text ILIKE $q)")
            params["q"] = f"%{search}%"
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""

        total = idx.execute(f"SELECT COUNT(*) AS n FROM captures{where_sql}", params)
        rows = idx.execute(
            f"""
            SELECT
              doc_id, capture_id, source_type, source_name,
              fetch_ts, observed_ts, domain, url, canonical_url,
              title, language, length(text) AS text_len,
              content_hash, parent_doc_or_dup_group
            FROM captures{where_sql}
            ORDER BY fetch_ts DESC
            LIMIT {int(limit)} OFFSET {int(offset)}
            """,
            params,
        )
        return {
            "total": int(total[0]["n"]) if total else 0,
            "limit": limit,
            "offset": offset,
            "rows": rows,
        }

    @app.get("/captures/{capture_id}")
    def capture_detail(capture_id: str) -> dict[str, Any]:
        s = get_settings()
        idx = DuckDbIndex(
            db_path=s.duckdb_path(),
            jsonl_dir=s.staging_jsonl_dir(),
            iceberg_warehouse=s.iceberg_warehouse,
        )
        rows = idx.execute(
            """
            SELECT * FROM captures WHERE capture_id = $cid LIMIT 1
            """,
            {"cid": capture_id},
        )
        if not rows:
            raise HTTPException(404, "capture not found")
        return rows[0]

    @app.get("/captures/{capture_id}/related")
    def capture_related(capture_id: str, limit: int = Query(12, ge=1, le=50)) -> dict[str, Any]:
        from awareness.storage.duckdb_index import find_related_captures  # local import

        s = get_settings()
        idx = DuckDbIndex(
            db_path=s.duckdb_path(),
            jsonl_dir=s.staging_jsonl_dir(),
            iceberg_warehouse=s.iceberg_warehouse,
        )
        conn = idx.connect()
        siblings = find_related_captures(conn, capture_id, limit=limit)
        return {"capture_id": capture_id, "siblings": siblings}

    @app.get("/search")
    def search(
        q: str = Query(..., min_length=1),
        limit: int = Query(30, ge=1, le=200),
        offset: int = Query(0, ge=0),
        source: str | None = Query(None),
        domain: str | None = Query(None),
        start: datetime | None = Query(None),
        end: datetime | None = Query(None),
        mode: str | None = Query(None, description="auto | fts | prefix | substring"),
        fields: str | None = Query(None, description="comma-list: title,text,domain,url"),
    ) -> dict[str, Any]:
        s = get_settings()
        idx = DuckDbIndex(
            db_path=s.duckdb_path(),
            jsonl_dir=s.staging_jsonl_dir(),
            iceberg_warehouse=s.iceberg_warehouse,
        )
        field_list = [
            f.strip().lower()
            for f in (fields or s.search_default_fields).split(",")
            if f.strip()
        ]
        return idx.search(
            q,
            limit=limit,
            offset=offset,
            source=source,
            domain=domain,
            start=to_utc(start) if start else None,
            end=to_utc(end) if end else None,
            mode=(mode or s.search_default_mode),
            fields=field_list,
            max_results=s.search_max_results,
        )

    # ── static dashboard ─────────────────────────────────────────────────
    web_dir = Path(__file__).resolve().parent / "web"
    if web_dir.exists():
        # Cache-bust version from file mtimes so UI refreshes after deploys.
        def _asset_ver() -> str:
            try:
                m = max(
                    (web_dir / n).stat().st_mtime_ns
                    for n in ("style.css", "app.js", "index.html")
                    if (web_dir / n).exists()
                )
                return str(m)
            except OSError:
                return "1"

        app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")

        @app.get("/", include_in_schema=False)
        def root_index() -> FileResponse:
            # Serve HTML with no-store so browsers always pick up new asset URLs.
            html_path = web_dir / "index.html"
            text = html_path.read_text(encoding="utf-8")
            ver = _asset_ver()
            text = text.replace("/static/style.css", f"/static/style.css?v={ver}")
            text = text.replace("/static/app.js", f"/static/app.js?v={ver}")
            from fastapi.responses import HTMLResponse  # noqa: PLC0415

            return HTMLResponse(
                content=text,
                headers={
                    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                    "Pragma": "no-cache",
                },
            )

        @app.middleware("http")
        async def _no_cache_static(request, call_next):  # type: ignore[no-untyped-def]
            response = await call_next(request)
            path = request.url.path
            if path == "/" or path.startswith("/static/"):
                response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
                response.headers["Pragma"] = "no-cache"
            return response

    return app


def run() -> None:
    """Entry for the ``awareness-api`` script."""
    import uvicorn  # noqa: PLC0415

    host = os.environ.get("AW_API_HOST", "127.0.0.1")
    port = int(os.environ.get("AW_API_PORT", "8085"))
    uvicorn.run("awareness.api.server:create_app", host=host, port=port, factory=True)


# WSGI/ASGI export so ``uvicorn awareness.api.server:app`` works too.
app = create_app()
