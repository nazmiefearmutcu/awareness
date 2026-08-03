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
from awareness.storage.state import NEAR_DUP_SEGMENTS, StateDB
from awareness.util.hashing import hamming64, hamming128, simhash128

logger = get_logger("dedup")

# Default near-duplicate merge threshold in Hamming bits over the 128-bit
# signature. Must stay <= (NEAR_DUP_SEGMENTS - 1) so the band index's pigeonhole
# guarantee covers it — see tests/unit/test_dedup_invariant.py.
DEFAULT_NEAR_THRESHOLD = 24

# NEAR_DUP captures at or below this Hamming distance are treated like
# EXACT_DUP for storage: count as a dedup drop, do not re-store full text.
# Distances in (threshold, near_threshold] still persist for provenance.
# Worker-facing effective tight-store cutoff is min(TIGHT_NEAR_STORE_THRESHOLD,
# near_threshold) — see DedupEngine.tight_store_threshold (L-05).
TIGHT_NEAR_STORE_THRESHOLD = 12

# Low 64-bit mask used to compare legacy 64-bit candidate signatures (M-23).
_MASK64 = (1 << 64) - 1


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
    def __init__(self, state: StateDB, near_threshold: int = DEFAULT_NEAR_THRESHOLD) -> None:
        # 128-bit signatures: unrelated documents sit at Hamming ~45+, so a
        # Hamming≤24 default folds tight-to-moderate near-dups at perfect
        # precision (no false-merges) while leaving headroom to the unrelated
        # floor. Raising it catches more (recall) until precision erodes near
        # ~36 (see benchmarks/); the value is fully tunable per caller.
        self._state = state
        # M-19: clamp into [0, NEAR_DUP_SEGMENTS - 1] — the band index only
        # guarantees retrieval up to Hamming ≤ (segments - 1); an unclamped
        # threshold (>31) would silently miss every pair in the gap.
        self._near_threshold = max(0, min(int(near_threshold), NEAR_DUP_SEGMENTS - 1))

    @property
    def near_threshold(self) -> int:
        """Effective near-dup merge threshold (clamped into banding range)."""
        return self._near_threshold

    @property
    def tight_store_threshold(self) -> int:
        """Worker-facing tight-store cutoff (L-05).

        A tight NEAR_DUP is one at Hamming ≤ this value. It can never exceed
        the engine's merge threshold: with near_threshold < 12 the tight store
        cutoff collapses to the threshold itself.
        """
        return min(TIGHT_NEAR_STORE_THRESHOLD, self._near_threshold)

    def evaluate(self, cap: DocCapture) -> DedupOutcome:
        """Decide dedup state for ``cap`` and update its ``parent_doc_or_dup_group``."""
        # Step 1: register/observe the content_hash.
        canonical_doc_id, was_new = self._state.upsert_dedup(cap.content_hash, cap.doc_id)

        if not was_new:
            # H-23: EXACT_DUP/REVISION must fold to the *union-find root*, not
            # the raw first-seen doc_id — same as the NEAR_DUP path below — so
            # downstream folding (search collapse, related()) stays consistent
            # when the canonical doc later joins a near-dup cluster.
            cap.parent_doc_or_dup_group = self._state.uf_find(canonical_doc_id)
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
                # M-23: legacy 64-bit candidates (sig_hex NULL rows) carry only
                # the low 64 bits — compare them with hamming64 against the
                # query's low half, not hamming128 (which would count every set
                # bit in the query's high half as a mismatch).
                if other_sig.bit_length() <= 64:
                    dist = hamming64(sig & _MASK64, other_sig)
                else:
                    dist = hamming128(sig, other_sig)
                if dist < best_dist:
                    best_dist = dist
                    best_doc = other_doc_id
            if best_doc is not None and best_dist <= self._near_threshold:
                root = self._state.uf_union(cap.doc_id, best_doc)
                cap.parent_doc_or_dup_group = root
                self._state.add_near_dup_index(cap.doc_id, sig)
                return DedupOutcome(
                    decision=DedupDecision.NEAR_DUP,
                    dup_group=root,
                    reason=f"simhash128_hamming={best_dist}",
                    hamming=best_dist,
                )

        # Step 3: brand new canonical doc.
        if sig > 0:
            self._state.add_near_dup_index(cap.doc_id, sig)
        root = self._state.uf_union(cap.doc_id, cap.doc_id)
        cap.parent_doc_or_dup_group = root
        return DedupOutcome(decision=DedupDecision.NEW, dup_group=root, reason="new_content")
