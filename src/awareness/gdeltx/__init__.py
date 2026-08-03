"""GDELT analytics bridge.

Cross-references external GDELT DOC 2.0 article counts with the local
capture corpus: per-term/per-day series, Pearson correlation between the
two, and coverage-gap signals ("GDELT says this story is big, but our
capture rate is near zero"). The bridge is resilient when offline — every
GDELT API failure degrades to an empty series with a structured-log
warning, never an exception.

Exposes the :class:`~awareness.gdeltx.engine.GdeltBridge` and the FastAPI
router factory :func:`~awareness.gdeltx.router.create_gdeltx_router`.
"""

from __future__ import annotations

from awareness.gdeltx.engine import GdeltBridge
from awareness.gdeltx.models import GapReport, GdeltComparison, GdeltWindow
from awareness.gdeltx.router import create_gdeltx_router

__all__ = [
    "GapReport",
    "GdeltBridge",
    "GdeltComparison",
    "GdeltWindow",
    "create_gdeltx_router",
]
