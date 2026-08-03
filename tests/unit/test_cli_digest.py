"""CLI tests for ``awareness digest`` (markdown / JSON / --out).

Builds a tiny JSONL corpus under the tmp project root (same chunk pattern as
the rest of the unit suite) and drives the command through Typer's CliRunner.
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


def _write_doc(
    root: Path,
    idx: int,
    *,
    ts: datetime,
    title: str = "",
    text: str = "",
    domain: str = "example.com",
) -> None:
    day = root / "captures" / f"{ts:%Y}" / f"{ts:%m}" / f"{ts:%d}"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}",
        capture_id=f"cap-{idx:04d}",
        source_type="rss",
        domain=domain,
        url=f"https://{domain}/{idx}",
        fetch_ts=ts.isoformat(),
        observed_ts=ts.isoformat(),
        title=title,
        text=text,
    )
    (day / f"chunk-{idx:04d}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _corpus(tmp_project: Path) -> None:
    """Three recent captures; 'bitcoin' is the dominant term."""
    root = tmp_project / "data" / "jsonl"
    now = datetime.now(UTC)
    _write_doc(root, 1, ts=now - timedelta(hours=2), title="Bitcoin hits record", text="market rally bitcoin surge")
    _write_doc(root, 2, ts=now - timedelta(hours=1), title="bitcoin crash watch", text="dip")
    _write_doc(root, 3, ts=now - timedelta(hours=3), title="Sports roundup", text="nothing here")


def test_digest_markdown_stdout(tmp_project: Path) -> None:
    _corpus(tmp_project)
    result = runner.invoke(app, ["digest", "--markdown"])
    assert result.exit_code == 0, result.output
    assert "# Weekly Digest" in result.output
    assert "bitcoin" in result.output  # top term rendered in the Top terms section
    assert "## Top terms" in result.output


def test_digest_defaults_to_markdown(tmp_project: Path) -> None:
    _corpus(tmp_project)
    result = runner.invoke(app, ["digest", "--days", "7"])
    assert result.exit_code == 0, result.output
    assert "# Weekly Digest" in result.output
    assert "bitcoin" in result.output


def test_digest_json_output(tmp_project: Path) -> None:
    _corpus(tmp_project)
    result = runner.invoke(app, ["digest", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["title"] == "Weekly Digest (7d)"
    assert payload["total_captures"] == 3
    assert any(term["term"] == "bitcoin" for term in payload["top_terms"])


def test_digest_out_writes_file(tmp_project: Path) -> None:
    _corpus(tmp_project)
    out = tmp_project / "out" / "digest.md"
    result = runner.invoke(app, ["digest", "--markdown", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "# Weekly Digest" in text
    assert "bitcoin" in text
