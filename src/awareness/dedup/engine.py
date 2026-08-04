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
- W19 content-diversity guard: a band candidate only merges when its stored
  token-set sketch agrees with the new doc's within bounds (see
  :data:`NEAR_DUP_MAX_TOKEN_COUNT_RATIO` / :data:`NEAR_DUP_SHORT_DOC_MAX_TOKENS`),
  so distinct articles sharing template/boilerplate text cannot collapse
  into one dup group.
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

import xxhash

from awareness.obs.logging import get_logger
from awareness.schemas.doc import DocCapture
from awareness.storage.state import StateDB
from awareness.util.hashing import hamming64, hamming128, normalize_for_hash, simhash128

logger = get_logger("dedup")

# Default near-duplicate merge threshold in Hamming bits over the 128-bit
# signature. With the 32x8 band layout (16 real data bands of 8 bits) the
# exact pigeonhole guarantee is Hamming <= 15 and retrieval beyond is
# probabilistic, yet the W7 benchmark measured the H<=32 default at F1 0.961
# with P 1.0 — band sharing at 8-bit width still surfaces distance-32 pairs
# and the per-band candidate limit binds before the banding width. Raised
# from 24 (F1 0.845) per the benchmark; see
# tests/unit/test_dedup_invariant.py and tests/unit/test_near_threshold_32.py.
DEFAULT_NEAR_THRESHOLD = 32

# Upper bound of the engine clamp. Must cover DEFAULT_NEAR_THRESHOLD (32).
# Callers may tune past the banding's probabilistic retrieval range; the cap
# prevents a misconfigured threshold from merging the whole corpus (precision
# erodes past ~36 — unrelated 128-bit simhashes sit at Hamming ~45+).
NEAR_CLAMP_MAX = 40

# NEAR_DUP captures at or below this Hamming distance are treated like
# EXACT_DUP for storage: count as a dedup drop, do not re-store full text.
# Distances in (threshold, near_threshold] still persist for provenance.
# Worker-facing effective tight-store cutoff is min(TIGHT_NEAR_STORE_THRESHOLD,
# near_threshold) — see DedupEngine.tight_store_threshold (L-05).
TIGHT_NEAR_STORE_THRESHOLD = 12

# Low 64-bit mask used to compare legacy 64-bit candidate signatures (M-23).
_MASK64 = (1 << 64) - 1

# W19 boilerplate foot-gun: the simhash near-dup band lookup is a *retrieval*
# step, but merging on Hamming ≤ DEFAULT_NEAR_THRESHOLD alone lets DISTINCT
# articles that share template/boilerplate text (a repeated filler sentence)
# collapse into ONE parent_doc_or_dup_group → search collapse returns 1 row
# instead of N, and export dedupe folds them. Reproduced with 4 distinct
# climate docs sharing a footer: Hamming 15-24, all merged into one cluster.
#
# The content-diversity guard gates the merge using the token-set sketch
# (``token_hash`` = xxh3_64 of the sorted unique tokens, ``token_count`` =
# unique-token count) stored on the candidate's dedup_near rows:
#   * both sketches known:
#       - |count_a - count_b| / max(count_a, count_b) must be ≤
#         NEAR_DUP_MAX_TOKEN_COUNT_RATIO (true near-dups of one article have
#         near-identical vocabularies; boilerplate-only overlaps do not);
#       - when BOTH docs are 'short' (≤ NEAR_DUP_SHORT_DOC_MAX_TOKENS unique
#         tokens) the token_hash must match exactly — boilerplate dominates
#         short docs, so a genuine near-dup of a short doc is a near-identical
#         copy, while distinct short articles sharing a filler sentence have
#         different token sets by construction.
#   * legacy rows (NULL sketch) → 'unknown' → merge by Hamming only (old
#     behavior).
# Long docs skip the exact-match requirement: boilerplate is a small fraction
# of a large article, so Hamming ≤ threshold + the count-ratio bound is
# sufficient there (rephrased long articles still merge).
NEAR_DUP_MAX_TOKEN_COUNT_RATIO = 0.5
NEAR_DUP_SHORT_DOC_MAX_TOKENS = 200


