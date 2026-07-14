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


def test_jsonl_write_syncs_open_chunk_without_rename(tmp_path: Path, monkeypatch) -> None:
    """Crash-safe flush: write() fsyncs the open .tmp and leaves it unfinalized."""
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
        flush_seconds=3600.0,
    )
    n = w.write([_row(i) for i in range(3)])
    assert n == 3
    # Open chunk still a temp — not yet committed.
    assert w.open_records == 3
    assert w.open_bytes > 0
    assert list(tmp_path.rglob("*.jsonl")) == []
    temps = list(tmp_path.rglob("*.tmp"))
    assert len(temps) == 1
    # write() path invoked fsync (mid-chunk durability).
    assert len(synced) >= 1
    # Explicit sync is idempotent and still returns True while open.
    assert w.sync() is True
    chunk = w.flush()
    assert chunk is not None and chunk.exists()
    assert w.open_records == 0
    assert w.sync() is False


def test_jsonl_recover_orphan_temps_promotes_valid(tmp_path: Path) -> None:
    from awareness.storage.jsonl import recover_orphan_temps

    day = tmp_path / "captures" / "2024" / "01" / "02"
    day.mkdir(parents=True)
    good = day / "captures-1-deadbeef.jsonl.tmp"
    good.write_text(
        json.dumps({"doc_id": "d1", "text": "hello"}) + "\n",
        encoding="utf-8",
    )
    empty = day / "captures-2-cafebabe.jsonl.tmp"
    empty.write_text("", encoding="utf-8")
    junk = day / "captures-3-bad.jsonl.tmp"
    junk.write_text("not-json\n{partial", encoding="utf-8")

    promoted = recover_orphan_temps(tmp_path)
    assert len(promoted) == 1
    final = day / "captures-1-deadbeef.jsonl"
    assert final in promoted
    assert final.exists()
    assert not good.exists()
    assert not empty.exists()
    assert not junk.exists()
    assert json.loads(final.read_text(encoding="utf-8").splitlines()[0])["doc_id"] == "d1"


def test_jsonl_recover_orphan_gzip_temp(tmp_path: Path) -> None:
    import gzip as gzip_mod

    from awareness.storage.jsonl import recover_orphan_temps

    day = tmp_path / "captures" / "2024" / "03" / "04"
    day.mkdir(parents=True)
    tmp = day / "captures-9-abcdef01.jsonl.gz.tmp"
    payload = (json.dumps({"doc_id": "gz1"}) + "\n").encode("utf-8")
    with gzip_mod.open(tmp, "wb") as fh:
        fh.write(payload)

    promoted = recover_orphan_temps(tmp_path)
    assert len(promoted) == 1
    final = promoted[0]
    assert final.name.endswith(".jsonl.gz")
    assert final.exists()
    with gzip_mod.open(final, "rt", encoding="utf-8") as fh:
        assert json.loads(fh.readline())["doc_id"] == "gz1"
