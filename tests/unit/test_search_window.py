from __future__ import annotations

from datetime import datetime

from awareness.cli.main import _resolve_search_window


def test_empty_start_means_all_time() -> None:
    start_dt, end_dt = _resolve_search_window("", "now")
    assert start_dt is None
    assert end_dt is not None


def test_all_keyword_means_all_time() -> None:
    start_dt, _ = _resolve_search_window("all time", "now")
    assert start_dt is None


def test_explicit_start_is_parsed() -> None:
    start_dt, _ = _resolve_search_window("2026-01-01", "now")
    assert isinstance(start_dt, datetime)
    assert start_dt.year == 2026 and start_dt.month == 1
