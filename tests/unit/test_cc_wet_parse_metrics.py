"""WET shard download / parse observability metrics."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from warcio.warcwriter import WARCWriter

from awareness.config import get_settings
from awareness.obs.metrics import get_metrics
from awareness.schemas.doc import SourceKind
from awareness.sources.base import AdapterContext, PartitionSpec
from awareness.sources.commoncrawl_wet import (
    CommonCrawlWetAdapter,
    _iter_wet_captures,
)

_PROSE = (
    "This is a multi-record WET metrics fixture paragraph with the common "
    "English stopwords the of and to with that have be for quality admission. "
    "Hello world from the test suite with enough extra filler words here now yes "
    "so that normalization and language detection both admit the capture body. "
)


def _write_wet(path: Path, n: int = 2) -> None:
    with open(path, "wb") as fh:
        writer = WARCWriter(fh, gzip=False)
        for i in range(n):
            text = f"{_PROSE} Record index {i} unique token u{i}.\n"
            payload = text.encode("utf-8")
            rec = writer.create_warc_record(
                f"https://example.com/page-{i}",
                "conversion",
                payload=io.BytesIO(payload),
                length=len(payload),
                warc_content_type="text/plain",
            )
            writer.write_record(rec)


def test_iter_wet_emits_records_seen_and_parse_hist(tmp_path: Path) -> None:
    wet = tmp_path / "shard.warc"
    _write_wet(wet, n=3)
    m = get_metrics()
    before_seen = m.counter_sum("cc_wet.records_seen")

    rows = list(
        _iter_wet_captures(
            path=wet,
            crawl_id="CC-MAIN-2024-26",
            shard_path="crawl-data/x/wet/shard",
            domains_filter=None,
            languages_filter=None,
            user_agent="TestAgent",
            job_id="j1",
            task_id="t1",
            batch_id="b1",
            ingest_version="0.1.0",
        )
    )
    assert len(rows) >= 1
    assert m.counter_sum("cc_wet.records_seen") >= before_seen + 3
    snap = m.snapshot()
    hists = [h for h in snap["histograms"] if h["name"] == "cc_wet.iter_parse_seconds"]
    assert hists and sum(h["count"] for h in hists) >= 1


async def test_run_partition_cache_hit_and_parse_metrics(tmp_project: Path) -> None:
    settings = get_settings()
    cache_dir = settings.warc_cache_dir or settings.data_dir / "warc"
    cache_dir.mkdir(parents=True, exist_ok=True)
    shard_path = "crawl-data/CC-MAIN-2024-26/segments/1/wet/metrics-shard.gz"
    local = cache_dir / shard_path.replace("/", "_")
    _write_wet(local, n=2)

    m = get_metrics()
    before_attempts = m.counter_sum("cc_wet.shard_download_attempts")
    before_emitted = m.counter_sum("cc_wet.shard_parse_emitted")

    adapter = CommonCrawlWetAdapter()
    partition = PartitionSpec(
        source_type=SourceKind.COMMON_CRAWL_WET,
        partition_key="CC-MAIN-2024-26:wet:metrics-shard",
        payload={
            "kind": "shard-fetch",
            "crawl_id": "CC-MAIN-2024-26",
            "shard_path": shard_path,
        },
    )
    context = AdapterContext(
        user_agent="TestAgent",
        job_id="job-m",
        task_id="task-m",
        batch_id="batch-m",
        ingest_version="0.1.0",
        checkpoint={},
        is_stopping=lambda: False,
    )

    captures = []
    async for cap in adapter.run_partition(partition, context):
        captures.append(cap)

    assert len(captures) >= 1
    assert m.counter_sum("cc_wet.shard_download_attempts") >= before_attempts + 1
    assert (
        m.counter_value(
            "cc_wet.shard_download_attempts",
            labels={"crawl_id": "CC-MAIN-2024-26", "outcome": "cache_hit"},
        )
        >= 1
    )
    assert m.counter_sum("cc_wet.shard_parse_emitted") >= before_emitted + len(captures)
    snap = m.snapshot()
    parse_h = [h for h in snap["histograms"] if h["name"] == "cc_wet.shard_parse_seconds"]
    assert parse_h and sum(h["count"] for h in parse_h) >= 1


async def test_run_partition_download_ok_metrics(tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When cache misses, successful HTTP stream records download ok metrics."""
    settings = get_settings()
    cache_dir = settings.warc_cache_dir or settings.data_dir / "warc"
    cache_dir.mkdir(parents=True, exist_ok=True)
    shard_path = "crawl-data/CC-MAIN-2024-26/segments/9/wet/dl-metrics.gz"
    local = cache_dir / shard_path.replace("/", "_")
    local.unlink(missing_ok=True)

    wet_path = cache_dir / "_fixture_wet.warc"
    _write_wet(wet_path, n=1)
    body = wet_path.read_bytes()

    class _FakeResp:
        status_code = 200

        async def aiter_bytes(self, _chunk: int):
            yield body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, *a, **k):
            return _FakeResp()

    import awareness.sources.commoncrawl_wet as mod

    monkeypatch.setattr(mod.httpx, "AsyncClient", _FakeClient)

    m = get_metrics()
    before = m.counter_sum("cc_wet.shard_download_attempts")

    adapter = CommonCrawlWetAdapter()
    partition = PartitionSpec(
        source_type=SourceKind.COMMON_CRAWL_WET,
        partition_key="CC-MAIN-2024-26:wet:dl-metrics",
        payload={
            "kind": "shard-fetch",
            "crawl_id": "CC-MAIN-2024-26",
            "shard_path": shard_path,
        },
    )
    context = AdapterContext(
        user_agent="TestAgent",
        job_id="job-d",
        task_id="task-d",
        batch_id="batch-d",
        ingest_version="0.1.0",
        checkpoint={},
        is_stopping=lambda: False,
    )

    captures = []
    async for cap in adapter.run_partition(partition, context):
        captures.append(cap)

    assert local.exists()
    assert m.counter_sum("cc_wet.shard_download_attempts") >= before + 1
    assert (
        m.counter_value(
            "cc_wet.shard_download_attempts",
            labels={"crawl_id": "CC-MAIN-2024-26", "outcome": "ok"},
        )
        >= 1
    )
    snap = m.snapshot()
    dl_h = [h for h in snap["histograms"] if h["name"] == "cc_wet.shard_download_seconds"]
    assert dl_h and sum(h["count"] for h in dl_h) >= 1
