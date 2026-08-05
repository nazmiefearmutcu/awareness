"""CLI tests for ``awareness briefing`` (movers, top terms, new domains, sentiment, alerts, GDELT gaps).

Builds a tiny multi-day JSONL corpus under the tmp project root (same chunk
pattern as the rest of the unit suite): an 8-day "bitcoin update" baseline, a
spike day (10 docs on a brand-new domain with sentiment words), and a doc
today. GDELT is skipped with ``--no-gdelt`` so this suite never touches the
network.
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
    """Multi-day corpus: baseline, a spike day on a new domain, one doc today.

    The spike day (10 "great" docs on ``spike.news``) is the only day where
    volume bursts, so the movers / sentiment sections have something to show
    while the top-8 terms are dominated by "bitcoin".
    """
    root = tmp_project / "data" / "jsonl"
    now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    n = 1
    for age in range(8, 0, -1):  # 8 baseline days, 1 doc each
        _write_doc(
            root, n,
            ts=now - timedelta(days=age),
            title="Bitcoin update",
            text="bitcoin update",
            domain="baseline.example",
        )
        n += 1
    for i in range(10):  # spike day on a brand-new domain, positive sentiment
        _write_doc(
            root, n,
            ts=now - timedelta(days=1),
            title=f"Bitcoin halving rally {i}",
            text="bitcoin halving rally great",
            domain="spike.news",
        )
        n += 1
    _write_doc(  # today: still positive sentiment for bitcoin
        root, n,
        ts=now - timedelta(hours=2),
        title="Bitcoin update great",
        text="bitcoin update great",
        domain="baseline.example",
    )


def test_briefing_sections(tmp_project: Path) -> None:
    _corpus(tmp_project)

    result = runner.invoke(app, ["briefing", "--days", "3", "--no-gdelt"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Awareness briefing" in out
    assert "Yesterday's movers" in out
    assert "count 11" in out  # bitcoin spike day: 10 spike + 1 baseline doc
    assert "(z=" in out
    assert "Top terms" in out
    assert "bitcoin" in out  # dominant top term
    assert "New domains" in out
    assert "spike.news" in out  # domain first seen inside the window
    assert "Sentiment shift" in out
    assert "▲" in out  # bitcoin: positive today vs mixed prior days
    # W6-F2: a term with ZERO captures on the last day must be skipped, not
    # reported as a fabricated sentiment crash (▼ would be a lie).
    assert "▼" not in out
    assert "Alerts (last 24h)" in out
    assert "GDELT gaps" in out
    assert "no coverage gaps" in out


def test_briefing_json(tmp_project: Path) -> None:
    _corpus(tmp_project)

    result = runner.invoke(app, ["briefing", "--days", "3", "--json", "--no-gdelt"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["days"] == 3
    assert payload["total_captures"] == 19
    assert payload["top_terms"][0]["term"] == "bitcoin"
    assert any(m["term"] == "bitcoin" and m["zscore"] >= 2.5 for m in payload["movers"])
    assert {"domain": "spike.news", "count": 10} in payload["new_domains"]
    assert any(s["term"] == "bitcoin" and s["direction"] == "up" for s in payload["sentiment"])
    assert payload["alerts"] == {"count": 0, "recent": []}
    assert payload["gdelt_gaps"] == {"skipped": True, "note": None, "gaps": []}
    assert payload["window_start"] < payload["window_end"]


def test_briefing_empty_corpus(tmp_project: Path) -> None:
    result = runner.invoke(app, ["briefing", "--no-gdelt"])
    assert result.exit_code == 0, result.output
    assert "no corpus yet" in result.output

    result = runner.invoke(app, ["briefing", "--json", "--no-gdelt"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["message"] == "no corpus yet — start tail or run a backfill"
