"""Deduplication engine — exact + canonical-URL + simhash near-duplicate.

Design principles (per spec):
- Decision only: the worker decides what to persist. EXACT_DUP captures are
  skipped at storage time (content already on disk under another URL);
  REVISION is likewise skipped (same bytes re-fetched). Tight NEAR_DUP
  (Hamming ≤ :data:`TIGHT_NEAR_STORE_THRESHOLD`) is also skipped — near-
  identical text is not worth a second full-text row. Looser NEAR_DUP and
  NEW still land for provenance.
- ``parent_doc_or_dup_group`` is set so downstream queries can fold captures
  into canonical docs (``WHERE doc_id = parent_doc_or_dup_group``).
- Decision space:
    * NEW            — first time we see this content_hash
    * REVISION       — same canonical URL re-fetched, same content
    * EXACT_DUP      — same content seen from a different canonical URL
    * NEAR_DUP       — near-duplicate of an existing doc by simhash threshold

The engine writes dedup index rows as a side effect and mutates
``cap.parent_doc_or_dup_group`` in place.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from awareness.obs.logging import get_logger
from awareness.schemas.doc import DocCapture
from awareness.storage.state import StateDB
from awareness.util.hashing import hamming128, simhash128

logger = get_logger("dedup")

# Default near-duplicate merge threshold in Hamming bits over the 128-bit
# signature. Must stay <= (NEAR_DUP_SEGMENTS - 1) so the band index's pigeonhole
# guarantee covers it — see tests/unit/test_dedup_invariant.py.
DEFAULT_NEAR_THRESHOLD = 24

# NEAR_DUP captures at or below this Hamming distance are treated like
# EXACT_DUP for storage: count as a dedup drop, do not re-store full text.
# Distances in (threshold, near_threshold] still persist for provenance.
TIGHT_NEAR_STORE_THRESHOLD = 12


class DedupDecision(str, Enum):
    NEW = "new"
    REVISION = "revision"
    EXACT_DUP = "exact_dup"
    NEAR_DUP = "near_dup"


@dataclass(slots=True)
class DedupOutcome:
    decision: DedupDecision
    dup_group: str
    reason: str
    # Hamming distance for NEAR_DUP decisions (simhash128); None otherwise.
    hamming: int | None = None

    @property
    def is_unique(self) -> bool:
        return self.decision == DedupDecision.NEW


class DedupEngine:
    def __init__(self, state: StateDB, near_threshold: int = 24) -> None:
        # 128-bit signatures: unrelated documents sit at Hamming ~45+, so a
        # Hamming≤24 default folds tight-to-moderate near-dups at perfect
        # precision (no false-merges) while leaving headroom to the unrelated
        # floor. Raising it catches more (recall) until precision erodes near
        # ~36 (see benchmarks/); the value is fully tunable per caller.
        self._state = state
        self._near_threshold = max(0, near_threshold)

    def evaluate(self, cap: DocCapture) -> DedupOutcome:
        """Decide dedup state for ``cap`` and update its ``parent_doc_or_dup_group``."""
        # Step 1: register/observe the content_hash.
        canonical_doc_id, was_new = self._state.upsert_dedup(cap.content_hash, cap.doc_id)

        if not was_new:
            cap.parent_doc_or_dup_group = canonical_doc_id
            if canonical_doc_id == cap.doc_id:
                # Same URL+content already seen; this is a fresh capture (different fetch_ts).
                return DedupOutcome(
                    decision=DedupDecision.REVISION,
                    dup_group=canonical_doc_id,
                    reason="same_url_content_recaptured",
                )
            return DedupOutcome(
                decision=DedupDecision.EXACT_DUP,
                dup_group=canonical_doc_id,
                reason="content_hash_match",
            )

        # Step 2: near-duplicate scan via 128-bit simhash band buckets. The
        # detection signature is computed from the document text (the durable
        # ``near_dup_hash`` stays a 64-bit provenance fingerprint).
        sig = simhash128(cap.text) if cap.text else 0
        if sig > 0:
            best_doc: str | None = None
            best_dist: int = 129
            for other_doc_id, other_sig in self._state.find_near_dup_candidates(sig):
                if other_doc_id == cap.doc_id:
                    continue
                dist = hamming128(sig, other_sig)
                if dist < best_dist:
                    best_dist = dist
                    best_doc = other_doc_id
            if best_doc is not None and best_dist <= self._near_threshold:
                cap.parent_doc_or_dup_group = best_doc
                self._state.add_near_dup_index(cap.doc_id, sig)
                return DedupOutcome(
                    decision=DedupDecision.NEAR_DUP,
                    dup_group=best_doc,
                    reason=f"simhash128_hamming={best_dist}",
                    hamming=best_dist,
                )

        # Step 3: brand new canonical doc.
        if sig > 0:
            self._state.add_near_dup_index(cap.doc_id, sig)
        cap.parent_doc_or_dup_group = cap.doc_id
        return DedupOutcome(decision=DedupDecision.NEW, dup_group=cap.doc_id, reason="new_content")
