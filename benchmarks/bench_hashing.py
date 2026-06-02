"""Suite: content-fingerprint hashing throughput (MB/s).

Awareness fingerprints every document for exact-dedup with
``xxhash.xxh3_64`` (see ``awareness.util.hashing.content_hash``). This suite
answers two questions honestly:

1. **Raw digest throughput** — is xxh3 actually the fastest choice vs the
   usual alternatives (BLAKE3, SHA-256, MD5, BLAKE2b, MurmurHash3)? This is
   the standard non-crypto-hash benchmark (SMHasher-style, reported MB/s).
2. **Full ``content_hash`` throughput** — the real per-document cost
   Awareness pays (NFKC normalize + punctuation strip + xxh3).

Higher MB/s is better.
"""

from __future__ import annotations

import hashlib

import mmh3
import xxhash

from awareness.util.hashing import content_hash, normalize_for_hash

from .corpus import make_documents
from .harness import Entry, Suite, throughput, time_callable


def _total_bytes(docs: list[str]) -> int:
    return sum(len(d.encode("utf-8")) for d in docs)


def run(repeats: int = 5) -> list[Suite]:
    docs = make_documents(8_000, seed=1234)
    blobs = [d.encode("utf-8") for d in docs]
    mb = _total_bytes(docs) / (1024 * 1024)

    # ── 1) raw digest throughput on the same per-doc byte blobs ───────────
    raw = Suite(
        key="hash_throughput",
        title="Content-fingerprint hashing",
        metric="Throughput (MB/s)",
        higher_is_better=True,
        subtitle=f"per-document digest over {len(docs):,} docs ({mb:.0f} MB total)",
    )

    def bench(fn) -> float:
        secs = time_callable(lambda: [fn(b) for b in blobs], repeats=repeats)
        return throughput(mb, secs)

    competitors: list[tuple[str, object, bool, str]] = [
        ("xxh3_64 (Awareness)", lambda b: xxhash.xxh3_64_hexdigest(b), True, "the digest Awareness uses"),
        ("BLAKE3", _blake3_or_none(), False, ""),
        ("MurmurHash3", lambda b: mmh3.hash64(b), False, ""),
        ("BLAKE2b", lambda b: hashlib.blake2b(b).hexdigest(), False, ""),
        ("MD5", lambda b: hashlib.md5(b).hexdigest(), False, ""),
        ("SHA-256", lambda b: hashlib.sha256(b).hexdigest(), False, ""),
    ]
    for name, fn, is_aw, note in competitors:
        if fn is None:
            continue
        raw.add(Entry(name=name, value=bench(fn), unit="MB/s", is_awareness=is_aw, note=note))

    # ── 2) full content_hash (normalize + xxh3) — the real per-doc cost ───
    full = Suite(
        key="content_hash_pipeline",
        title="Awareness content_hash pipeline",
        metric="Throughput (MB/s)",
        higher_is_better=True,
        subtitle="NFKC-normalize + punctuation-fold + xxh3, vs digest-only baselines",
    )
    secs = time_callable(lambda: [content_hash(d) for d in docs], repeats=repeats)
    full.add(
        Entry(
            name="content_hash (normalize+xxh3)",
            value=throughput(mb, secs),
            unit="MB/s",
            is_awareness=True,
            note="full Awareness fingerprint path",
        )
    )
    # baseline: normalize cost alone
    secs_norm = time_callable(lambda: [normalize_for_hash(d) for d in docs], repeats=repeats)
    full.add(
        Entry(name="normalize_for_hash only", value=throughput(mb, secs_norm), unit="MB/s", note="normalization step")
    )
    secs_raw = time_callable(lambda: [xxhash.xxh3_64_hexdigest(b) for b in blobs], repeats=repeats)
    full.add(Entry(name="xxh3 digest only", value=throughput(mb, secs_raw), unit="MB/s", note="digest step"))

    return [raw, full]


def _blake3_or_none():
    try:
        import blake3

        return lambda b: blake3.blake3(b).hexdigest()
    except Exception:
        return None


if __name__ == "__main__":
    for s in run():
        print(f"\n== {s.title} — {s.metric} ==")
        for e in sorted(s.entries, key=lambda x: -x.value):
            tag = " *" if e.is_awareness else "  "
            print(f"{tag} {e.name:32s} {e.value:10.1f} {e.unit}")
