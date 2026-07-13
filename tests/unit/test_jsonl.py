"""JSONL staging writer tests — atomicity, rotation, fsync."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from awareness.storage.jsonl import JsonlStagingWriter


def _row(i: int) -> dict:
    return {
        "doc_id": f"d{i:04d}",
        "fetch_ts": datetime(2024, 1, 1, tzinfo=UTC),
        "text": "hello world",
    }


def test_jsonl_writes_and_commits(tmp_path: Path) -> None:
    w = JsonlStagingWriter(root=tmp_path, max_records_per_file=10, flush_seconds=60.0)
    w.write([_row(i) for i in range(5)])
    chunk = w.flush()
    assert chunk is not None and chunk.exists()
    assert chunk.suffix == ".jsonl"
    lines = chunk.read_text().splitlines()
    assert len(lines) == 5
    payload = json.loads(lines[0])
    assert payload["doc_id"] == "d0000"
    # No .tmp left behind.
    leftovers = list(tmp_path.rglob("*.tmp"))
    assert leftovers == []


def test_jsonl_rotates_on_record_limit(tmp_path: Path) -> None:
    w = JsonlStagingWriter(root=tmp_path, max_records_per_file=3, flush_seconds=60.0)
    w.write([_row(i) for i in range(7)])
    w.flush()
    files = sorted(tmp_path.rglob("*.jsonl"))
    # 7 records / 3 per file = at least 2 rotations.
    assert len(files) >= 2
    total = sum(1 for f in files for _ in f.read_text().splitlines())
    assert total == 7


def test_jsonl_context_manager_commits(tmp_path: Path) -> None:
    with JsonlStagingWriter(root=tmp_path) as w:
        w.write([_row(0)])
    files = list(tmp_path.rglob("*.jsonl"))
    assert len(files) == 1


def test_jsonl_gzip_commit_is_readable(tmp_path: Path) -> None:
    """Compressed chunks fsync via underlying fileobj and rename without .tmp."""
    import gzip

    w = JsonlStagingWriter(
        root=tmp_path,
        max_records_per_file=100,
        flush_seconds=60.0,
        compress=True,
    )
    w.write([_row(i) for i in range(3)])
    chunk = w.flush()
    assert chunk is not None and chunk.exists()
    assert chunk.name.endswith(".jsonl.gz")
    assert list(tmp_path.rglob("*.tmp")) == []
    with gzip.open(chunk, "rt", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["doc_id"] == "d0000"


def test_jsonl_fsync_handle_gzip_uses_underlying(tmp_path: Path, monkeypatch) -> None:
    """Gzip commit path must fsync the real file, not skip durability."""
    import gzip as gzip_mod

    synced: list[int] = []
    real_fsync = os.fsync

    def spy_fsync(fd: int) -> None:
        synced.append(fd)
        try:
            real_fsync(fd)
        except OSError:
            pass

    monkeypatch.setattr("awareness.storage.jsonl.os.fsync", spy_fsync)

    w = JsonlStagingWriter(
        root=tmp_path,
        max_records_per_file=100,
        flush_seconds=60.0,
        compress=True,
    )
    w.write([_row(0)])
    # Underlying handle should be a GzipFile wrapping a real file.
    assert w._fh is not None  # noqa: SLF001
    assert isinstance(w._fh, gzip_mod.GzipFile)  # noqa: SLF001
    chunk = w.flush()
    assert chunk is not None
    # At least one fsync for the data file (plus optional parent-dir fsync).
    assert len(synced) >= 1
