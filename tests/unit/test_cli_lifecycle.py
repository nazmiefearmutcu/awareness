"""CLI tests for ``awareness lifecycle`` (topic phases, compare, emerging).

Builds a tiny multi-day JSONL corpus under the tmp project root (same chunk
pattern as ``test_topicx_engine.py``) and drives the command through Typer's
CliRunner: phase badge + counts table, --chart sparkline, --json payload,
clean "no captures" handling, term validation (exit 2), --compare and
--emerging tables.
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
    domain: str,
    title: str = "",
    text: str = "",
    days_ago: int = 0,
) -> None:
    ts = (
        datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        - timedelta(days=days_ago)
    ).isoformat()
    day = root / "captures" / f"{ts[:4]}" / f"{ts[5:7]}" / f"{ts[8:10]}"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}",
        capture_id=f"cap-{idx:04d}",
        source_type="rss",
        domain=domain,
        url=f"https://{domain}/{idx}",
        fetch_ts=ts,
        observed_ts=ts,
        title=title,
        text=text,
    )
    (day / f"chunk-{idx:04d}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _corpus(tmp_project: Path) -> None:
    """Multi-day corpus: alphaflare (EMERGING), betawave (EXPANDING),
    gammasink (DECLINING), deltadrip (DORMANT)."""
    root = tmp_project / "data" / "jsonl"
    _write_doc(root, 1, domain="example.com", title="Alphaflare report", text="alphaflare signal")
    _write_doc(root, 2, domain="news.example", title="Alphaflare news", text="alphaflare update")
    _write_doc(root, 3, domain="example.com", title="Alphaflare recap", text="alphaflare recap")
    _write_doc(root, 4, domain="example.com", days_ago=1, title="Betawave", text="betawave mention")
    _write_doc(root, 5, domain="example.com", title="Betawave grows", text="betawave betawave")
    _write_doc(root, 6, domain="news.example", title="Betawave rising", text="betawave")
    _write_doc(root, 7, domain="example.com", title="Betawave more", text="betawave again")
    _write_doc(root, 8, domain="example.com", days_ago=10, title="Gammasink peak", text="gammasink gammasink")
    _write_doc(root, 9, domain="news.example", days_ago=10, title="Gammasink story", text="gammasink")
    _write_doc(root, 10, domain="example.com", days_ago=10, title="Gammasink end", text="gammasink")
    _write_doc(root, 11, domain="example.com", days_ago=2, title="Deltadrip", text="deltadrip")


def test_lifecycle_phase_badge_and_counts_table(tmp_project: Path) -> None:
    _corpus(tmp_project)
    result = runner.invoke(app, ["lifecycle", "alphaflare", "--days", "7"])
    assert result.exit_code == 0, result.output
    assert "EMERGING" in result.output
    assert "slope 7d" in result.output
    assert "peak" in result.output
    assert "Date" in result.output
    assert "Count" in result.output
    assert datetime.now(UTC).strftime("%Y-%m-%d") in result.output


def test_lifecycle_chart_sparkline(tmp_project: Path) -> None:
    _corpus(tmp_project)
    result = runner.invoke(app, ["lifecycle", "alphaflare", "--days", "7", "--chart"])
    assert result.exit_code == 0, result.output
    assert "Sparkline" in result.output
    assert "█" in result.output  # busy day = max block
    assert "▁" in result.output  # empty days = floor block


def test_lifecycle_json_payload(tmp_project: Path) -> None:
    _corpus(tmp_project)
    result = runner.invoke(app, ["lifecycle", "alphaflare", "--days", "7", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["term"] == "alphaflare"
    assert data["phase"] == "EMERGING"
    assert data["peak_count"] == 3
    assert data["slope_7d"] > 0
    assert len(data["counts"]) == 8  # 7-day window -> 8 zero-filled buckets


def test_lifecycle_no_captures_for_term(tmp_project: Path) -> None:
    _corpus(tmp_project)
    result = runner.invoke(app, ["lifecycle", "ghostword", "--days", "7"])
    assert result.exit_code == 0, result.output
    assert "No captures for" in result.output
    assert "ghostword" in result.output


def test_lifecycle_empty_corpus_clean_message(tmp_project: Path) -> None:
    result = runner.invoke(app, ["lifecycle", "anything"])
    assert result.exit_code == 0, result.output
    assert "No captures for" in result.output
    assert "anything" in result.output


def test_lifecycle_invalid_term_is_exit_2(tmp_project: Path) -> None:
    _corpus(tmp_project)
    empty = runner.invoke(app, ["lifecycle", ""])
    assert empty.exit_code == 2
    assert "must not be empty" in empty.output
    too_long = runner.invoke(app, ["lifecycle", "x" * 81])
    assert too_long.exit_code == 2
    assert "80 characters" in too_long.output
    control = runner.invoke(app, ["lifecycle", "alpha\tflare"])
    assert control.exit_code == 2
    assert "control" in control.output


def test_lifecycle_compare_table(tmp_project: Path) -> None:
    _corpus(tmp_project)
    result = runner.invoke(
        app, ["lifecycle", "--compare", "alphaflare,betawave,gammasink", "--days", "7"]
    )
    assert result.exit_code == 0, result.output
    assert "Lifecycle comparison" in result.output
    for header in ("term", "phase", "slope_7d", "peak", "first_seen"):
        assert header in result.output
    assert "alphaflare" in result.output
    assert "EMERGING" in result.output
    assert "betawave" in result.output
    assert "EXPANDING" in result.output
    assert "gammasink" in result.output


def test_lifecycle_compare_wins_over_term(tmp_project: Path) -> None:
    _corpus(tmp_project)
    result = runner.invoke(app, ["lifecycle", "gammasink", "--compare", "alphaflare,betawave"])
    assert result.exit_code == 0, result.output
    assert "ignoring TERM" in result.output
    assert "alphaflare" in result.output
    assert "betawave" in result.output


def test_lifecycle_emerging_table(tmp_project: Path) -> None:
    _corpus(tmp_project)
    result = runner.invoke(app, ["lifecycle", "--emerging", "--days", "7", "--limit", "20"])
    assert result.exit_code == 0, result.output
    assert "Emerging terms" in result.output
    assert "alphaflare" in result.output
    assert "betawave" in result.output
    assert "deltadrip" not in result.output  # below the 3-doc floor
