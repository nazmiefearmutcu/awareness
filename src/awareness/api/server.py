"""FastAPI HTTP control surface.

Endpoints:
    GET  /                       — web dashboard (static SPA)
    GET  /healthz                — liveness + search index readiness
    GET  /status                 — overall status
    GET  /metrics                — counters/histograms snapshot (JSON; ?format=prometheus)
    GET  /staging                — JSONL staging backlog (pending manifests + oldest age)
    POST /backfill               — submit
    POST /backfill/{id}/run      — run pending tasks (non-blocking task)
    GET  /backfill/{id}          — status
    GET  /jobs                   — list jobs
    POST /tail/start             — start tail (background task)
    POST /tail/stop              — stop tail
    GET  /tail                   — tail state
    GET  /inspect                — date/domain/source range query
    GET  /captures               — paginated capture listing for UI (unique=none|content|group)
    GET  /captures/{capture_id}  — full capture (incl. text) for UI detail view
    GET  /captures/{id}/related  — sibling captures in the same dup_group
    GET  /search                 — BM25-ranked full-text search w/ snippets
    GET  /counts                 — counts grouped by source, domain & language
    GET  /dedup-stats            — dedup index stats + process skip counters
    GET  /jobsearch/sources      — public job boards catalog
    GET  /jobsearch/profile      — personalization profile
    PUT  /jobsearch/profile      — save profile
    POST /jobsearch/search       — personalized live job search

Run with ``awareness-api`` script or ``uvicorn awareness.api.server:create_app``.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import threading
import time
from collections import abc as _abc
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Literal
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from awareness import __version__
from awareness.config import get_settings
from awareness.obs.logging import configure_logging, get_logger
from awareness.obs.metrics import get_metrics
from awareness.planner.planner import Planner
from awareness.schemas.doc import SourceKind
from awareness.schemas.jobs import BackfillRequest, JobStatus
from awareness.storage.duckdb_index import DuckDbIndex
from awareness.storage.state import StateDB
from awareness.tail.engine import TailEngine
from awareness.util.lang import PRIMARY_LANGUAGE_SQL, append_language_filter
from awareness.util.timeutil import coerce_relative_end, inclusive_end, to_utc
from awareness.workers.engine import WorkerEngine

logger = get_logger("api")

_LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


def _api_bind_host() -> str:
    """Configured bind host (AW_API_HOST, default loopback)."""
    return os.environ.get("AW_API_HOST", "127.0.0.1")


def _api_bind_port() -> str:
    """Configured bind port (AW_API_PORT, default 8085)."""
    return os.environ.get("AW_API_PORT", "8085")


def _guard_non_loopback_without_key() -> None:
    """Refuse to serve a non-loopback interface without AW_API_KEY.

    Binding to 0.0.0.0 / a LAN IP without a bearer token exposes the full
    control plane to the network. Called from ``run()`` before uvicorn binds
    and again from the lifespan startup (belt and suspenders, so factory/
    ``uvicorn awareness.api.server:create_app`` invocations are covered too).
    """
    import sys  # noqa: PLC0415

    host = _api_bind_host()
    if host in _LOOPBACK_HOSTS:
        return
    if get_settings().api_key:
        return
    logger.error(
        "api_binding_refused_non_loopback_without_key",
        host=host,
        hint="set AW_API_KEY before binding to a non-loopback interface",
    )
    sys.exit(1)

# State-changing routes that reject non-JSON bodies and cross-origin requests
# (CSRF posture: text/plain is CORS-safelisted and skips preflight).
_CSRF_ROUTE_PREFIXES = (
    "/backfill",
    "/tail/start",
    "/tail/stop",
    "/jobsearch/search",
    "/settings/",
    "/jobsearch/profile",
    "/alerts",
    "/saved",
    "/x/",
    "/consume",
)

# Mutating endpoints that legitimately accept a request without a body (their
# action is the verb itself). They still get the Origin gate; only the
# content-type/body requirements are relaxed for them.
_CSRF_BODYLESS_PATHS = (
    "/alerts/check",
)


def _is_csrf_protected(method: str, path: str) -> bool:
    if method not in ("POST", "PUT", "PATCH", "DELETE"):
        return False
    return path.startswith(_CSRF_ROUTE_PREFIXES)


def _origin_allowed(request: Request) -> bool:
    """True when a present Origin header matches the CONFIGURED server host.

    The configured host (AW_API_HOST/AW_API_PORT, default 127.0.0.1:8085) is
    the trust anchor, not the request's ``Host`` header — an attacker can
    spoof ``Host`` but cannot make a browser send an Origin for a host it
    did not actually load. Missing Origin stays permissive (those requests
    are still gated by the JSON/body rules above).
    """
    origin = request.headers.get("origin")
    if not origin:
        return True
    try:
        origin_host = urlsplit(origin).netloc
    except ValueError:
        return False
    if not origin_host:
        return False
    expected = f"{_api_bind_host()}:{_api_bind_port()}"
    if origin_host == expected:
        return True
    # Tolerate an Origin that omits the port (browsers normalize away the
    # default :80/:443) by comparing against the bare configured host.
    if ":" not in origin_host and origin_host == expected.split(":", 1)[0]:
        return True
    # Loopback aliases: an operator opening the UI via http://localhost:8085
    # (instead of 127.0.0.1) sends Origin: http://localhost:8085 — accept the
    # alias when the configured bind is loopback (including IPv6 [::1]).
    bind_host = _api_bind_host()
    port = _api_bind_port()
    if bind_host in ("127.0.0.1", "::1", "localhost"):
        return origin_host in (
            f"localhost:{port}",
            f"127.0.0.1:{port}",
            f"[::1]:{port}",
        )
    # 0.0.0.0 bind: accept the loopback aliases too (a LAN operator on the
    # machine's real IP sends the UI's own host as Origin — covered by the
    # exact-match above; loopback access via localhost/127.0.0.1 stays open).
    if bind_host == "0.0.0.0":
        return origin_host in (
            f"localhost:{port}",
            f"127.0.0.1:{port}",
            f"[::1]:{port}",
        )
    return False


# Simple in-process token-bucket limiter for abuse-prone endpoints. Per-key
# (path-class) buckets: each class allows _RATE_MAX_BURST requests per
# _RATE_WINDOW_SECONDS. Deliberately coarse (keyless localhost trust model);
# it exists to stop page-driven amplification (e.g. <img> GETs hammering
# /gdelt/*), not to replace real auth.
_RATE_WINDOW_SECONDS = 10.0
_RATE_MAX_BURST = 20
_RATE_LIMITED_PREFIXES = ("/gdelt/", "/consume/export", "/alerts/check")
_rate_buckets: dict[str, tuple[float, int]] = {}
_rate_lock = threading.Lock()


def _rate_allowed(path: str) -> bool:
    if not path.startswith(_RATE_LIMITED_PREFIXES):
        return True
    now = time.monotonic()
    key = next(p for p in _RATE_LIMITED_PREFIXES if path.startswith(p))
    with _rate_lock:
        last, count = _rate_buckets.get(key, (0.0, 0))
        if now - last >= _RATE_WINDOW_SECONDS:
            _rate_buckets[key] = (now, 1)
            return True
        if count >= _RATE_MAX_BURST:
            return False
        _rate_buckets[key] = (last, count + 1)
        return True


def _check_api_key(request: Request) -> str | None:
    """Return an error message when the request fails API-key auth.
    ``GET /healthz`` stays open for load-balancer probes. Only enforced when
    ``AW_API_KEY`` is set; without a key the localhost-trust behavior applies.
    """
    if request.method in ("GET", "HEAD") and request.url.path == "/healthz":
        return None
    settings = get_settings()
    expected = settings.api_key
    if not expected:
        return None
    authorization = request.headers.get("authorization", "")
    provided = ""
    if authorization.startswith("Bearer "):
        provided = authorization[7:].strip()
    if not provided or not hmac.compare_digest(provided, expected):
        return "missing or invalid API key"
    return None


def require_api_key(request: Request) -> None:
    """Optional bearer-token auth for the control plane (router dependency)."""
    error = _check_api_key(request)
    if error is not None:
        raise HTTPException(
            status_code=401,
            detail=error,
            headers={"WWW-Authenticate": "Bearer"},
        )


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
    index: DuckDbIndex | None = None
    background_tasks: set[asyncio.Task[Any]] = set()
    active_job_runs: ClassVar[set[str]] = set()


_index_lock = threading.Lock()


def _get_index() -> DuckDbIndex:
    """Return the process-wide DuckDbIndex (create once, double-checked locking)."""
    idx = _State.index
    if idx is not None:
        return idx
    with _index_lock:
        if _State.index is None:
            s = get_settings()
            _State.index = DuckDbIndex(
                db_path=s.duckdb_path(),
                jsonl_dir=s.staging_jsonl_dir(),
                iceberg_warehouse=s.iceberg_warehouse,
            )
        return _State.index


def _warmup_index() -> None:
    """Warm the search index at startup (W19).

    Runs health_snapshot() so the FIRST /healthz probe reports index_ready=True
    instead of a lazy cold-start False (health_snapshot connects + refreshes
    views). Acceptable one-time cost at startup; failure only warns — the
    index rebuilds lazily on the next request.
    """
    try:
        _get_index().health_snapshot()
    except Exception as exc:
        logger.warning("index_warmup_failed", error=str(exc))


def _close_index() -> None:
    """Close and clear the process-wide DuckDbIndex under the index lock.

    Call after settings that can change data_dir / duckdb / staging paths, and
    on lifespan shutdown, so the next _get_index() rebuilds against current paths.
    """
    with _index_lock:
        if _State.index is not None:
            _State.index.close()
            _State.index = None


# Fold key expressions for GET /captures?unique=…
# Keep newest fetch_ts per key via DISTINCT ON; empty/null hash falls back to capture_id.
_UNIQUE_FOLD_KEY_SQL: dict[str, str] = {
    "content": ("COALESCE(NULLIF(TRIM(CAST(content_hash AS VARCHAR)), ''), capture_id)"),
    "group": (
        "COALESCE("
        "NULLIF(TRIM(CAST(parent_doc_or_dup_group AS VARCHAR)), ''), "
        "NULLIF(TRIM(CAST(content_hash AS VARCHAR)), ''), "
        "capture_id)"
    ),
}

_CAPTURE_LIST_SELECT = """
              doc_id, capture_id, source_type, source_name,
              fetch_ts, observed_ts, domain, url, canonical_url,
              title, language, length(text) AS text_len,
              content_hash, parent_doc_or_dup_group
