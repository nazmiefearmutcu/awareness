"""``awareness-markets`` — the showme-facing service (API + collector runner, one process).

Run: ``awareness-markets`` or ``python -m awareness.markets.app`` (127.0.0.1:8086 by default,
``AW_MARKETS_HOST`` / ``AW_MARKETS_PORT`` to change). The runner starts automatically unless
``AW_MARKETS_AUTOSTART_RUNNER=false``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI

from awareness.markets import __version__
from awareness.markets.api import MarketsAPI, create_markets_router
from awareness.markets.collectors.base import Collector, CollectorContext
from awareness.markets.config import MarketsSettings, get_markets_settings
from awareness.markets.http import Fetcher
from awareness.markets.scheduler import MarketsRunner, default_collectors
from awareness.markets.sources.registry import PublisherRegistry
from awareness.markets.store import MarketsStore

log = logging.getLogger("awareness.markets.app")


class MarketsService:
    """Owns store, registry, fetcher, pipeline and runner for one process."""

    def __init__(self, settings: MarketsSettings, *, collectors: Sequence[Collector] | None = None) -> None:
        self.settings = settings
        self.store = MarketsStore(settings.store_path)
        self.registry = PublisherRegistry.load(settings.config_file("publishers.yaml"))
        self.fetcher = Fetcher(settings, self.store)
        self.pipeline: Any = None
        self.pipeline_error: str | None = None
        self._collectors = collectors
        self.runner: MarketsRunner | None = None
        self._recluster_task: asyncio.Task[None] | None = None
        self.store.ensure_watchlist(settings.watchlist)

    def build_pipeline(self) -> None:
        try:
            from awareness.markets.news.pipeline import NewsPipeline

            self.pipeline = NewsPipeline.build(
                settings=self.settings, store=self.store, registry=self.registry, fetcher=self.fetcher
            )
        except Exception as exc:  # news collectors will fail loudly and report health
            self.pipeline_error = f"{type(exc).__name__}: {exc}"
            log.error("news pipeline unavailable: %s", self.pipeline_error)

    def context(self) -> CollectorContext:
        return CollectorContext(
            settings=self.settings,
            store=self.store,
            http=self.fetcher,
            registry=self.registry,
            pipeline=self.pipeline,
            now=lambda: datetime.now(UTC),
        )

    async def start_runner(self) -> None:
        if self.pipeline is None and self.pipeline_error is None:
            self.build_pipeline()
        collectors = list(self._collectors) if self._collectors is not None else default_collectors()
        self.runner = MarketsRunner(self.context(), collectors)
        await self.runner.start()
        if self.pipeline is not None and hasattr(self.pipeline, "recluster"):
            self._recluster_task = asyncio.create_task(self._recluster_once())

    async def _recluster_once(self) -> None:
        try:
            from awareness.markets.news.pipeline import NLP_VERSION

            if self.store.kv_get("nlp_version") != NLP_VERSION and hasattr(self.pipeline, "reenrich"):
                count = await self.pipeline.reenrich(hours=96)
                self.store.kv_set("nlp_version", NLP_VERSION)
                log.info("startup re-enrichment: %d items follow NLP %s", count, NLP_VERSION)
            changed = await self.pipeline.recluster()
            log.info("startup recluster changed %d items", changed)
        except Exception as exc:  # never block the service on maintenance
            log.warning("startup maintenance failed: %s", exc)

    async def close(self) -> None:
        if self.runner is not None:
            await self.runner.stop()
        await self.fetcher.aclose()
        self.store.close()


def create_app(
    settings: MarketsSettings | None = None,
    *,
    start_runner: bool | None = None,
    collectors: Sequence[Collector] | None = None,
) -> FastAPI:
    settings = settings or get_markets_settings()
    holder: dict[str, MarketsService] = {}

    def service() -> MarketsService:
        if "svc" not in holder:
            holder["svc"] = MarketsService(settings, collectors=collectors)
        return holder["svc"]

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        svc = service()
        run = settings.autostart_runner if start_runner is None else start_runner
        if run:
            await svc.start_runner()
        try:
            yield
        finally:
            await svc.close()
            holder.pop("svc", None)

    app = FastAPI(
        title="Awareness Markets",
        version=__version__,
        description="Sourced news, sentiment, economic & corporate calendars for showme.",
        lifespan=lifespan,
    )
    api = MarketsAPI(
        settings=settings,
        get_store=lambda: service().store,
        get_registry=lambda: service().registry,
        get_runner=lambda: service().runner,
    )
    app.include_router(create_markets_router(api))

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        svc = service()
        return {
            "ok": True,
            "service": "awareness-markets",
            "version": __version__,
            "runner": bool(svc.runner and svc.runner.running),
            "pipeline": svc.pipeline is not None,
            "pipeline_error": svc.pipeline_error,
        }

    return app


def run(argv: Sequence[str] | None = None) -> None:
    import uvicorn

    parser = argparse.ArgumentParser(prog="awareness-markets", description=__doc__)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-runner", action="store_true", help="serve the API without collecting")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)
    settings = get_markets_settings()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = create_app(settings, start_runner=False if args.no_runner else None)
    uvicorn.run(
        app,
        host=args.host or settings.host,
        port=args.port or settings.port,
        log_level=args.log_level,
        access_log=False,
    )


if __name__ == "__main__":
    run()
