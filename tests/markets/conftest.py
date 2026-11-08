"""Shared fixtures for awareness.markets tests.

``fixture_path(name)`` returns a real provider payload recorded on 2026-11-08
(see ``tests/markets/fixtures``). Tests never touch the network.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from awareness.markets.config import MarketsSettings
from awareness.markets.store import MarketsStore

FIXTURES = Path(__file__).parent / "fixtures"
FIXED_NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


@pytest.fixture()
def fixture_path() -> Callable[[str], Path]:
    def _get(name: str) -> Path:
        path = FIXTURES / name
        assert path.exists(), f"missing fixture {name}"
        return path

    return _get


@pytest.fixture()
def fixture_bytes(fixture_path: Callable[[str], Path]) -> Callable[[str], bytes]:
    return lambda name: fixture_path(name).read_bytes()


@pytest.fixture()
def markets_settings(tmp_path: Path) -> MarketsSettings:
    return MarketsSettings(data_dir=tmp_path / "markets", autostart_runner=False, sentiment_backend="lexicon")


@pytest.fixture()
def store(markets_settings: MarketsSettings) -> Iterator[MarketsStore]:
    s = MarketsStore(markets_settings.store_path)
    yield s
    s.close()


@pytest.fixture()
def fixed_now() -> datetime:
    return FIXED_NOW
