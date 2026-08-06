"""CLI tests: ``awareness briefing`` carries a Quality trend line.

Builds a tiny JSONL corpus (same chunk pattern as the rest of the unit
suite) with a known dup evolution: days -14..-8 carry per-day exact-dup
pairs (a shared ``content_hash`` inside the same bucket), days -7..0 carry
unique docs. The quality history ends at the newest capture day, so the
trailing 7 buckets (no dups) sit below the prior 7 (dup days) and the
briefing prints "quality: dup-ratio ▼ ..." with direction "improved". An
empty corpus skips the line gracefully. GDELT is skipped with ``--no-gdelt``
so this suite never touches the network.
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
        title=title,
        text=text,
        content_hash=content_hash,
    )
    (day / f"chunk-{idx:04d}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _corpus(tmp_project: Path) -> None:
    """Known dup evolution: dup pairs on days -14..-8, unique docs on -7..0.

    ``fresh.example`` first appears on day -3, so the trailing window has
    exactly one new domain.
    """
    root = tmp_project / "data" / "jsonl"
    now = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    n = 1
    for age in range(14, 7, -1):  # days -14..-8: one dup pair per day
        _write_doc(
            root, n, ts=now - timedelta(days=age),
            title="Bitcoin update", text="bitcoin update",
            domain="example.com", content_hash=f"dup-{age}",
        )
        n += 1
        _write_doc(
            root, n, ts=now - timedelta(days=age) + timedelta(hours=1),
            title="Bitcoin update", text="bitcoin update",
            domain="example.com", content_hash=f"dup-{age}",
        )
        n += 1
    for age in range(7, 0, -1):  # days -7..-1: unique docs
        domain = "fresh.example" if age == 3 else "example.com"
        _write_doc(
            root, n, ts=now - timedelta(days=age),
            title="Bitcoin update", text="bitcoin update",
            domain=domain, content_hash=f"clean-{age}",
        )
        n += 1
    _write_doc(  # today: unique doc
        root, n, ts=now - timedelta(hours=2),
        title="Bitcoin update", text="bitcoin update",
        domain="example.com", content_hash="clean-0",
    )


def test_briefing_quality_line_improved(tmp_project: Path) -> None:
    _corpus(tmp_project)

    result = runner.invoke(app, ["briefing", "--days", "3", "--no-gdelt"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "quality: dup-ratio" in out
    assert "▼" in out  # improved arrow
    assert "vs last week" in out
    assert "new domains this window" in out


def test_briefing_quality_trend_json(tmp_project: Path) -> None:
    _corpus(tmp_project)

    result = runner.invoke(app, ["briefing", "--days", "3", "--json", "--no-gdelt"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    trend = payload["quality_trend"]
    assert trend["direction"] == "improved"
    assert trend["dup_ratio_prior"] > 0
    assert trend["dup_ratio_now"] == 0
    assert trend["dup_ratio_prior"] > trend["dup_ratio_now"]
    assert trend["new_domains"] == 1


def test_briefing_quality_line_empty_corpus_skipped(tmp_project: Path) -> None:
    result = runner.invoke(app, ["briefing", "--no-gdelt"])
    assert result.exit_code == 0, result.output
    assert "no corpus yet" in result.output
    assert "quality: dup-ratio" not in result.output

    result = runner.invoke(app, ["briefing", "--json", "--no-gdelt"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["message"] == "no corpus yet — start tail or run a backfill"
