"""JSONL staging writer emits process-local commit metrics."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from awareness.obs.metrics import get_metrics
from awareness.storage.jsonl import JsonlStagingWriter


def _row(i: int) -> dict:
    return {
        "doc_id": f"d{i:04d}",
        "fetch_ts": datetime(2024, 1, 1, tzinfo=UTC),
        "text": "hello world",
    }


def test_jsonl_commit_increments_metrics(tmp_path: Path) -> None:
    m = get_metrics()
    before_chunks = m.counter_sum("jsonl.chunks_committed")
    before_recs = m.counter_sum("jsonl.records_committed")
    before_written = m.counter_sum("jsonl.records_written")
    before_bytes = m.counter_sum("jsonl.bytes_committed")

    w = JsonlStagingWriter(root=tmp_path, max_records_per_file=100, flush_seconds=60.0)
    n = w.write([_row(i) for i in range(4)])
    assert n == 4
    chunk = w.flush()
    assert chunk is not None and chunk.exists()

    assert m.counter_sum("jsonl.records_written") >= before_written + 4
    assert m.counter_sum("jsonl.chunks_committed") >= before_chunks + 1
    assert m.counter_sum("jsonl.records_committed") >= before_recs + 4
    assert m.counter_sum("jsonl.bytes_committed") > before_bytes

    snap = m.snapshot()
    hists = [h for h in snap["histograms"] if h["name"] == "jsonl.commit_seconds"]
    assert hists
    assert sum(h["count"] for h in hists) >= 1


def test_jsonl_rotation_commits_multiple_chunks(tmp_path: Path) -> None:
    m = get_metrics()
    before = m.counter_sum("jsonl.chunks_committed")
    w = JsonlStagingWriter(root=tmp_path, max_records_per_file=2, flush_seconds=60.0)
    w.write([_row(i) for i in range(5)])
    w.flush()
    # 5 records / 2 per file → at least 2 commits during write + final flush.
    assert m.counter_sum("jsonl.chunks_committed") >= before + 2


def test_jsonl_write_emits_sync_metrics_and_open_gauges(tmp_path: Path) -> None:
    m = get_metrics()
    before_syncs = m.counter_sum("jsonl.syncs")

    w = JsonlStagingWriter(root=tmp_path, max_records_per_file=100, flush_seconds=3600.0)
    w.write([_row(i) for i in range(2)])

    assert m.counter_sum("jsonl.syncs") >= before_syncs + 1
    assert m.counter_value("jsonl.syncs", labels={"outcome": "ok"}) >= 1
    snap = m.snapshot()
    hists = [h for h in snap["histograms"] if h["name"] == "jsonl.sync_seconds"]
    assert hists and sum(h["count"] for h in hists) >= 1
    gauges = {g["name"]: g["value"] for g in snap["gauges"]}
    assert gauges.get("jsonl.open_records") == 2
    assert gauges.get("jsonl.open_bytes", 0) > 0

    w.flush()
    snap2 = m.snapshot()
    gauges2 = {g["name"]: g["value"] for g in snap2["gauges"]}
    assert gauges2.get("jsonl.open_records") == 0
    assert gauges2.get("jsonl.open_bytes") == 0


def test_jsonl_orphan_recovery_metrics(tmp_path: Path) -> None:
    import json

    from awareness.storage.jsonl import recover_orphan_temps

    m = get_metrics()
    before_ok = m.counter_sum("jsonl.orphans_recovered")
    before_rm = m.counter_sum("jsonl.orphans_removed")

    day = tmp_path / "captures" / "2025" / "01" / "01"
    day.mkdir(parents=True)
    (day / "ok.jsonl.tmp").write_text(
        json.dumps({"doc_id": "x"}) + "\n", encoding="utf-8"
    )
    (day / "empty.jsonl.tmp").write_text("", encoding="utf-8")

    promoted = recover_orphan_temps(tmp_path)
    assert len(promoted) == 1
    assert m.counter_sum("jsonl.orphans_recovered") >= before_ok + 1
    assert m.counter_sum("jsonl.orphans_removed") >= before_rm + 1
