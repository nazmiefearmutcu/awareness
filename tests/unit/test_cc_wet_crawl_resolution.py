"""C-02 regression: crawl_ids_for_range resolves REAL crawl IDs (bundled)."""

from __future__ import annotations

from datetime import UTC, datetime

import awareness.sources.cc_crawls as cc
from awareness.config import get_settings
from awareness.sources.commoncrawl_wet import crawl_ids_for_range


def test_2024_range_returns_real_bundled_crawl_ids(monkeypatch) -> None:
    monkeypatch.setattr(cc, "_fetch_catalog", lambda: None)
    monkeypatch.setattr(cc, "_read_cache", lambda: None)
    crawls = crawl_ids_for_range(
        datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 12, 31, tzinfo=UTC)
    )
    # The real published 2024 crawls in the bundled snapshot — the old
    # implementation fabricated odd-week anchors that missed most of these.
    expected_2024 = [
        "CC-MAIN-2024-51",
        "CC-MAIN-2024-46",
        "CC-MAIN-2024-42",
        "CC-MAIN-2024-38",
        "CC-MAIN-2024-33",
        "CC-MAIN-2024-30",
        "CC-MAIN-2024-26",
        "CC-MAIN-2024-22",
        "CC-MAIN-2024-18",
        "CC-MAIN-2024-10",
    ]
    for cid in expected_2024:
        assert cid in crawls, f"real crawl {cid} missing from resolution"
    assert set(crawls).issubset(set(cc.BUNDLED_CRAWL_IDS))
    assert crawls == sorted(crawls, reverse=True)


def test_iso_week_fallback_has_no_parity_coercion(monkeypatch) -> None:
    """When catalog resolution fails, every week in range is enumerated —
    no even/odd anchoring (the old bug dropped alternating crawls)."""

    def _boom(start, end):
        raise RuntimeError("catalog unavailable")

    monkeypatch.setattr(cc, "resolve_crawl_ids", _boom)
    crawls = crawl_ids_for_range(
        datetime(2024, 6, 1, tzinfo=UTC), datetime(2024, 6, 10, tzinfo=UTC)
    )
    # ISO weeks 22 and 23 both appear; 2024-06-10 is in week 23.
    assert "CC-MAIN-2024-22" in crawls
    assert "CC-MAIN-2024-23" in crawls


def test_cache_path_honors_cache_dir(monkeypatch) -> None:
    """M-05: cc_crawls cache respects settings.cache_dir, not data_dir/cache."""
    settings = get_settings()
    custom = settings.data_dir / "custom-cache"
    monkeypatch.setattr(settings, "cache_dir", custom, raising=False)
    path = cc._cache_path()
    assert path.parent == custom
    assert path.name == "cc_collinfo.json"
