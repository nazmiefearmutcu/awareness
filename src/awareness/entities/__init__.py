"""Entity analysis — heuristic NER + corpus co-occurrence / correlation."""

from __future__ import annotations

from awareness.entities.engine import EntityEngine
from awareness.entities.extract import extract_entities, normalize_entity
from awareness.entities.models import (
    CoOccurrence,
    CorrelationResult,
    ExtractedEntity,
    TimeBucket,
)

__all__ = [
    "CoOccurrence",
    "CorrelationResult",
    "EntityEngine",
    "ExtractedEntity",
    "TimeBucket",
    "extract_entities",
    "normalize_entity",
]
