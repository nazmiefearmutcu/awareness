"""The collector contract.

A collector fetches one provider (or one family of endpoints), parses the
payload with pure ``parse_*`` functions and upserts typed records into the
store. It raises on provider failure so the scheduler can record health; it
never fabricates a value to paper over a failure.

Implementations live next to their parsers (``news/feeds.py``,
``calendar/economic.py`` …) and are registered by the scheduler.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from awareness.markets.config import MarketsSettings
from awareness.markets.http import Fetcher
from awareness.markets.models import NewsItem, RawNewsItem
from awareness.markets.store import MarketsStore

if TYPE_CHECKING:
    from awareness.markets.sources.registry import PublisherRegistry


def utcnow() -> datetime:
    return datetime.now(UTC)


@runtime_checkable
class NewsPipelineProtocol(Protocol):
    """Enrich + quality-gate + cluster + persist raw items; returns stored items."""

    async def process(self, raws: list[RawNewsItem]) -> list[NewsItem]: ...


@dataclass
class CollectorContext:
    settings: MarketsSettings
    store: MarketsStore
    http: Fetcher
    registry: PublisherRegistry
    pipeline: NewsPipelineProtocol | None = None
    now: Callable[[], datetime] = utcnow
    log: Any = field(default_factory=lambda: logging.getLogger("awareness.markets"))


@dataclass
class CollectorResult:
    items: int = 0
    notes: list[str] = field(default_factory=list)


@runtime_checkable
class Collector(Protocol):
    """``name`` is the provider id used for health rows (e.g. ``econ.nasdaq``)."""

    name: str
    interval_s: int
    lock_group: str | None

    async def run(self, ctx: CollectorContext) -> CollectorResult: ...
