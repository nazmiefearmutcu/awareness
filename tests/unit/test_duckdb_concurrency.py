from __future__ import annotations

import json
import threading
from pathlib import Path

from awareness.storage.duckdb_index import DuckDbIndex

_FULL_KEYS = (
    "doc_id", "capture_id", "parent_doc_or_dup_group", "source_type",
    "source_name", "source_locator", "source_shard",
    "source_offset_or_record_id", "discovery_channel", "job_id", "batch_id",
    "ingest_version", "url", "canonical_url", "domain", "fetch_ts",
    "observed_ts", "published_ts", "last_modified", "content_type",
    "http_status", "etag", "title", "text", "language", "content_hash",
    "near_dup_hash", "robots_decision", "terms_note_if_relevant",
)


def _write_doc(root: Path, idx: int, *, title: str, text: str, domain: str = "example.com") -> None:
    day = root / "captures" / "2026" / "06" / "01"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}",
        capture_id=f"cap-{idx}",
        source_type="rss",
        domain=domain,
        url=f"https://{domain}/{idx}",
        fetch_ts="2026-06-01T12:00:00+00:00",
        title=title,
        text=text,
    )
    (day / f"chunk-{idx}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def test_duckdb_multi_thread_concurrency(tmp_path: Path) -> None:
    """Shared singleton + long-lived conn must stay safe under threadpool load.

    Matches the API model: one DuckDbIndex, many threads calling search/execute
    under the instance RLock. Multi-process concurrent writers against one
    DuckDB file are not supported once connections are long-lived (DuckDB
    exclusive file lock) — that is intentional for the process-wide singleton.
    """
    jsonl_dir = tmp_path / "jsonl"
    db_path = tmp_path / "duckdb" / "metadata.duckdb"

    _write_doc(jsonl_dir, 1, title="Global financial markets rally", text="The financial sector surged today.")

    idx = DuckDbIndex(db_path=db_path, jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    # Warm the long-lived connection + views/FTS once (as lifespan would).
    idx.execute("SELECT COUNT(*) FROM captures")
    idx.search("financial")

    num_threads = 8
    ops_per_thread = 20
    errors: list[str] = []
    err_lock = threading.Lock()

    def worker(worker_id: int) -> None:
        for i in range(ops_per_thread):
            try:
                if i % 2 == 0:
                    rows = idx.execute("SELECT COUNT(*) AS count FROM captures")
                    assert rows and int(rows[0]["count"]) >= 1
                else:
                    res = idx.search("financial")
                    assert res["total"] >= 1
            except Exception as e:  # noqa: BLE001 — collect for assertion
                with err_lock:
                    errors.append(f"worker {worker_id} op {i}: {e}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    idx.close()
    assert not errors, f"Detected {len(errors)} concurrency exceptions: {errors[:5]}"


def test_search_reuses_same_connection(tmp_path: Path) -> None:
    """Hot search path must not open a new DuckDB connection per call."""
    jsonl_dir = tmp_path / "jsonl"
    _write_doc(jsonl_dir, 1, title="Bitcoin rally", text="Markets moved on bitcoin news.")
    idx = DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=jsonl_dir,
        iceberg_warehouse=None,
    )
    idx.search("bitcoin")
    conn1 = idx._conn
    assert conn1 is not None
    idx.search("bitcoin")
    idx.execute("SELECT 1 AS n")
    assert idx._conn is conn1
    idx.close()