def token_set_fingerprint(text: str | None) -> tuple[int | None, int | None]:
    """Return the W19 token-set sketch ``(token_hash, token_count)`` for *text*.

    ``token_hash`` is xxh3_64 (converted to a signed 64-bit int for the BIGINT
    columns) over the sorted unique tokens of the normalized text; NULL when
    the text tokenizes to nothing. Used by the content-diversity guard at
    index write time and compared against the candidate's stored sketch at
    merge time.
    """
    tokens = normalize_for_hash(text or "").split()
    if not tokens:
        return None, None
    uniq = sorted(set(tokens))
    raw = xxhash.xxh3_64_intdigest(" ".join(uniq))
    if raw >= (1 << 63):
        raw -= 1 << 64
    return raw, len(uniq)


def _content_guard_allows_merge(
    cap_tokens: tuple[int | None, int | None],
    cand_token_hash: int | None,
    cand_token_count: int | None,
) -> bool:
    """W19 content-diversity gate: may ``cap`` merge into ``candidate``'s cluster?"""
    if cand_token_hash is None or cand_token_count is None:
        return True  # legacy/unknown sketch → old behavior
    cap_hash, cap_count = cap_tokens
    if cap_hash is None or cap_count is None:
        return True  # new doc has no token set — nothing to compare
    ratio = abs(cap_count - cand_token_count) / max(cap_count, cand_token_count)
    if ratio > NEAR_DUP_MAX_TOKEN_COUNT_RATIO:
        return False
    if cap_count <= NEAR_DUP_SHORT_DOC_MAX_TOKENS and cand_token_count <= NEAR_DUP_SHORT_DOC_MAX_TOKENS:
        return cap_hash == cand_token_hash
    return True


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
        # M-19: clamp into [0, NEAR_CLAMP_MAX] — the 32x8 band layout retrieves
        # candidates by shared 8-bit band; the exact pigeonhole guarantee is
        # Hamming <= (128 // 8) - 1 = 15 and retrieval is probabilistic to ~31,
        # but the W7 benchmark validated H<=32 (the default) empirically. The
        # clamp still bounds pathological caller values so a misconfigured
        # threshold can never collapse the whole corpus into one group.
        self._near_threshold = max(0, min(int(near_threshold), NEAR_CLAMP_MAX))

    @property
    def near_threshold(self) -> int:
        """Effective near-dup merge threshold (clamped into [0, NEAR_CLAMP_MAX])."""
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
        cap_tokens = token_set_fingerprint(cap.text)
        cap_token_hash, cap_token_count = cap_tokens
        if sig > 0:
            best_doc: str | None = None
            best_dist: int = 129
            best_cand_sketch: tuple[int | None, int | None] = (None, None)
            for other_doc_id, other_sig, cand_token_hash, cand_token_count in (
                self._state.find_near_dup_candidate_rows(sig)
            ):
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
                    best_cand_sketch = (cand_token_hash, cand_token_count)
            if (
                best_doc is not None
                and best_dist <= self._near_threshold
                and _content_guard_allows_merge(cap_tokens, *best_cand_sketch)
            ):
                root = self._state.uf_union(cap.doc_id, best_doc)
                cap.parent_doc_or_dup_group = root
                self._state.add_near_dup_index(
                    cap.doc_id, sig, token_hash=cap_token_hash, token_count=cap_token_count
                )
                return DedupOutcome(
                    decision=DedupDecision.NEAR_DUP,
                    dup_group=root,
                    reason=f"simhash128_hamming={best_dist}",
                    hamming=best_dist,
                )

        # Step 3: brand new canonical doc.
        if sig > 0:
            self._state.add_near_dup_index(
                cap.doc_id, sig, token_hash=cap_token_hash, token_count=cap_token_count
            )
        root = self._state.uf_union(cap.doc_id, cap.doc_id)
        cap.parent_doc_or_dup_group = root
        return DedupOutcome(decision=DedupDecision.NEW, dup_group=root, reason="new_content")
