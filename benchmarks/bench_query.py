"""Suite: query layer — BM25 full-text latency, range scan, index build.

Awareness queries the corpus through DuckDB: a ``captures`` view over the
JSONL staging files, with the FTS extension providing BM25-ranked search
(``awareness.storage.duckdb_index.DuckDbIndex``). This suite measures the
real production path and contrasts it with the obvious baselines:

* **BM25 search latency** (ms, p50) — DuckDB FTS vs SQLite FTS5 (stdlib) vs
  a naive Python substring scan. Lower is better.
* **Range-scan count latency** (ms) — a windowed ``COUNT(*)`` over the
  corpus, the bread-and-butter ``awareness counts`` query.
* **FTS index build time** (s) — cold build over the full corpus.

References: DuckDB FTS ≈ 0.3–0.5 s ranking ~3.8 M rows; SQLite FTS5 single-
digit ms on millions of rows. We report our own same-machine numbers.
"""

from __future__ import annotations

import sqlite3
import statistics
import tempfile
import time
from pathlib import Path

from awareness.storage.duckdb_index import DuckDbIndex
from awareness.storage.jsonl import JsonlStagingWriter

from .corpus import make_documents
from .harness import Entry, Suite

_QUERIES = [
    "infrastructure investment", "coastal sediment", "streaming parser",
    "quarterly figures volatility", "shared secret protocol", "planting season",
    "cache invalidation version", "migratory birds", "observability stack",
    "connection pool timeout", "reservoir record lows", "final movement",
]


def _build_corpus_jsonl(root: Path, n: int) -> list[dict]:
    docs = make_documents(n, seed=2025)
    writer = JsonlStagingWriter(root, max_records_per_file=5_000)
    rows: list[dict] = []
    for i, text in enumerate(docs):
        ts = f"2024-01-{(i % 28) + 1:02d}T00:00:00+00:00"
        # Full column set the DuckDB ``captures`` view projects (nulls allowed).
        row = {
            "doc_id": f"doc{i}", "capture_id": f"cap{i}",
            "parent_doc_or_dup_group": f"doc{i}",
            "source_type": "local_fixture", "source_name": "bench",
            "source_locator": "bench", "source_shard": None,
            "source_offset_or_record_id": None, "discovery_channel": "bench",
            "job_id": "benchjob", "batch_id": "benchbatch", "ingest_version": "0.0",
            "url": f"https://bench.test/{i}", "canonical_url": f"https://bench.test/{i}",
            "domain": "bench.test",
            "fetch_ts": ts, "observed_ts": ts, "published_ts": None, "last_modified": None,
            "content_type": "text/plain", "http_status": 200, "etag": None,
            "title": text[:60], "text": text, "language": "en",
            "content_hash": f"h{i}", "near_dup_hash": i,
            "robots_decision": "not_applicable", "terms_note_if_relevant": None,
        }
        writer.write([row])
        rows.append(row)
    writer.flush()
    writer.close()
    return rows


def _p50_ms(fn, queries, repeats: int = 3) -> float:
    samples: list[float] = []
    for _ in range(repeats):
        for q in queries:
            t0 = time.perf_counter()
            fn(q)
            samples.append((time.perf_counter() - t0) * 1000)
    return statistics.median(samples)