"""


def unique_fold_key_sql(unique: str) -> str | None:
    """Return SQL fold-key expression for unique mode, or None for no fold."""
    if unique in (None, "", "none"):
        return None
    expr = _UNIQUE_FOLD_KEY_SQL.get(unique)
    if expr is None:
        raise ValueError(f"invalid unique mode: {unique!r}")
    return expr


def query_captures_list(
    idx: DuckDbIndex,
    *,
    limit: int,
    offset: int,
    where: list[str] | None = None,
    params: dict[str, Any] | None = None,
    unique: str = "none",
) -> dict[str, Any]:
    """Paginated captures listing with optional unique folding (DuckDB).

    ``unique``:
      * ``none``    — all rows (default)
      * ``content`` — one row per content_hash (newest fetch_ts)
      * ``group``   — one row per parent_doc_or_dup_group / content_hash / capture_id
    """
    where = where or []
    params = dict(params or {})
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    fold_key = unique_fold_key_sql(unique)

    if fold_key is None:
        total_rows = idx.execute(f"SELECT COUNT(*) AS n FROM captures{where_sql}", params)
        rows = idx.execute(
            f"""
            SELECT{_CAPTURE_LIST_SELECT}
            FROM captures{where_sql}
            ORDER BY fetch_ts DESC
            LIMIT {int(limit)} OFFSET {int(offset)}
            """,
            params,
        )
    else:
        total_rows = idx.execute(
            f"SELECT COUNT(*) AS n FROM ("
            f"SELECT DISTINCT {fold_key} AS _fold_key FROM captures{where_sql}"
            f") _u",
            params,
        )
        rows = idx.execute(
            f"""
            SELECT * EXCLUDE (_fold_key) FROM (
              SELECT DISTINCT ON ({fold_key})
                {_CAPTURE_LIST_SELECT.strip()},
                {fold_key} AS _fold_key
              FROM captures{where_sql}
              ORDER BY {fold_key}, fetch_ts DESC
            ) _folded
            ORDER BY fetch_ts DESC
            LIMIT {int(limit)} OFFSET {int(offset)}
            """,
            params,
        )

    return {
        "total": int(total_rows[0]["n"]) if total_rows else 0,
        "limit": limit,
        "offset": offset,
        "unique": unique if unique else "none",
        "rows": rows,
    }


def create_app() -> FastAPI:  # noqa: PLR0915 - route surface is spec-mandated
    settings = get_settings()
    configure_logging(level=settings.log_level, json=settings.log_json, log_dir=settings.log_dir)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        _guard_non_loopback_without_key()
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
        except Exception as exc:
            logger.warning("orphan_tail_reconcile_failed", error=str(exc))
        _State.state = state
        _State.planner = Planner(state)
        _State.tail = TailEngine(state, _State.planner)

        # W19: warm the search index so the first /healthz probe reports
        # index_ready=True instead of a lazy cold-start False.
        _warmup_index()

        reaper = None
        if settings.reaper_enabled:
            from awareness.workers.engine import DatabaseReaper

            reaper = DatabaseReaper(state)
            await reaper.start()

        # ── alerts runner (feature): opt-in periodic alert evaluation ──
        # Gated on AW_ALERTS_AUTOSTART=1 (default off). Runs over the same
        # lazy index the API serves; stopped below before the index closes.
        alerts_runner = None
        if os.environ.get("AW_ALERTS_AUTOSTART") == "1":
            from awareness.alerts.runner import create_default_runner  # noqa: PLC0415

            alerts_runner = create_default_runner(_get_index)
            await alerts_runner.start()

        try:
            yield
        finally:
            if reaper:
                await reaper.stop()
            if _State.tail and _State.tail.running:
                await _State.tail.stop(drain_seconds=10.0)
            # Cancel AND await background jobs so their finally blocks (engine
            # close, active-run markers) run before the loop shuts down.
            pending = [t for t in list(_State.background_tasks) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            # Stop the alerts runner before the index closes underneath it.
            if alerts_runner is not None:
                await alerts_runner.stop()
            if _alert_store_instance is not None:
                try:
                    _alert_store_instance.close()
                except Exception as exc:
                    logger.warning("alerts_store_close_failed", error=str(exc))
            _close_saved_store()
            _close_index()
            # Drain process-wide pooled httpx clients so sockets/TLS sessions
            # do not leak across uvicorn reloads / process exit.
            try:
                from awareness.util.http import aclose_shared_async_clients

                await aclose_shared_async_clients()
            except Exception as exc:
                logger.warning("shared_http_clients_shutdown_failed", error=str(exc))

    app = FastAPI(
        title="Awareness",
        version=__version__,
        lifespan=lifespan,
        dependencies=[Depends(require_api_key)],
    )

    @app.middleware("http")
    async def _security(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Auth fallback (covers mounts) + CSRF posture for mutating routes.

        The API-key dependency handles router routes; this middleware catches
        anything it does not (e.g. the static mount) and enforces the CSRF
        rules: mutating requests on protected prefixes must carry a non-empty
        ``application/json`` body (415 for other content types, 422 for empty
        bodies) and a present ``Origin`` must match the configured server host
        (403 otherwise). Middlewares cannot raise HTTPException (Starlette only
        converts those below ExceptionMiddleware), so auth failures here return
        a response directly.
        """
        auth_error = _check_api_key(request)
        if auth_error is not None:
            return PlainTextResponse(
                f"401: {auth_error}",
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        if _is_csrf_protected(request.method, request.url.path):
            # Origin gate applies to every mutating route (body or not).
            if not _origin_allowed(request):
                return PlainTextResponse("403: cross-origin request rejected", status_code=403)
            # Content-type/body requirements: relaxed for endpoints that
            # legitimately take no body (their action is the verb itself —
            # bodyless POSTs listed in _CSRF_BODYLESS_PATHS, and DELETE which
            # is never CORS-safelisted and always preflighted). Everything
            # else must carry a non-empty JSON body.
            if request.method != "DELETE" and request.url.path not in _CSRF_BODYLESS_PATHS:
                ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
                if ctype != "application/json":
                    return PlainTextResponse("415: content-type must be application/json", status_code=415)
                body = await request.body()
                if not body or not body.strip():
                    return PlainTextResponse("422: empty body not allowed", status_code=422)
        # Rate limit AFTER auth+CSRF: blocked cross-origin/unauthorized
        # traffic must not burn the operator's budget (W1 finding 2).
        if not _rate_allowed(request.url.path):
            return PlainTextResponse(
                "429: rate limit exceeded (abuse-prone endpoint)", status_code=429
            )
        return await call_next(request)

    @app.get("/healthz")
    @app.head("/healthz")
    def healthz() -> dict[str, Any]:
        """Liveness probe plus search-index readiness.

        ``ok`` stays True while the process can answer (liveness). ``index_ready``
        reports whether DuckDB views are queryable; clients that need search
        should wait for ``index_ready`` (and optionally ``index.fts_built``).
        No config values (state db URL, data dir) are exposed here.
        """
        out: dict[str, Any] = {
            "ok": True,
            "version": __version__,
            "index_ready": False,
            "index": None,
        }
        try:
            idx = _get_index()
            snap = idx.health_snapshot()
            # /healthz is unauthenticated: never leak filesystem paths
            # (db_path / jsonl_dir) to anonymous probes.
            out["index"] = {k: v for k, v in snap.items() if k not in ("db_path", "jsonl_dir")}
            out["index_ready"] = bool(snap.get("ready"))
        except Exception as exc:
            logger.warning("healthz_index_probe_failed", error=str(exc))
            out["index"] = {
                "ready": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
            out["index_ready"] = False
        return out

    @app.get("/status")
    def status() -> dict[str, Any]:
        st = _State.state
        if st is None:
            raise HTTPException(500, "not initialized")
        jobs = [j.model_dump(mode="json") for j in st.list_jobs(limit=10)]
        return {"tail": st.get_tail(), "jobs": jobs}

    @app.get("/metrics")
    def metrics(
        request: Request,
        format: str | None = Query(
            default=None,
            description="Response format: omit/json (default) or prometheus/prom/text",
        ),
    ) -> Any:
        """Process metrics as JSON snapshot or Prometheus text exposition.

        Default remains JSON for the SPA/dashboard. Pass ``?format=prometheus``
        (aliases: ``prom``, ``text``) or ``Accept: text/plain`` to scrape with
        Prometheus / VictoriaMetrics / Grafana Alloy.
        """
        fmt = (format or "").strip().lower()
        accept = (request.headers.get("accept") or "").lower()
        want_prom = fmt in ("prometheus", "prom", "text", "exposition") or (
            "text/plain" in accept and "application/json" not in accept
        )
        if want_prom:
            body = get_metrics().render_prometheus()
            return PlainTextResponse(
                content=body,
                media_type="text/plain; version=0.0.4; charset=utf-8",
            )
        return get_metrics().snapshot()

    @app.get("/dedup-stats")
    def dedup_stats() -> dict[str, Any]:
        if _State.state is None:
            raise HTTPException(500, "not initialized")
        stats: dict[str, Any] = dict(_State.state.dedup_stats())
        # Process-local skip counters (also on GET /metrics). Cheap sums over labels.
        m = get_metrics()
        stats["fetch_skipped_seen"] = int(m.counter_sum("tail.fetch_skipped_seen"))
        stats["tight_near_skipped"] = int(m.counter_sum("dedup.tight_near_skipped"))
        return stats

    @app.get("/staging")
    def staging(
        include_manifests: bool = Query(
            default=True,
            description="Include per-manifest rows (path/records/bytes/age). "
            "Set false for a lightweight backlog summary only.",
        ),
    ) -> dict[str, Any]:
        """JSONL staging backlog pending Iceberg compaction.

        Mirrors ``awareness compact --status/--json``: pending chunk count,
        total records/bytes, oldest committed_at + age_seconds so operators
        and the SPA can see warehouse fold lag without shell access.
        """
        st = _State.state
        if st is None:
            raise HTTPException(500, "not initialized")
        summary = st.pending_manifest_summary()
        if not include_manifests:
            # Drop the potentially large per-file list for cheap polling.
            return {
                "pending_count": summary.get("pending_count", 0),
                "total_records": summary.get("total_records", 0),
                "total_bytes": summary.get("total_bytes", 0),
                "oldest_committed_at": summary.get("oldest_committed_at"),
                "oldest_age_seconds": summary.get("oldest_age_seconds"),
            }
        return summary

    @app.post("/backfill")
    def submit_backfill(body: BackfillBody) -> dict[str, Any]:
        if _State.planner is None:
            raise HTTPException(500, "not initialized")
        try:
            end = body.end or (coerce_relative_end(body.end_str or "now"))
            srcs = [SourceKind(s) for s in body.sources] if body.sources else []
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, f"invalid backfill parameters: {exc}") from exc
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
        # In-flight guard: one engine per job. The in-process marker is race-free
        # (both requests share the event loop); the DB RUNNING check catches a
        # second process racing to run the same job.
        if job_id in _State.active_job_runs:
            raise HTTPException(409, f"job {job_id} is already running")
        job = _State.state.get_job(job_id)
        if job is not None and job.status == JobStatus.RUNNING:
            raise HTTPException(409, f"job {job_id} is already running")
        engine = WorkerEngine(_State.state, _State.planner)

        async def _runner() -> None:
            try:
                await engine.run_job(job_id)
            finally:
                _State.active_job_runs.discard(job_id)
                await engine.aclose()

        _State.active_job_runs.add(job_id)
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
    def list_jobs(limit: int = Query(20, ge=1, le=500)) -> list[dict[str, Any]]:
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
        if body.gdelt_max_urls != 0 and not (1 <= body.gdelt_max_urls <= 100_000):
            raise HTTPException(400, "gdelt_max_urls must be 0 (config default) or in 1..100000")
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
                "retry_scheduled_count": 0,
                "retry_scheduled": [],
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
            except Exception as exc:
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
            "retry_scheduled_count": state.count_retry_scheduled(job_id) if live else 0,
            "retry_scheduled": state.list_retry_scheduled_tasks(job_id, limit=10) if live else [],
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
        idx = _get_index()
        end_dt = inclusive_end(to_utc(end)) if end else coerce_relative_end("now")
        where = ["fetch_ts >= $start", "fetch_ts <= $end"]
        params: dict[str, Any] = {"start": to_utc(start), "end": end_dt}
        if domain:
            where.append("lower(domain) = $dom")
            params["dom"] = str(domain).strip().lower()
        if source:
            where.append("lower(source_type) = $src")
            params["src"] = str(source).strip().lower()
        sql = f"""
            SELECT doc_id, capture_id, source_type, source_name, fetch_ts,
                   domain, title, length(text) AS text_len, language
            FROM captures
            WHERE {" AND ".join(where)}
            ORDER BY fetch_ts DESC
            LIMIT {int(limit)}
        """
        return idx.execute(sql, params)

    @app.get("/counts")
    def counts(start: datetime, end: datetime | None = None) -> dict[str, Any]:
        idx = _get_index()
        end_dt = inclusive_end(to_utc(end)) if end else coerce_relative_end("now")
        p = {"start": to_utc(start), "end": end_dt}
        total = idx.execute("SELECT COUNT(*) AS n FROM captures WHERE fetch_ts BETWEEN $start AND $end", p)
        # Case-normalize source buckets (RSS vs rss) so dashboard chips match filters.
        by_source = idx.execute(
            """
            SELECT lower(CAST(source_type AS VARCHAR)) AS source_type, COUNT(*) AS n
            FROM captures
            WHERE fetch_ts BETWEEN $start AND $end
              AND source_type IS NOT NULL
              AND CAST(source_type AS VARCHAR) != ''
            GROUP BY 1
            ORDER BY n DESC
            """,
            p,
        )
        by_domain = idx.execute(
            "SELECT domain, COUNT(*) AS n FROM captures WHERE fetch_ts BETWEEN $start AND $end AND domain IS NOT NULL GROUP BY domain ORDER BY n DESC LIMIT 25",
            p,
        )
        # Primary BCP-47 tags so en / en-US / en_GB roll into one "en" bucket.
        by_language = idx.execute(
            f"""
            SELECT {PRIMARY_LANGUAGE_SQL} AS language, COUNT(*) AS n
            FROM captures
            WHERE fetch_ts BETWEEN $start AND $end
              AND language IS NOT NULL
              AND CAST(language AS VARCHAR) != ''
            GROUP BY 1
            ORDER BY n DESC
            LIMIT 50
            """,
            p,
        )
        return {
            "total": total,
            "by_source": by_source,
            "by_domain": by_domain,
            "by_language": by_language,
        }

    @app.get("/captures")
    def list_captures(
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        start: datetime | None = Query(None),
        end: datetime | None = Query(None),
        domain: str | None = Query(None),
        source: str | None = Query(None),
        language: str | None = Query(None, description="BCP-47 language tag filter"),
        search: str | None = Query(None),
        unique: Literal["none", "content", "group"] = Query(
            "none",
            description="Collapse duplicates: none | content (content_hash) | group (dup group)",
        ),
    ) -> dict[str, Any]:
        idx = _get_index()
        where: list[str] = []
        params: dict[str, Any] = {}
        if start is not None:
            where.append("fetch_ts >= $start")
            params["start"] = to_utc(start)
        if end is not None:
            where.append("fetch_ts <= $end")
            params["end"] = inclusive_end(to_utc(end))
        if domain:
            # Case-insensitive: SPA may send Example.COM; captures store lower eTLD+1.
            where.append("lower(domain) = $dom")
            params["dom"] = str(domain).strip().lower()
        if source:
            # Case-insensitive: RSS vs rss / Common_Crawl_Wet vs common_crawl_wet.
            where.append("lower(source_type) = $src")
            params["src"] = str(source).strip().lower()
        # BCP-47: primary tags (en) match regional subtags (en-US); case/underscore-insensitive.
        append_language_filter(where, params, language)
        if search:
            where.append("(title ILIKE $q OR text ILIKE $q)")
            params["q"] = f"%{search}%"
        return query_captures_list(
            idx,
            limit=limit,
            offset=offset,
            where=where,
            params=params,
            unique=unique,
        )

    @app.get("/captures/{capture_id}")
    def capture_detail(capture_id: str) -> dict[str, Any]:
        idx = _get_index()
        rows = idx.execute(
            """
            SELECT * FROM captures WHERE capture_id = $cid LIMIT 1
            """,
            {"cid": capture_id},
        )
        if not rows:
            raise HTTPException(404, "capture not found")
        row = dict(rows[0])
        # Total siblings in the same dup-group (for reader title badge).
        row["related_count"] = idx.related_count(capture_id)
        return row

    @app.get("/captures/{capture_id}/related")
    def capture_related(capture_id: str, limit: int = Query(12, ge=1, le=50)) -> dict[str, Any]:
        idx = _get_index()
        siblings = idx.related(capture_id, limit=limit)
        # Full sibling total (may exceed *limit*); UI uses this for counts.
        related_count = idx.related_count(capture_id)
        return {
            "capture_id": capture_id,
            "siblings": siblings,
            "related_count": related_count,
        }

    @app.get("/search")
    def search(
        q: str = Query(..., min_length=1),
        limit: int = Query(30, ge=1, le=200),
        offset: int = Query(0, ge=0),
        source: str | None = Query(None),
        domain: str | None = Query(None),
        language: str | None = Query(None, description="BCP-47 language tag filter"),
        start: datetime | None = Query(None),
        end: datetime | None = Query(None),
        mode: str | None = Query(None, description="auto | fts | prefix | substring"),
        fields: str | None = Query(None, description="comma-list: title,text,domain,url"),
    ) -> dict[str, Any]:
        s = get_settings()
        idx = _get_index()
        field_list = [f.strip().lower() for f in (fields or s.search_default_fields).split(",") if f.strip()]
        return idx.search(
            q,
            limit=limit,
            offset=offset,
            source=source,
            domain=domain,
            language=language,
            start=to_utc(start) if start else None,
            end=inclusive_end(to_utc(end)) if end else None,
            mode=(mode or s.search_default_mode),
            fields=field_list,
            max_results=s.search_max_results,
        )

    # ── settings (engine config + tail seeds) ────────────────────────────
    @app.get("/settings/schema")
    def settings_schema() -> dict[str, Any]:
        from awareness.config.persist import schema_payload

        return schema_payload()

    @app.put("/settings/config")
    def settings_put_config(body: dict[str, Any]) -> dict[str, Any]:
        from awareness.config.persist import apply_updates

        # Accept {values: {...}} or flat map
        values = body.get("values") if isinstance(body.get("values"), dict) else body
        if not isinstance(values, dict):
            raise HTTPException(400, "expected object of key → value")
        # Strip meta keys if flat
        values = {k: v for k, v in values.items() if k not in ("values", "note")}
        result = apply_updates(values)
        # data_dir (and derived duckdb/staging paths) may have changed; always
        # drop the singleton so the next request rebuilds against new paths.
        # Slightly cold after any config apply; safer than path-key heuristics.
        if result.get("applied"):
            _close_index()
        return result

    @app.get("/settings/tail-seeds")
    def settings_get_tail_seeds() -> dict[str, Any]:
        from awareness.config.persist import read_tail_seeds

        return read_tail_seeds()

    @app.put("/settings/tail-seeds")
    def settings_put_tail_seeds(body: dict[str, Any]) -> dict[str, Any]:
        from awareness.config.persist import write_tail_seeds

        try:
            return write_tail_seeds(body or {})
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    # ── job search (public boards + personalization) ─────────────────────
    @app.get("/jobsearch/sources")
    def jobsearch_sources() -> dict[str, Any]:
        from awareness.jobsearch.engine import JobSearchEngine

        s = get_settings()
        eng = JobSearchEngine(s.data_dir)
        return {"sources": eng.catalog()}

    @app.get("/jobsearch/profile")
    def jobsearch_get_profile() -> dict[str, Any]:
        from awareness.jobsearch.engine import JobSearchEngine

        s = get_settings()
        eng = JobSearchEngine(s.data_dir)
        return eng.get_profile().model_dump(mode="json")

    @app.put("/jobsearch/profile")
    def jobsearch_put_profile(body: dict[str, Any]) -> dict[str, Any]:
        from awareness.jobsearch.engine import JobSearchEngine
        from awareness.jobsearch.models import profile_from_flat

        s = get_settings()
        eng = JobSearchEngine(s.data_dir)
        profile = profile_from_flat(body)
        return eng.put_profile(profile).model_dump(mode="json")

    @app.post("/jobsearch/search")
    async def jobsearch_search(body: dict[str, Any]) -> dict[str, Any]:
        from awareness.jobsearch.engine import JobSearchEngine
        from awareness.jobsearch.models import JobSearchRequest, profile_from_flat

        s = get_settings()
        eng = JobSearchEngine(s.data_dir)
        profile = None
        if body.get("profile") is not None:
            profile = profile_from_flat(body["profile"] if isinstance(body["profile"], dict) else body)
        elif any(k in body for k in ("titles", "skills", "locations", "sources", "remote_only")):
            profile = profile_from_flat(body)
        try:
            li_pages = int(body.get("linkedin_pages") or 3)
        except (TypeError, ValueError):
            li_pages = 3
        try:
            limit = int(body.get("limit") or 40)
        except (TypeError, ValueError):
            raise HTTPException(400, "limit must be an integer") from None
        limit = max(1, min(limit, 100))
        req = JobSearchRequest(
            q=str(body.get("q") or ""),
            profile=profile,
            limit=limit,
            save_profile=bool(body.get("save_profile", False)),
            linkedin_pages=max(1, min(li_pages, 5)),
        )
        result = await eng.search(req)
        return result.model_dump(mode="json")

    # ── feature routers (analytics / alerts / entities / sentiment / origin / source-intel / consume) ──
    from awareness.alerts.router import create_alerts_router
    from awareness.alerts.store import AlertStore
    from awareness.analytics.router import create_analytics_router
    from awareness.briefings.router import create_briefings_router  # noqa: PLC0415
    from awareness.consume.router import wire
    from awareness.corpusx.router import create_corpusx_router
    from awareness.entities.router import create_entities_router
    from awareness.gdeltx.router import create_gdeltx_router
    from awareness.origin.router import create_origin_router
    from awareness.qualityx.router import create_qualityx_router  # noqa: PLC0415
    from awareness.savedsearch.router import create_savedsearch_router  # noqa: PLC0415
    from awareness.savedsearch.store import SavedSearchStore  # noqa: PLC0415
    from awareness.sentiment.router import create_sentiment_router
    from awareness.sourceintel.router import router as sourceintel_router
    from awareness.topicx.router import create_topicx_router

    app.include_router(create_analytics_router(_get_index))
    # Saved briefings: filesystem-backed (settings.data_dir / "briefings"),
    # resolved lazily per request so CLI-written files appear without restart.
    app.include_router(create_briefings_router(lambda: get_settings().data_dir / "briefings"))
    app.include_router(create_corpusx_router(_get_index))
    app.include_router(create_entities_router(_get_index))
    app.include_router(sourceintel_router)
    app.include_router(create_sentiment_router(_get_index))
    app.include_router(create_origin_router(_get_index))
    app.include_router(create_gdeltx_router(_get_index))
    app.include_router(create_topicx_router(_get_index))

    # Process-wide AlertStore: one SQLite connection for the app lifetime,
    # closed on shutdown. (Per-request construction leaked a connection + WAL
    # lock for every /alerts call.)
    _alert_store_instance: AlertStore | None = None

    def _alert_store() -> AlertStore:
        nonlocal _alert_store_instance
        if _alert_store_instance is None:
            _alert_store_instance = AlertStore(settings.data_dir / "alerts.db")
        return _alert_store_instance

    # Process-wide SavedSearchStore: one SQLite connection for the app lifetime,
    # closed on shutdown (same pattern as the AlertStore above).
    _saved_store_instance: SavedSearchStore | None = None

    def _saved_store() -> SavedSearchStore:
        nonlocal _saved_store_instance
        if _saved_store_instance is None:
            _saved_store_instance = SavedSearchStore(
                settings.data_dir / "saved_searches.db"
            )
        return _saved_store_instance

    def _close_saved_store() -> None:
        if _saved_store_instance is not None:
            try:
                _saved_store_instance.close()
            except Exception as exc:
                logger.warning("saved_store_close_failed", error=str(exc))

    app.include_router(create_alerts_router(_get_index, _alert_store))
    app.include_router(create_savedsearch_router(_saved_store, _get_index))
    app.include_router(create_qualityx_router(_get_index))
    wire(app)

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

    host = _api_bind_host()
    port = int(_api_bind_port())
    _guard_non_loopback_without_key()
    uvicorn.run("awareness.api.server:create_app", host=host, port=port, factory=True)


# WSGI/ASGI export so ``uvicorn awareness.api.server:app`` works too.
app = create_app()
