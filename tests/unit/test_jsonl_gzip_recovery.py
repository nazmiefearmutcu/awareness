"""H-11: truncated gzip orphan temps must be recovered, not deleted.

A crash mid-write leaves ``*.jsonl.gz.tmp`` files whose stream ends before
the gzip trailer (EOFError / BadGzipFile on read). Every COMPLETE record in
them was fsync'd and must be promoted (records 1..N-1); only the trailing
partial record is dropped. Files unreadable from the start are still deleted.

Each test record is written as its own gzip member so truncation points are
exact: a member that is fully present always decompresses, a cut member is
unreadable.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from awareness.storage.jsonl import recover_orphan_temps


def _write_gzip_temp(day: Path, name: str, records: list[dict]) -> tuple[Path, list[bytes]]:
    members = [
        gzip.compress((json.dumps(r) + "\n").encode("utf-8"), mtime=0) for r in records
    ]
    path = day / name
    path.write_bytes(b"".join(members))
    return path, members


def _truncate_to(path: Path, keep_bytes: int) -> int:
    data = path.read_bytes()
    keep = max(1, min(keep_bytes, len(data)))
    path.write_bytes(data[:keep])
    return len(data) - keep


def _read_jsonl(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_truncated_gzip_temp_is_promoted(tmp_path: Path) -> None:
    day = tmp_path / "captures" / "2026" / "01" / "01"
    day.mkdir(parents=True)
    records = [
        {"doc_id": f"d{i}", "text": "word " * (60 + i * 5), "fetch_ts": "2026-01-01T00:00:00+00:00"}
        for i in range(3)
    ]
    tmp, members = _write_gzip_temp(day, "ok.jsonl.gz.tmp", records)
    # Cut inside member 2: member 1 is fully present, the stream then dies.
    cut = len(members[0]) + len(members[1]) // 2
    assert _truncate_to(tmp, cut) > 0

    promoted = recover_orphan_temps(tmp_path)
    assert len(promoted) == 1, "truncated gzip temp with complete records must be promoted"
    final = tmp.with_name("ok.jsonl.gz")
    assert final.exists()
    assert not tmp.exists()

    recovered = _read_jsonl(final)
    assert len(recovered) == 1, "complete member-1 record must survive"
    assert recovered == records[:1]


def test_truncated_gzip_drops_partial_tail(tmp_path: Path) -> None:
    """A cut inside the last member leaves only complete records promoted."""
    day = tmp_path / "captures" / "2026" / "01" / "01"
    day.mkdir(parents=True)
    records = [
        {"doc_id": f"d{i}", "text": "word " * (80 + i * 7), "fetch_ts": "2026-01-01T00:00:00+00:00"}
        for i in range(4)
    ]
    tmp, members = _write_gzip_temp(day, "cut.jsonl.gz.tmp", records)
    # Keep members 1..3 whole and cut inside member 4.
    keep = sum(len(m) for m in members[:3]) + 15
    assert _truncate_to(tmp, keep) > 0

    promoted = recover_orphan_temps(tmp_path)
    assert len(promoted) == 1
    recovered = _read_jsonl(promoted[0])
    assert recovered == records[:3], "only complete records survive, partial tail dropped"


def test_garbage_gzip_temp_is_deleted(tmp_path: Path) -> None:
    day = tmp_path / "captures" / "2026" / "01" / "01"
    day.mkdir(parents=True)
    garbage = day / "bad.jsonl.gz.tmp"
    garbage.write_bytes(b"\x1f\x8bnot-a-real-gzip-stream-at-all")
    empty = day / "empty.jsonl.gz.tmp"
    empty.write_bytes(b"")

    promoted = recover_orphan_temps(tmp_path)
    assert promoted == []
    assert not garbage.exists(), "unreadable-from-start temp must be deleted"
    assert not empty.exists()


def test_truncated_plain_temp_keeps_complete_records(tmp_path: Path) -> None:
    """Plain (non-gzip) temps truncated mid-line keep records 1..N-1."""
    day = tmp_path / "captures" / "2026" / "01" / "01"
    day.mkdir(parents=True)
    tmp = day / "plain.jsonl.tmp"
    tmp.write_text(
        '{"doc_id": "d1"}\n'
        '{"doc_id": "d2"}\n'
        '{"doc_id": "d3", "text": "truncated-mid-li',
        encoding="utf-8",
    )
    promoted = recover_orphan_temps(tmp_path)
    assert len(promoted) == 1
    lines = [l for l in promoted[0].read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["doc_id"] == "d1"
    assert json.loads(lines[1])["doc_id"] == "d2"
