"""Collector protocol and shared context (see :mod:`.base`)."""

from awareness.markets.collectors.base import (
    Collector,
    CollectorContext,
    CollectorResult,
    NewsPipelineProtocol,
)

__all__ = ["Collector", "CollectorContext", "CollectorResult", "NewsPipelineProtocol"]
