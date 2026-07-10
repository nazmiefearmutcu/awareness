from __future__ import annotations

from datetime import UTC, datetime

import awareness.sources.cc_crawls as cc

_CATALOG = [
    ("CC-MAIN-2026-21", datetime(2026, 5, 11, tzinfo=UTC), datetime(2026, 5, 25, tzinfo=UTC)),
    ("CC-MAIN-2026-17", datetime(2026, 4, 13, tzinfo=UTC), datetime(2026, 4, 27, tzinfo=UTC)),
    ("CC-MAIN-2026-12", datetime(2026, 3, 16, tzinfo=UTC), datetime(2026, 3, 30, tzinfo=UTC)),
]


def test_resolves_only_overlapping_crawls(monkeypatch) -> None:
    monkeypatch.setattr(cc, "_load_catalog", lambda: _CATALOG)
    ids = cc.resolve_crawl_ids(
        datetime(2026, 4, 1, tzinfo=UTC), datetime(2026, 5, 1, tzinfo=UTC)
    )
    assert ids == ["CC-MAIN-2026-17"]


def test_bundled_fallback_returns_only_real_ids(monkeypatch) -> None:
    monkeypatch.setattr(cc, "_fetch_catalog", lambda: None)
    monkeypatch.setattr(cc, "_read_cache", lambda: None)
    ids = cc.resolve_crawl_ids(
        datetime(2024, 1, 1, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC)
    )
    assert ids, "fallback must still yield crawl ids for a wide range"
    assert set(ids).issubset(set(cc.BUNDLED_CRAWL_IDS))
    assert ids == sorted(ids, reverse=True)
