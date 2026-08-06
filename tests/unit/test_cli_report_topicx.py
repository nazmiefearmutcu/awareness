"""CLI tests: ``awareness report`` carries a Topic lifecycle section.

Builds a tiny single-term JSONL corpus (same chunk pattern as the rest of
the unit suite) and drives the command through Typer's CliRunner with
``--no-gdelt`` so this suite never touches the network. The markdown report
gains a "## Topic lifecycle" section with the digest's top terms and their
phases; the ``--json`` payload carries ``lifecycle`` (full model dump per
term). An empty corpus yields ``lifecycle: []`` and the report still
succeeds (exit 0).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from awareness.cli.main import app

runner = CliRunner()

_PHASES = {"EMERGING", "EXPANDING", "PEAKING", "DECLINING", "DORMANT", "STABLE"}

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
    """Three captures today; 'bitcoin' is the digest's dominant top term."""
    root = tmp_project / "data" / "jsonl"
    now = datetime.now(UTC).replace(microsecond=0)
    _write_doc(root, 1, ts=now - timedelta(hours=2), title="Bitcoin hits record", text="market rally bitcoin surge")
    _write_doc(root, 2, ts=now - timedelta(hours=1), title="bitcoin crash watch", text="dip")
    _write_doc(root, 3, ts=now - timedelta(hours=3), title="Sports roundup", text="nothing here")


def test_report_markdown_has_lifecycle_section(tmp_project: Path) -> None:
    _corpus(tmp_project)

    result = runner.invoke(app, ["report", "--days", "7", "--no-gdelt"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "## Topic lifecycle" in out
    assert "bitcoin" in out
    # The digest's top term renders as "term (PHASE, slope 7d ...)".
    assert any(f"({phase}," in out for phase in sorted(_PHASES))


def test_report_json_lifecycle_payload(tmp_project: Path) -> None:
    _corpus(tmp_project)

    result = runner.invoke(app, ["report", "--json", "--no-gdelt"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    lifecycle = payload["lifecycle"]
    assert isinstance(lifecycle, list)
    assert len(lifecycle) >= 1
    entry = lifecycle[0]
    assert entry["term"] == "bitcoin"
    assert entry["phase"] in _PHASES
    assert set(entry) == {
        "term", "phase", "counts", "slope_7d",
        "peak_count", "peak_date", "first_seen", "last_seen",
    }
    assert isinstance(entry["counts"], list) and entry["counts"]
    # The digest's top term is profiled first.
    assert lifecycle[0]["term"] == payload["digest"]["top_terms"][0]["term"]
    # Bounded: at most the digest's top 3 terms.
    assert len(lifecycle) <= 3


def test_report_lifecycle_empty_corpus(tmp_project: Path) -> None:
    result = runner.invoke(app, ["report", "--days", "7", "--no-gdelt"])
    assert result.exit_code == 0, result.output
    assert "## Topic lifecycle" in result.output
    assert "No topic lifecycle data" in result.output

    result = runner.invoke(app, ["report", "--json", "--no-gdelt"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["lifecycle"] == []
    assert payload["digest"]["total_captures"] == 0
