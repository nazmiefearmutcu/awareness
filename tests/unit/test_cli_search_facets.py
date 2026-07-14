"""CLI search prints domain facets summary when present."""

from __future__ import annotations

import json
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
    title: str,
    text: str,
    domain: str,
    source_type: str = "rss",
    language: str | None = "en",
) -> None:
    day = root / "data" / "jsonl" / "captures" / "2026" / "06" / "01"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}",
        capture_id=f"cap-{idx}",
        source_type=source_type,
        domain=domain,
        url=f"https://{domain}/{idx}",
        fetch_ts="2026-06-01T12:00:00+00:00",
        title=title,
        text=text,
        language=language,
    )
    (day / f"chunk-{idx}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def test_search_prints_domain_facets_summary(tmp_project: Path) -> None:
    _write_doc(tmp_project, 1, title="Alpha one", text="alpha news", domain="a.example")
    _write_doc(tmp_project, 2, title="Alpha two", text="alpha again", domain="a.example")
    _write_doc(tmp_project, 3, title="Alpha three", text="alpha other", domain="b.example")

    result = runner.invoke(app, ["search", "alpha", "--no-interactive", "--mode", "substring"])
    assert result.exit_code == 0, result.output
    # Summary line: Domains: a.example (2), b.example (1)
    assert "Domains:" in result.output
    assert "a.example" in result.output
    assert "b.example" in result.output
    # Count annotations present for the top domain.
    assert "a.example (2)" in result.output or "a.example(2)" in result.output


def test_search_prints_language_facets_summary(tmp_project: Path) -> None:
    _write_doc(
        tmp_project, 1, title="Alpha one", text="alpha news",
        domain="a.example", language="en",
    )
    _write_doc(
        tmp_project, 2, title="Alpha two", text="alpha again",
        domain="b.example", language="tr",
    )
    result = runner.invoke(app, ["search", "alpha", "--no-interactive", "--mode", "substring"])
    assert result.exit_code == 0, result.output
    assert "Languages:" in result.output
    assert "en" in result.output
    assert "tr" in result.output


def test_search_omits_domain_facets_when_empty(tmp_project: Path) -> None:
    _write_doc(tmp_project, 1, title="Only sports", text="football only", domain="sports.example")
    result = runner.invoke(
        app, ["search", "zzzz-no-match-zzzz", "--no-interactive", "--mode", "substring"]
    )
    assert result.exit_code == 0, result.output
    assert "Domains:" not in result.output

def test_search_prints_source_facets_summary(tmp_project: Path) -> None:
    _write_doc(
        tmp_project, 1, title="Alpha one", text="alpha news",
        domain="a.example", source_type="rss",
    )
    _write_doc(
        tmp_project, 2, title="Alpha two", text="alpha again",
        domain="a.example", source_type="rss",
    )
    _write_doc(
        tmp_project, 3, title="Alpha wet", text="alpha wet",
        domain="b.example", source_type="common_crawl_wet",
    )

    result = runner.invoke(app, ["search", "alpha", "--no-interactive", "--mode", "substring"])
    assert result.exit_code == 0, result.output
    assert "Sources:" in result.output
    assert "rss" in result.output
    assert "common_crawl_wet" in result.output
    # Count annotations for the dominant source.
    assert "rss (2)" in result.output or "rss(2)" in result.output
    # Domain facets still printed next to / above sources.
    assert "Domains:" in result.output


def test_search_omits_source_facets_when_empty(tmp_project: Path) -> None:
    _write_doc(tmp_project, 1, title="Only sports", text="football only", domain="sports.example")
    result = runner.invoke(
        app, ["search", "zzzz-no-match-zzzz", "--no-interactive", "--mode", "substring"]
    )
    assert result.exit_code == 0, result.output
    assert "Sources:" not in result.output

