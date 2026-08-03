"""CLI tests for ``awareness trends`` (term frequency + z-scores + chart).

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


def test_trends_chart_table_and_sparkline(tmp_project: Path) -> None:
    _corpus(tmp_project)
    result = runner.invoke(app, ["trends", "bitcoin", "--days", "7", "--chart"])
    assert result.exit_code == 0, result.output
    assert "Trend:" in result.output
    assert "bitcoin" in result.output
    assert "Count" in result.output
    assert datetime.now(UTC).strftime("%Y-%m-%d") in result.output
    assert "█" in result.output  # sparkline max block for the busy day
    assert "▁" in result.output  # sparkline floor block for empty days


def test_trends_empty_term_is_bad_parameter(tmp_project: Path) -> None:
    _corpus(tmp_project)
    result = runner.invoke(app, ["trends", ""])
    assert result.exit_code != 0
    assert "must not be empty" in result.output


def test_trends_sentiment_column(tmp_project: Path) -> None:
    root = tmp_project / "data" / "jsonl"
    now = datetime.now(UTC)
    _write_doc(root, 1, ts=now - timedelta(hours=1), title="Bitcoin hits record", text="bitcoin rally")
    result = runner.invoke(app, ["trends", "bitcoin", "--days", "7", "--sentiment"])
    assert result.exit_code == 0, result.output
    assert "Sentiment" in result.output
    assert "+1.00" in result.output  # "record"/"rally" are positive lexicon words


def test_trends_rejects_unknown_granularity(tmp_project: Path) -> None:
    _corpus(tmp_project)
    result = runner.invoke(app, ["trends", "bitcoin", "--granularity", "hour"])
    assert result.exit_code != 0
