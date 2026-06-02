"""Pytest fixtures: temp project root + reset Settings singleton per test."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from awareness.config import reset_settings


@pytest.fixture()
def tmp_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Path, None, None]:
    """Isolate every test in a fresh project root."""
    monkeypatch.setenv("AW_PROJECT_ROOT", str(tmp_path))
    # No on-disk YAML overrides during tests.
    monkeypatch.delenv("AW_CONFIG_FILE", raising=False)
    # `tail start` / `start` set destination flags directly in os.environ (that is
    # how they hand config to reset_settings()). Those writes are NOT undone by
    # monkeypatch, so clear them here to isolate each test from prior leakage.
    for _leak in (
        "AW_ENABLE_ICEBERG", "AW_ENABLE_JSONL_STAGING", "AW_ENABLE_GDRIVE",
        "AW_ICEBERG_WAREHOUSE", "AW_DATA_DIR",
    ):
        monkeypatch.delenv(_leak, raising=False)
    # Always JSON logging off during tests for readable failures.
    monkeypatch.setenv("AW_LOG_JSON", "false")
    monkeypatch.setenv("AW_LOG_LEVEL", "WARNING")
    reset_settings()
    yield tmp_path
    reset_settings()
