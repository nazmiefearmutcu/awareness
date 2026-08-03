"""Source-quality / replication-analysis subsystem.

Mines the captures lake (via ``awareness.storage.duckdb_index.DuckDbIndex``)
for per-domain source intelligence: quality ranking, replication ("who
copies whom") edges derived from the dedup structure, and freshness/staleness
reporting. See ``awareness.sourceintel.engine`` for the scoring formula.
"""

from awareness.sourceintel.engine import SourceIntelEngine, UnknownDomainError
from awareness.sourceintel.models import (
    DomainFreshness,
    DomainProfile,
    DomainScore,
    LanguageShare,
    ReplicationEdge,
    SourceTypeShare,
    TermCount,
)

__all__ = [
    "DomainFreshness",
    "DomainProfile",
    "DomainScore",
    "LanguageShare",
    "ReplicationEdge",
    "SourceIntelEngine",
    "SourceTypeShare",
    "TermCount",
    "UnknownDomainError",
]
