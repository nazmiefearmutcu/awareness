"""Suite: end-to-end ingestion throughput (single core).

The real Awareness hot loop per document is: ``normalize_text`` → fingerprint
(``content_hash`` + ``simhash64``) → JSONL stage write. This suite measures
that loop in documents/second on one core, and isolates the impact of the
SimHash optimization by running the identical loop with the old naive
SimHash vs the vectorized one.

Single-core reference points (cited, different pipelines — context only):
CCNet ≈ 600 docs/s for its dedup-hash pass and ≈ 40 docs/s for its full
processing pass (which additionally runs langID + SentencePiece + a KenLM
perplexity model Awareness does not). warcio reads ≈ 13,700 WARC records/s.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from awareness.normalize.text import normalize_text
from awareness.storage.jsonl import JsonlStagingWriter
from awareness.util.hashing import content_hash, simhash64

from .bench_simhash import _naive_simhash64
from .corpus import make_documents
from .harness import Entry, Suite, throughput, time_callable


def _run_loop(docs: list[str], root: Path, *, simhash_fn) -> None:
    writer = JsonlStagingWriter(root, max_records_per_file=10_000)
    for i, text in enumerate(docs):
        nt = normalize_text(text, min_chars=1)
        ch = content_hash(nt.text)
        sh = simhash_fn(nt.text)
        writer.write(
            [{
                "doc_id": f"d{i}", "capture_id": f"c{i}",
                "source_type": "local_fixture", "source_name": "bench",
                "fetch_ts": "2024-01-01T00:00:00+00:00",
                "url": f"https://b.test/{i}", "domain": "b.test",
                "title": nt.title, "text": nt.text, "language": "en",
                "content_hash": ch, "near_dup_hash": sh,
                "parent_doc_or_dup_group": f"d{i}",
            }]
        )
    writer.flush()
    writer.close()


def run(repeats: int = 3) -> list[Suite]:
    docs = make_documents(6_000, seed=555)

    # ── full ingest loop: optimized vs naive simhash ─────────────────────
    loop = Suite(
        key="ingestion_loop",
        title="End-to-end ingestion throughput",
        metric="Throughput (docs/s)",
        higher_is_better=True,
        subtitle=f"normalize → content_hash + simhash → JSONL write, {len(docs):,} docs, single core",
    )

    def timed(simhash_fn) -> float:
        tmp = Path(tempfile.mkdtemp(prefix="aw_bench_ingest_"))
        secs = time_callable(lambda: _run_loop(docs, tmp, simhash_fn=simhash_fn), repeats=repeats, warmup=1)
        return throughput(len(docs), secs)

    loop.add(Entry("Awareness loop (vectorized simhash)", timed(simhash64), "docs/s", is_awareness=True))
    loop.add(Entry("Awareness loop (naive simhash)", timed(_naive_simhash64), "docs/s",
                   note="pre-optimization baseline"))

    # ── fingerprint stage in isolation (the optimization target) ─────────
    fp = Suite(
        key="ingestion_fingerprint",
        title="Fingerprint stage throughput",
        metric="Throughput (docs/s)",
        higher_is_better=True,
        subtitle=f"content_hash + simhash per document, {len(docs):,} docs",
    )
    s_opt = time_callable(lambda: [(content_hash(d), simhash64(d)) for d in docs], repeats=repeats)
    fp.add(Entry("content_hash + simhash (vectorized)", throughput(len(docs), s_opt), "docs/s", is_awareness=True))
    s_naive = time_callable(lambda: [(content_hash(d), _naive_simhash64(d)) for d in docs], repeats=max(1, repeats - 1))
    fp.add(Entry("content_hash + simhash (naive)", throughput(len(docs), s_naive), "docs/s",
                 note="pre-optimization baseline"))

    return [loop, fp]


if __name__ == "__main__":
    for s in run():
        print(f"\n== {s.title} — {s.metric} ==  ({s.subtitle})")
        for e in sorted(s.entries, key=lambda x: -x.value):
            tag = " *" if e.is_awareness else "  "
            print(f"{tag} {e.name:38s} {e.value:10.1f} {e.unit} {e.note}")
