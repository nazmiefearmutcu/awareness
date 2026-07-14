"""Content hashing + simhash for near-duplicate detection.

We use:
- xxhash64 for fast exact-content hash (hex digest, 16 chars).
- A token-level Charikar simhash over 3-shingles of normalized lowercase
  text. Two widths are provided:
    * :func:`simhash64`  — 64-bit, kept for backward compatibility and as the
      durable provenance fingerprint stored in ``DocCapture.near_dup_hash``.
    * :func:`simhash128` — 128-bit, frequency-weighted. This is the
      near-duplicate *detection* signature: doubling the bit budget and
      down-weighting boilerplate shingles lifts pairwise near-dup F1 from
      ~0.86 (64-bit) to ~0.99, ahead of a 128-permutation MinHash at a 64x
      smaller footprint (see ``benchmarks/``).

Both share the same shingles and the same +1/-1 sign rule; the bit
accumulation is vectorized with NumPy so a wider fingerprint costs no extra
Python-level work per shingle.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Callable

import mmh3
import numpy as np
import xxhash

_WS_RE = re.compile(r"\s+", re.UNICODE)
_NON_ALNUM = re.compile(r"[^0-9a-z\s]+", re.UNICODE)
_MASK64 = 0xFFFFFFFFFFFFFFFF
_MASK128 = (1 << 128) - 1
_ARANGE64 = np.arange(64, dtype=np.uint64)


def normalize_for_hash(text: str) -> str:
    """Lowercase + strip punctuation + collapse whitespace + NFKC."""
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text).lower()
    s = _NON_ALNUM.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def content_hash(text: str) -> str:
    """Stable 64-bit xxhash of the normalized text, hex-encoded."""
    return xxhash.xxh3_64_hexdigest(normalize_for_hash(text))


def _shingles(tokens: list[str], k: int = 3) -> list[str]:
    if len(tokens) < k:
        return [" ".join(tokens)] if tokens else []
    return [" ".join(tokens[i : i + k]) for i in range(len(tokens) - k + 1)]


def _grams_for(text: str, k: int) -> list[str]:
    normalized = normalize_for_hash(text)
    if not normalized:
        return []
    return _shingles(normalized.split(" "), k=k)


def _bits_to_int(out_bits: np.ndarray) -> int:
    """Combine a boolean bit array (LSB first) into a Python int."""
    out = 0
    for i in np.nonzero(out_bits)[0]:
        out |= 1 << int(i)
    return out


def simhash64(text: str, k: int = 3) -> int:
    """Compute a 64-bit Charikar simhash (signed int).

    Vectorized with NumPy but **bit-identical** to the original per-shingle
    accumulator: each shingle contributes +1/-1 per bit via mmh3's unsigned
    64-bit hash, and bit *i* is set when its signed sum is >= 0.
    Values are returned as a signed 64-bit integer in [-2^63, 2^63 - 1].
    """
    grams = _grams_for(text, k)
    if not grams:
        return 0
    hv = np.fromiter(
        (mmh3.hash64(g.encode("utf-8"), signed=False)[0] for g in grams),
        dtype=np.uint64,
        count=len(grams),
    )
    bits = ((hv[:, None] >> _ARANGE64) & np.uint64(1)).astype(np.int64)  # (n, 64)
    sums = (bits * 2 - 1).sum(axis=0)  # +1 if bit set, -1 otherwise
    val = _bits_to_int(sums >= 0)
    if val >= (1 << 63):
        val -= (1 << 64)
    return val


def simhash128(
    text: str, k: int = 3, *, weighted: bool = True, idf: Callable[[str], float] | None = None
) -> int:
    """Compute a 128-bit frequency-weighted Charikar simhash (unsigned int).

    The detection-grade fingerprint. mmh3's 128-bit hash supplies both 64-bit
    halves per shingle; when ``weighted`` each distinct shingle is scaled by
    ``1 + ln(1 + count)`` so a handful of boilerplate shingles cannot dominate
    the signature. When ``idf`` is given, each shingle's weight is additionally
    scaled by ``idf(shingle)`` so corpus-common boilerplate can be down-weighted.
    Returns 0 for empty input.
    """
    grams = _grams_for(text, k)
    if not grams:
        return 0
    if weighted:
        counts = Counter(grams)
        uniq = list(counts.keys())
        weights = np.fromiter(
            (
                (1.0 + np.log1p(counts[g])) * (idf(g) if idf is not None else 1.0)
                for g in uniq
            ),
            dtype=np.float64,
            count=len(uniq),
        )
    else:
        uniq = grams
        weights = None

    pairs = [mmh3.hash64(g.encode("utf-8"), signed=False) for g in uniq]
    lo = np.fromiter((p[0] & _MASK64 for p in pairs), dtype=np.uint64, count=len(pairs))
    hi = np.fromiter((p[1] & _MASK64 for p in pairs), dtype=np.uint64, count=len(pairs))
    bits_lo = ((lo[:, None] >> _ARANGE64) & np.uint64(1)).astype(np.float64)
    bits_hi = ((hi[:, None] >> _ARANGE64) & np.uint64(1)).astype(np.float64)
    signed = np.concatenate([bits_lo, bits_hi], axis=1) * 2.0 - 1.0  # (n, 128)
    if weights is not None:
        signed *= weights[:, None]
    sums = signed.sum(axis=0)
    return _bits_to_int(sums >= 0) & _MASK128


def hamming64(a: int, b: int) -> int:
    """Hamming distance between two 64-bit ints."""
    return ((a ^ b) & _MASK64).bit_count()


def hamming128(a: int, b: int) -> int:
    """Hamming distance between two 128-bit ints."""
    return ((a ^ b) & _MASK128).bit_count()


def near_duplicate(a: int, b: int, threshold: int = 3) -> bool:
    """True if 64-bit simhash Hamming distance is at most ``threshold`` bits."""
    return hamming64(a, b) <= threshold


def sig128_to_hex(sig: int) -> str:
    """Encode a 128-bit signature as a fixed-width 32-char hex string."""
    return f"{sig & _MASK128:032x}"


def sig128_from_hex(hexstr: str) -> int:
    """Decode a 32-char hex signature back to an int (0 on bad input)."""
    try:
        return int(hexstr, 16) & _MASK128
    except (ValueError, TypeError):
        return 0


# A stable doc_id derived from (canonical_url || content_hash).
def doc_id_for(canonical_url: str | None, content_hash_hex: str) -> str:
    """Deterministic doc_id. xxhash3_128 of url+content for stable identity."""
    key = (canonical_url or "") + "::" + content_hash_hex
    return xxhash.xxh3_128_hexdigest(key)


def capture_id_for(doc_id: str, observed_ts_iso: str, source_locator: str | None) -> str:
    """Per-capture unique id."""
    key = f"{doc_id}|{observed_ts_iso}|{source_locator or ''}"
    return xxhash.xxh3_128_hexdigest(key)
