"""M-05: ``export --format txt`` must not overwrite files for duplicate doc_ids.

Re-captures of the same URL share ``doc_id`` but have distinct ``capture_id``;
the txt exporter previously named files only from title+doc_id and clobbered
them. capture_id is now part of the filename.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from awareness.cli.main import app

runner = CliRunner()


def _write_capture(root: Path, *, doc_id: str, capture_id: str, title: str) -> None:
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
        "fetch_ts": "2026-06-01T12:00:00+00:00",
        "observed_ts": None,
        "published_ts": None,
        "last_modified": None,
        "content_type": None,
        "http_status": None,
        "etag": None,
        "title": title,
        "text": f"body for {title} {capture_id}",
        "language": "en",
        "content_hash": None,
        "near_dup_hash": None,
        "robots_decision": None,
        "terms_note_if_relevant": None,
    }
    (day / f"{capture_id}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def test_export_txt_does_not_overwrite_duplicate_doc_ids(tmp_project: Path) -> None:
    # Same doc_id (same URL captured twice) — different capture_ids.
    _write_capture(tmp_project, doc_id="doc-same", capture_id="cap-aaa", title="Breaking News")
    _write_capture(tmp_project, doc_id="doc-same", capture_id="cap-bbb", title="Breaking News")

    out_dir = tmp_project / "exported"
    result = runner.invoke(
        app,
        ["export", "--format", "txt", "--output", str(out_dir), "--limit", "0"],
    )
    assert result.exit_code == 0, result.output
    files = sorted(p.name for p in out_dir.iterdir() if p.is_file())
    assert len(files) == 2, f"expected 2 files, got {files}"
    # Both capture_ids must appear in the filenames.
    assert any("cap-aaa" in f for f in files)
    assert any("cap-bbb" in f for f in files)
    bodies = sorted((out_dir / f).read_text(encoding="utf-8") for f in files)
    assert any("cap-aaa" in b for b in bodies)
    assert any("cap-bbb" in b for b in bodies)


def test_export_txt_single_doc_still_works(tmp_project: Path) -> None:
    _write_capture(tmp_project, doc_id="doc-1", capture_id="cap-1", title="Only Story")

    out_dir = tmp_project / "exported2"
    result = runner.invoke(
        app,
        ["export", "--format", "txt", "--output", str(out_dir), "--limit", "0"],
    )
    assert result.exit_code == 0, result.output
    files = list(out_dir.iterdir())
    assert len(files) == 1
    assert "OnlyStory" in files[0].name