def run(n_docs: int = 20_000) -> list[Suite]:
    suites: list[Suite] = []
    tmp = Path(tempfile.mkdtemp(prefix="aw_bench_query_"))
    rows = _build_corpus_jsonl(tmp, n_docs)

    # ── Awareness DuckDB index: build time + warm it ─────────────────────
    idx = DuckDbIndex(db_path=tmp / "index.duckdb", jsonl_dir=tmp, iceberg_warehouse=None)
    idx.connect()
    t0 = time.perf_counter()
    # First FTS-backed search triggers the index build.
    idx.search("infrastructure", mode="fts", limit=10)
    build_secs = time.perf_counter() - t0

    build = Suite(
        key="query_index_build",
        title="FTS index build time",
        metric="Build time (s, lower=better)",
        higher_is_better=False,
        subtitle=f"cold BM25 index over {n_docs:,} documents",
    )
    build.add(Entry("DuckDB FTS (Awareness)", build_secs, "s", is_awareness=True))

    # ── search-path optimization: cached views vs refresh-per-query ──────
    # The headline win here is our own before/after: search() used to rebuild
    # all DuckDB views on every call. We now refresh only when the source
    # files change, so steady-state queries are bound by the query itself.
    opt = Suite(
        key="query_search_optimization",
        title="BM25 search latency — view-cache optimization",
        metric="Latency p50 (ms, lower=better)",
        higher_is_better=False,
        subtitle=f"DuckDB FTS BM25 over {n_docs:,} docs; before vs after caching the view refresh",
    )

    def _search_forced_refresh(q: str):
        # Simulate the pre-optimization path: refresh views on every call.
        idx._refresh_views(idx._conn)
        idx._views_signature = idx._source_signature()
        idx._fts_built_signature = None
        return idx.search(q, mode="fts", limit=10)

    before_ms = _p50_ms(_search_forced_refresh, _QUERIES, repeats=2)
    after_ms = _p50_ms(lambda q: idx.search(q, mode="fts", limit=10), _QUERIES)
    opt.add(Entry("DuckDB FTS — cached views (Awareness)", after_ms, "ms", is_awareness=True,
                  note="refresh only when corpus changes"))
    opt.add(Entry("DuckDB FTS — refresh per query (before)", before_ms, "ms",
                  note="pre-optimization baseline"))

    # ── peer context: how the BM25 path compares to other lexical search ─
    search = Suite(
        key="query_search_latency",
        title="Keyword search latency vs peers",
        metric="Latency p50 (ms, lower=better)",
        higher_is_better=False,
        subtitle=f"ranked/unranked lexical search over {n_docs:,} docs (log scale)",
    )
    search.add(Entry("DuckDB FTS BM25 (Awareness)", after_ms, "ms", is_awareness=True,
                     note="BM25-ranked, shares the analytical SQL surface"))
    sq_ms = _sqlite_fts5_latency(rows, _QUERIES)
    if sq_ms is not None:
        search.add(Entry("SQLite FTS5", sq_ms, "ms", note="dedicated inverted index"))
    texts = [(r["title"] or "") + " " + (r["text"] or "") for r in rows]
    lowered = [t.lower() for t in texts]

    def naive(q: str):
        ql = q.lower().split()
        return [i for i, t in enumerate(lowered) if all(w in t for w in ql)][:10]

    nv_ms = _p50_ms(naive, _QUERIES)
    search.add(Entry("naive Python substring scan", nv_ms, "ms", note="unranked, no index"))

    # ── range-scan count latency ─────────────────────────────────────────
    rng = Suite(
        key="query_range_scan",
        title="Range-scan count latency",
        metric="Latency p50 (ms, lower=better)",
        higher_is_better=False,
        subtitle=f"windowed COUNT(*) over {n_docs:,} docs",
    )
    win = ["2024-01-05T00:00:00+00:00", "2024-01-20T00:00:00+00:00"]

    def range_count(_q):
        return idx.execute(
            "SELECT count(*) AS n FROM captures WHERE fetch_ts BETWEEN $a AND $b",
            {"a": win[0], "b": win[1]},
        )

    rc_ms = _p50_ms(range_count, ["x"], repeats=5)
    rng.add(Entry("DuckDB range scan (Awareness)", rc_ms, "ms", is_awareness=True))

    idx.close()
    suites.extend([opt, search, build, rng])
    return suites


def _sqlite_fts5_latency(rows: list[dict], queries: list[str]) -> float | None:
    try:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE VIRTUAL TABLE c USING fts5(title, text)")
        con.executemany("INSERT INTO c(title, text) VALUES (?, ?)",
                        [(r["title"], r["text"]) for r in rows])
        con.commit()
    except sqlite3.OperationalError:
        return None  # FTS5 not compiled in

    def fn(q: str):
        match = " ".join(q.split())
        con.execute("SELECT rowid FROM c WHERE c MATCH ? ORDER BY bm25(c) LIMIT 10", [match]).fetchall()

    ms = _p50_ms(fn, queries)
    con.close()
    return ms


if __name__ == "__main__":
    for s in run(n_docs=15_000):
        print(f"\n== {s.title} — {s.metric} ==  ({s.subtitle})")
        rev = s.higher_is_better
        for e in sorted(s.entries, key=lambda x: -x.value if rev else x.value):
            tag = " *" if e.is_awareness else "  "
            print(f"{tag} {e.name:32s} {e.value:10.3f} {e.unit}")
