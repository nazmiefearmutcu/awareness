"""M-01: ``backfill submit --source`` accepts aliases and fails cleanly.

Previously ``--source CC-WET`` / ``FineWeb`` (help's own examples) crashed
with a raw ValueError traceback. Now the CLI normalizes spellings and raises
``typer.BadParameter`` (exit code 2) with the valid list on failure.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from awareness.cli.main import _resolve_source_kind, app
from awareness.schemas.doc import SourceKind

runner = CliRunner()


def test_resolve_source_kind_aliases() -> None:
    assert _resolve_source_kind("CC-WET") is SourceKind.COMMON_CRAWL_WET
    assert _resolve_source_kind("cc_wet") is SourceKind.COMMON_CRAWL_WET
    assert _resolve_source_kind("common_crawl_wet") is SourceKind.COMMON_CRAWL_WET
    assert _resolve_source_kind("wet") is SourceKind.COMMON_CRAWL_WET
    assert _resolve_source_kind("FineWeb") is SourceKind.FINEWEB
    assert _resolve_source_kind("FW") is SourceKind.FINEWEB
    assert _resolve_source_kind("GDELT") is SourceKind.GDELT
    assert _resolve_source_kind("RSS") is SourceKind.RSS
    assert _resolve_source_kind("Sitemap") is SourceKind.SITEMAP
    # Canonical values still pass through.
    assert _resolve_source_kind("tail_recrawl") is SourceKind.TAIL_RECRAWL
    assert _resolve_source_kind("fineweb_2") is SourceKind.FINEWEB_2


def test_resolve_source_kind_bad_raises_bad_parameter() -> None:
    import typer

    try:
        _resolve_source_kind("bogus-source")
    except typer.BadParameter as exc:
        assert "bogus-source" in str(exc)
        assert "Valid values" in str(exc)
        assert "common_crawl_wet" in str(exc)
    else:
        raise AssertionError("expected typer.BadParameter")


def test_backfill_submit_accepts_alias_sources(tmp_project: Path) -> None:
    # RSS / GDELT / CC-WET plan without optional deps (datasets etc.).
    result = runner.invoke(
        app,
        [
            "backfill",
            "submit",
            "--start",
            "2026-06-01",
            "--source",
            "CC-WET",
            "--source",
            "GDELT",
            "--source",
            "rss",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Submitted backfill" in result.output
    # All three aliases planned tasks (job JSON carries task counts).
    assert '"pending"' in result.output or "zero_tasks" in result.output


def test_backfill_submit_bad_source_clean_error(tmp_project: Path) -> None:
    result = runner.invoke(
        app,
        [
            "backfill",
            "submit",
            "--start",
            "2026-06-01",
            "--source",
            "not-a-source",
        ],
    )
    assert result.exit_code == 2, result.output
    assert "not-a-source" in result.output
    assert "Valid values" in result.output
    assert "Traceback" not in result.output
