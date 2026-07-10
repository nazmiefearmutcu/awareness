from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
import time

import pytest
import duckdb

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


def _run_worker(db_path: Path, jsonl_dir: Path, worker_id: int, num_ops: int, error_count: multiprocessing.Value):
    """Run a worker process that repeatedly performs search/execute operations on DuckDbIndex."""
    idx = DuckDbIndex(db_path=db_path, jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    for i in range(num_ops):
        try:
            # Alternate between query and search
            if i % 2 == 0:
                idx.execute("SELECT COUNT(*) AS count FROM captures")
            else:
                idx.search("financial")
        except Exception as e:
            # If any exception is raised, print it and increment the shared error counter
            print(f"Worker {worker_id} failed on op {i}: {e}", flush=True)
            with error_count.get_lock():
                error_count.value += 1
        time.sleep(0.01)


def test_duckdb_multi_process_concurrency(tmp_path: Path) -> None:
    jsonl_dir = tmp_path / "jsonl"
    db_path = tmp_path / "duckdb" / "metadata.duckdb"
    
    # Write a doc so there is some data
    _write_doc(jsonl_dir, 1, title="Global financial markets rally", text="The financial sector surged today.")
    
    # Initialize the database and views first
    idx = DuckDbIndex(db_path=db_path, jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    idx.execute("SELECT COUNT(*) FROM captures")
    
    # Run multiple processes concurrently
    num_processes = 5
    ops_per_process = 20
    
    # Shared error count
    error_count = multiprocessing.Value("i", 0)
    
    processes = []
    for i in range(num_processes):
        p = multiprocessing.Process(
            target=_run_worker,
            args=(db_path, jsonl_dir, i, ops_per_process, error_count)
        )
        processes.append(p)
        p.start()
        
    for p in processes:
        p.join()
        
    assert error_count.value == 0, f"Detected {error_count.value} concurrency exceptions during test."
