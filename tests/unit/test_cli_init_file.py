"""W19: ``awareness init`` must materialize the DuckDB search-index file.

Previously the state DB, dirs, and Iceberg catalog were created but
``data/duckdb/metadata.duckdb`` only appeared on the first query. init now
opens + health-checks + closes a DuckDbIndex so the file exists immediately,
and a broken index build must warn without failing init.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

import awareness.cli.main as cli_main
from awareness.cli.main import app
from awareness.config import get_settings

runner = CliRunner()


def test_init_materializes_duckdb_file(tmp_project: Path) -> None:
    result = runner.invoke(app, ["init", "--no-interactive"])
    assert result.exit_code == 0, result.output
    settings = get_settings()
    assert settings.data_dir is not None
    duckdb_file = settings.duckdb_path()
    assert duckdb_file.exists(), f"duckdb file missing after init: {duckdb_file}"
    assert duckdb_file.stat().st_size > 0, "duckdb file is empty"
    # Re-running init stays idempotent.
    result2 = runner.invoke(app, ["init", "--no-interactive"])
    assert result2.exit_code == 0, result2.output
    assert duckdb_file.exists()


def test_init_is_idempotent_when_duckdb_broken(tmp_project: Path, monkeypatch) -> None:
    """A failing index build must warn, not fail init."""
    real = cli_main.DuckDbIndex

    class _BoomIndex:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def health_snapshot(self):
            raise RuntimeError("index boom")

        def close(self) -> None:
            pass

    monkeypatch.setattr(cli_main, "DuckDbIndex", _BoomIndex)
    result = runner.invoke(app, ["init", "--no-interactive"])
    assert result.exit_code == 0, result.output
    assert "DuckDB index init skipped" in result.output
    monkeypatch.setattr(cli_main, "DuckDbIndex", real)
