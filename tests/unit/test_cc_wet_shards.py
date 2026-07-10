from __future__ import annotations

from datetime import UTC, datetime

from awareness.schemas.doc import SourceKind
from awareness.schemas.jobs import BackfillRequest
from awareness.sources.commoncrawl_wet import CommonCrawlWetAdapter


def test_plan_propagates_max_shards(monkeypatch) -> None:
    import awareness.sources.cc_crawls as cc
    monkeypatch.setattr(cc, "_fetch_catalog", lambda: None)
    monkeypatch.setattr(cc, "_read_cache", lambda: None)
    adapter = CommonCrawlWetAdapter(max_shards_per_crawl=4)
    req = BackfillRequest(
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 6, 1, tzinfo=UTC),
        sources=[SourceKind.COMMON_CRAWL_WET],
    )
    parts = adapter.plan(req)
    assert parts, "must produce discovery partitions for a real-crawl range"
    assert all(p.payload["max_shards"] == 4 for p in parts)


def test_default_shard_count_is_four() -> None:
    adapter = CommonCrawlWetAdapter()
    assert adapter._max_shards_per_crawl == 4
