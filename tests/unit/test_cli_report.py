"""CLI tests for ``awareness report`` (digest + quality + alerts + GDELT).

Builds a tiny JSONL corpus under the tmp project root (same chunk pattern as
the rest of the unit suite) and drives the command through Typer's CliRunner.
GDELT is either skipped (``--no-gdelt``) or stubbed on the bridge (mirroring
``tests/unit/test_digest_gdelt.py``) so this suite never touches the network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from awareness.analytics.models import TimeBucket
from awareness.alerts.store import AlertStore
from awareness.cli.main import app
from awareness.config import get_settings
from awareness.gdeltx.engine import GdeltBridge
from awareness.gdeltx.models import GdeltComparison

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


def _alerts_store() -> AlertStore:
    settings = get_settings()
    assert settings.data_dir is not None
    return AlertStore(settings.data_dir / "alerts.db")


def _seed_firing() -> None:
    store = _alerts_store()
    try:
        store.record_firing(
            rule_id="r1",
            rule_name="bitcoin mentions",
            kind="term_count",
            term="bitcoin",
            count=12.0,
            threshold=5.0,
            detail="12 mentions in the last 24h",
        )
    finally:
        store.close()


def _fake_comparison(*, term: str = "bitcoin") -> GdeltComparison:
    buckets = [
        TimeBucket(ts=datetime(2026, 6, 7, tzinfo=UTC), count=1),
        TimeBucket(ts=datetime(2026, 6, 8, tzinfo=UTC), count=3),
    ]
    return GdeltComparison(
        term=term,
        local_count=4,
        gdelt_count=88,
        local_series=buckets,
        gdelt_series=buckets,
        correlation_r=0.42,
        n_days=7,
        note="",
    )


def test_report_markdown_sections(tmp_project: Path) -> None:
    _corpus(tmp_project)
    _seed_firing()

    result = runner.invoke(app, ["report", "--days", "7", "--no-gdelt"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "## Digest" in out
    assert "## Corpus quality" in out
    assert "## Alert activity" in out
    assert "## GDELT context" in out
    assert "total_captures: 3" in out
    assert "bitcoin" in out  # digest top term
    assert "bitcoin mentions" in out  # alert activity row
    assert "GDELT context skipped" in out


def test_report_out_writes_file(tmp_project: Path) -> None:
    _corpus(tmp_project)
    out = tmp_project / "out" / "report.md"

    result = runner.invoke(app, ["report", "--no-gdelt", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "# Awareness report" in text
    assert "## Digest" in text
    assert "## Corpus quality" in text


def test_report_json_output(tmp_project: Path) -> None:
    _corpus(tmp_project)
    _seed_firing()

    result = runner.invoke(app, ["report", "--json", "--no-gdelt"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["days"] == 7
    assert payload["gdelt"] is None
    assert payload["digest"]["title"] == "Weekly Digest (7d)"
    assert payload["digest"]["total_captures"] == 3
    assert any(t["term"] == "bitcoin" for t in payload["digest"]["top_terms"])
    assert payload["quality"]["total_captures"] == 3
    assert payload["firings"][0]["rule_name"] == "bitcoin mentions"
    assert payload["firings"][0]["fired_at"]  # ISO string


def test_report_empty_corpus_zeros(tmp_project: Path) -> None:
    result = runner.invoke(app, ["report", "--days", "7", "--no-gdelt"])
    assert result.exit_code == 0, result.output
    assert "total_captures: 0" in result.output

    result = runner.invoke(app, ["report", "--json", "--no-gdelt"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["digest"]["total_captures"] == 0
    assert payload["quality"]["total_captures"] == 0
    assert payload["firings"] == []


def test_report_gdelt_context_stubbed(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _corpus(tmp_project)
    calls: list[str] = []

    def _fake_compare(self: object, term: str, window_days: int = 14) -> GdeltComparison:
        calls.append(term)
        return _fake_comparison(term=term)

    monkeypatch.setattr(GdeltBridge, "compare_with_local", _fake_compare)

    result = runner.invoke(app, ["report", "--days", "7"])
    assert result.exit_code == 0, result.output
    assert calls == ["bitcoin"]  # digest's top term
    assert "GDELT: bitcoin local 4 vs external 88 (r=0.42)" in result.output

    result = runner.invoke(app, ["report", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["gdelt"] == "GDELT: bitcoin local 4 vs external 88 (r=0.42)"


def test_report_gdelt_unavailable(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _corpus(tmp_project)

    def _raise(self: object, term: str, window_days: int = 14) -> GdeltComparison:
        raise RuntimeError("gdelt offline")

    monkeypatch.setattr(GdeltBridge, "compare_with_local", _raise)

    result = runner.invoke(app, ["report", "--days", "7"])
    assert result.exit_code == 0, result.output
    assert "GDELT unavailable" in result.output
    assert "## Corpus quality" in result.output  # rest of the report intact
