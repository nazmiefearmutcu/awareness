"""CLI tests for ``awareness quality --history``.

A tiny JSONL corpus is written under the tmp project root (same chunk pattern
as the rest of the unit suite) and the command is driven through Typer's
CliRunner. Plain ``quality`` (no ``--history``) must behave exactly as before.
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
    text: str,
    domain: str = "example.com",
    content_hash: str | None = None,
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
        title=f"doc {idx}",
        text=text,
        language="en",
        content_hash=content_hash,
    )
    (day / f"chunk-{idx:04d}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _history_corpus(root: Path) -> list[str]:
    """Two capture days around a fixed-noon anchor (deterministic dates).

    Yesterday: 4 docs, docs 1+2 an exact-dup pair (dup ratio 0.5). Today: 3
    unique docs, one on a brand-new domain. Returns the expected date strings.
    """
    anchor = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    _write_doc(root, 1, ts=anchor - timedelta(days=1), text="alpha one",
               domain="news.example", content_hash="h1")
    _write_doc(root, 2, ts=anchor - timedelta(days=1, hours=2), text="alpha two",
               domain="news.example", content_hash="h1")
    _write_doc(root, 3, ts=anchor - timedelta(days=1, hours=4), text="alpha three",
               domain="blog.example", content_hash="h3")
    _write_doc(root, 4, ts=anchor - timedelta(days=1, hours=6), text="alpha four",
               domain="markets.example", content_hash="h4")
    _write_doc(root, 5, ts=anchor, text="beta one", domain="markets.example", content_hash="h5")
    _write_doc(root, 6, ts=anchor - timedelta(hours=2), text="beta two",
               domain="news.example", content_hash="h6")
    _write_doc(root, 7, ts=anchor - timedelta(hours=4), text="beta three",
               domain="defi.example", content_hash="h7")
    return [(anchor - timedelta(days=1)).date().isoformat(), anchor.date().isoformat()]


# ── --history ───────────────────────────────────────────────────────────────


def test_quality_history_table_shows_date_rows(tmp_project: Path) -> None:
    days = _history_corpus(tmp_project / "data" / "jsonl")
    result = runner.invoke(app, ["quality", "--history", "7"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Quality history" in out
    assert "Date" in out and "total" in out and "dup%" in out
    assert "near-dup%" in out and "avg_len" in out and "new_domains" in out
    for day in days:
        assert day in out
    assert "50.0" in out  # yesterday: 2 of 4 docs share a hash
    assert "▁" in out  # dup-ratio sparkline rendered


def test_quality_history_json(tmp_project: Path) -> None:
    _history_corpus(tmp_project / "data" / "jsonl")
    result = runner.invoke(app, ["quality", "--history", "3", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload) == 3
    populated = [p for p in payload if p["total"] > 0]
    assert len(populated) == 2
    assert populated[0]["total"] == 4
    assert populated[0]["duplicate_ratio"] == 0.5
    assert populated[0]["new_domains"] == 3
    assert populated[1]["duplicate_ratio"] == 0.0
    assert populated[1]["new_domains"] == 1


def test_quality_history_default_window(tmp_project: Path) -> None:
    _history_corpus(tmp_project / "data" / "jsonl")
    result = runner.invoke(app, ["quality", "--history", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload) == 30


def test_quality_history_empty_corpus_clean_message(tmp_project: Path) -> None:
    result = runner.invoke(app, ["quality", "--history", "5"])
    assert result.exit_code == 0, result.output
    assert "empty corpus" in result.output


# ── plain quality (backward compat) ─────────────────────────────────────────


def test_quality_without_history_is_unchanged(tmp_project: Path) -> None:
    _history_corpus(tmp_project / "data" / "jsonl")
    result = runner.invoke(app, ["quality"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "total_captures" in out
    assert "7" in out
    assert "duplicate_ratio" in out
    assert "Quality history" not in out


def test_quality_empty_corpus_message(tmp_project: Path) -> None:
    result = runner.invoke(app, ["quality"])
    assert result.exit_code == 0, result.output
    assert "empty corpus" in result.output
