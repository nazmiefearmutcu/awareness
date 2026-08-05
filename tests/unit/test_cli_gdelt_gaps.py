"""CLI tests for ``awareness gdelt-gaps`` (coverage-gap flags).

The GDELT bridge is monkeypatched on the class (mirroring
``tests/unit/test_digest_gdelt.py``) so this suite never touches the network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from awareness.cli.main import app
from awareness.gdeltx.engine import GdeltBridge
from awareness.gdeltx.models import GapReport

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


def _write_doc(root: Path, idx: int, *, text: str = "bitcoin") -> None:
    ts = datetime.now(UTC) - timedelta(hours=1)
    day = root / "data" / "jsonl" / "captures" / f"{ts:%Y}" / f"{ts:%m}" / f"{ts:%d}"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}",
        capture_id=f"cap-{idx:04d}",
        source_type="rss",
        domain="example.com",
        url=f"https://example.com/{idx}",
        fetch_ts=ts.isoformat(),
        observed_ts=ts.isoformat(),
        title=text,
        text=text,
    )
    (day / f"chunk-{idx:04d}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _fake_reports(terms: list[str]) -> list[GapReport]:
    """Known GapReports: term 'a' is a clean gap, 'b' a truncated gap."""
    out: list[GapReport] = []
    for term in terms:
        if term == "a":
            out.append(
                GapReport(term="a", local_count=0, gdelt_count=300, ratio=0.0, gap=True)
            )
        else:
            out.append(
                GapReport(
                    term=term,
                    local_count=5,
                    gdelt_count=100,
                    ratio=0.05,
                    gap=True,
                    truncated=True,
                    note="gdelt day(s) hit the 250-record cap; counts are a floor",
                )
            )
    return out


def test_gdelt_gaps_table_badges(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], int]] = []

    def _fake_coverage_gap(self: object, terms: list[str], window_days: int = 7) -> list[GapReport]:
        calls.append((terms, window_days))
        return _fake_reports(terms)

    monkeypatch.setattr(GdeltBridge, "coverage_gap", _fake_coverage_gap)

    result = runner.invoke(app, ["gdelt-gaps", "--terms", "a,b", "--days", "7"])
    assert result.exit_code == 0, result.output
    assert calls == [(["a", "b"], 7)]
    out = result.output
    assert "a" in out and "b" in out
    assert "✓" in out  # gap badge for both terms
    assert "✗" not in out  # every fake report is a gap
    assert "⚠" in out  # 'b' is truncated
    assert "300" in out and "0.000" in out


def test_gdelt_gaps_json_raw_reports(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        GdeltBridge,
        "coverage_gap",
        lambda self, terms, window_days=7: _fake_reports(terms),
    )

    result = runner.invoke(app, ["gdelt-gaps", "--terms", "a,b", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert {r["term"] for r in payload} == {"a", "b"}
    assert payload[0]["gap"] is True
    assert payload[0]["truncated"] is False
    assert any(r["truncated"] is True for r in payload)
    assert any(r["note"] for r in payload)


def test_gdelt_gaps_bridge_raising_unavailable(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(self: object, terms: list[str], window_days: int = 7) -> list[GapReport]:
        raise RuntimeError("gdelt offline")

    monkeypatch.setattr(GdeltBridge, "coverage_gap", _raise)

    result = runner.invoke(app, ["gdelt-gaps", "--terms", "a"])
    assert result.exit_code == 0, result.output
    assert "GDELT unavailable (offline or rate-limited)" in result.output

    result = runner.invoke(app, ["gdelt-gaps", "--terms", "a", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["message"] == "GDELT unavailable (offline or rate-limited)"


def test_gdelt_gaps_default_terms_from_corpus(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(1, 4):
        _write_doc(tmp_project, i)
    calls: list[list[str]] = []

    def _fake_coverage_gap(self: object, terms: list[str], window_days: int = 7) -> list[GapReport]:
        calls.append(terms)
        return [GapReport(term=t, local_count=1, gdelt_count=50, ratio=0.02, gap=True) for t in terms]

    monkeypatch.setattr(GdeltBridge, "coverage_gap", _fake_coverage_gap)

    result = runner.invoke(app, ["gdelt-gaps", "--days", "3"])
    assert result.exit_code == 0, result.output
    assert calls and "bitcoin" in calls[0]
    assert "bitcoin" in result.output


def test_gdelt_gaps_terms_validation(tmp_project: Path) -> None:
    too_many = ",".join(f"t{i}" for i in range(21))
    result = runner.invoke(app, ["gdelt-gaps", "--terms", too_many])
    assert result.exit_code == 2
    assert "1..20" in result.output

    result = runner.invoke(app, ["gdelt-gaps", "--terms", ",,"])
    assert result.exit_code == 2
    assert "1..20" in result.output
