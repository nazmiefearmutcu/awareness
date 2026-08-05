"""Time-series corpus quality for the awareness project.

:mod:`awareness.qualityx` adds a time dimension to the point-in-time
corpus-quality snapshot (:class:`~awareness.corpusx.engine.CorpusXEngine`):
per-bucket (day/week/month) aggregates of corpus quality computed directly
from the ``captures`` view — so the history works on old corpus data — plus
a ``current`` snapshot delegate. Bucketing is the analytics engine's
calendar arithmetic (UTC), and every scan is bounded.
"""

from awareness.qualityx.engine import QualityTimeEngine
from awareness.qualityx.models import QualityHistory, QualityPoint

__all__ = [
    "QualityHistory",
    "QualityPoint",
    "QualityTimeEngine",
]
