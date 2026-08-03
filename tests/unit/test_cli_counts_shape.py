"""M-02: ``counts`` "total" must be a scalar int, not a row list."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from awareness.cli.main import app

runner = CliRunner()


def _write_capture(root: Path, *, doc_id: str, capture_id: str, title: str, fetch_ts: str) -> None:
    day = root / "data" / "jsonl" / "captures" / "2026" / "06" / "01"
    day.mkdir(parents=True, exist_ok=True)
    rec = {
        "doc_id": doc_id,
        "capture_id": capture_id,
        "parent_doc_or_dup_group": None,
        "source_type": "rss",
        "source_name": None,
        "source_locator": None,
        "source_shard": None,
        "source_offset_or_record_id": None,
        "discovery_channel": None,
        "job_id": None,
        "batch_id": None,
        "ingest_version": None,
        "url": f"https://example.com/{capture_id}",
        "canonical_url": f"https://example.com/{capture_id}",
        "domain": "example.com",
        "fetch_ts": fetch_ts,
        "observed_ts": None,
        "published_ts": None,
        "last_modified": None,
        "content_type": None,
        "http_status": None,
        "etag": None,
        "title": title,
        "text": f"body for {title}",
        "language": "en",
        "content_hash": None,
        "near_dup_hash": None,
        "robots_decision": None,
        "terms_note_if_relevant": None,
    }
    (day / f"{capture_id}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def test_counts_total_is_scalar_int(tmp_project: Path) -> None:
    _write_capture(tmp_project, doc_id="doc-1", capture_id="cap-1", title="One", fetch_ts="2026-06-01T12:00:00+00:00")
    _write_capture(tmp_project, doc_id="doc-2", capture_id="cap-2", title="Two", fetch_ts="2026-06-01T13:00:00+00:00")

    result = runner.invoke(app, ["counts", "--start", "2026-06-01", "--end", "2026-06-02"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert isinstance(data["total"], int), f"total must be an int, got {type(data['total'])}: {data['total']!r}"
    assert data["total"] == 2
    assert isinstance(data["by_source"], list)
    assert isinstance(data["by_domain"], list)
    assert isinstance(data["by_language"], list)


def test_counts_empty_total_is_zero_int(tmp_project: Path) -> None:
    result = runner.invoke(app, ["counts", "--start", "2026-01-01", "--end", "2026-01-02"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["total"] == 0
    assert isinstance(data["total"], int)
