"""CLI tests for ``awareness quality --record`` / ``--recorded``.

``--record`` builds a tiny JSONL corpus under the tmp project root (same
chunk pattern as the rest of the unit suite), persists a snapshot to
``<data_dir>/quality_history.jsonl``, and the JSONL store is exercised
directly. ``--recorded`` reads the file back as a table, tolerating torn
lines (a crash mid-append can only corrupt the final line).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from awareness.cli.main import app

runner = CliRunner()

_FULL_KEYS = (
    "doc_id", "capture_id", "parent_doc_or_dup_group", "source_type",
    "source_name", "source_locator", "source_shard",
    "source_offset_or_record_id", "discovery_channel", "job_id", "batch_id",
    "ingest_version", "url", "canonical_url", "domain", "fetch_ts",
    "observed_ts", "published_ts", "last_modified", "content_type",
    "http_status", "etag", "title", "text", "language", "content_hash",
    "near_dup_hash", "robots_decision", "terms_note_if_relevant",
)

_RECORD_FIELDS = (
    "ts", "total", "duplicate_ratio", "near_duplicate_ratio",
    "avg_length", "capture_rate", "dedup_groups",
)


def _write_doc(
    root: Path,
    idx: int,
    *,
    ts: datetime,
    text: str = "",
    content_hash: str | None = None,
    parent_group: str | None = None,
) -> None:
    day = root / "captures" / f"{ts:%Y}" / f"{ts:%m}" / f"{ts:%d}"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}",
        capture_id=f"cap-{idx:04d}",
        parent_doc_or_dup_group=parent_group,
        source_type="rss",
        domain="example.com",
        url=f"https://example.com/{idx}",
        fetch_ts=ts.isoformat(),
        observed_ts=ts.isoformat(),
        title=f"doc {idx}",
        text=text,
        content_hash=content_hash,
    )
    (day / f"chunk-{idx:04d}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _tiny_corpus(tmp_project: Path) -> None:
    """Three docs: one dup pair (shared content_hash AND dup group), one unique."""
    root = tmp_project / "data" / "jsonl"
    now = datetime.now(UTC)
    _write_doc(
        root, 1, ts=now - timedelta(hours=6), text="alpha report",
        content_hash="h-dup", parent_group="g-dup",
    )
    _write_doc(
        root, 2, ts=now - timedelta(hours=4), text="alpha report",
        content_hash="h-dup", parent_group="g-dup",
    )
    _write_doc(root, 3, ts=now - timedelta(hours=2), text="gamma yazi", content_hash="h-3")


def _history_path(tmp_project: Path) -> Path:
    return tmp_project / "data" / "quality_history.jsonl"


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ── --record ────────────────────────────────────────────────────────────────


def test_record_persists_snapshot_jsonl(tmp_project: Path) -> None:
    _tiny_corpus(tmp_project)
    result = runner.invoke(app, ["quality", "--record"])
    assert result.exit_code == 0, result.output
    assert "Recorded quality snapshot:" in result.output

    path = _history_path(tmp_project)
    assert path.exists()
    records = _lines(path)
    assert len(records) == 1
    rec = records[0]
    assert set(rec) == set(_RECORD_FIELDS)
    assert rec["total"] == 3
    assert rec["duplicate_ratio"] == 2 / 3
    assert rec["near_duplicate_ratio"] == 2 / 3
    assert rec["avg_length"] > 0
    assert rec["capture_rate"] > 0
    assert rec["dedup_groups"] == 1
    assert rec["ts"]


def test_record_appends_on_second_run(tmp_project: Path) -> None:
    _tiny_corpus(tmp_project)
    result = runner.invoke(app, ["quality", "--record"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["quality", "--record"])
    assert result.exit_code == 0, result.output

    records = _lines(_history_path(tmp_project))
    assert len(records) == 2
    for rec in records:
        assert set(rec) == set(_RECORD_FIELDS)


def test_record_on_empty_corpus_writes_zeroed_snapshot(tmp_project: Path) -> None:
    result = runner.invoke(app, ["quality", "--record"])
    assert result.exit_code == 0, result.output

    records = _lines(_history_path(tmp_project))
    assert len(records) == 1
    assert records[0]["total"] == 0
    assert records[0]["duplicate_ratio"] == 0.0


# ── --recorded ──────────────────────────────────────────────────────────────


def test_recorded_renders_table(tmp_project: Path) -> None:
    _tiny_corpus(tmp_project)
    runner.invoke(app, ["quality", "--record"])
    runner.invoke(app, ["quality", "--record"])
    records = _lines(_history_path(tmp_project))

    result = runner.invoke(app, ["quality", "--recorded", "7"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Recorded quality snapshots" in out
    assert records[0]["ts"][:10] in out  # date part of the ts column
    assert records[1]["ts"][:10] in out
    assert "3" in out  # total column
    assert "dup%" in out


def test_recorded_json_skips_corrupt_line(tmp_project: Path) -> None:
    _tiny_corpus(tmp_project)
    runner.invoke(app, ["quality", "--record"])
    path = _history_path(tmp_project)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("this is not json { torn line\n")

    result = runner.invoke(app, ["quality", "--recorded", "7", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload) == 1
    assert payload[0]["total"] == 3


def test_recorded_table_notes_skipped_corrupt_line(tmp_project: Path) -> None:
    _tiny_corpus(tmp_project)
    runner.invoke(app, ["quality", "--record"])
    path = _history_path(tmp_project)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("{torn-json\n")

    result = runner.invoke(app, ["quality", "--recorded", "7"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "skipped 1 unparseable line(s)" in out
    assert "total" in out


def test_recorded_empty_message(tmp_project: Path) -> None:
    result = runner.invoke(app, ["quality", "--recorded", "7"])
    assert result.exit_code == 0, result.output
    assert "no recorded quality snapshots" in result.output
