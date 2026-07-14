from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from awareness.config import get_settings
from awareness.schemas.doc import SourceKind
from awareness.sources.base import AdapterContext, PartitionSpec
from awareness.sources.commoncrawl_wet import CommonCrawlWetAdapter, crawl_ids_for_range

DUMMY_WET_RECORD = (
    "WARC/1.0\r\n"
    "WARC-Type: conversion\r\n"
    "WARC-Target-URI: https://example.com/test-wet\r\n"
    "WARC-Date: 2026-06-19T20:00:00Z\r\n"
    "WARC-Record-ID: <urn:uuid:test-record-id>\r\n"
    "Content-Length: 444\r\n"
    "\r\n"
    "This is a dummy WET record used for unit testing the Common Crawl WET parser. It must contain at least two hundred characters and more than fifty words so that the quality checks and the language detector both admit the capture. We include the common English stopwords the of and to with that have be so the Gopher gate sees running prose rather than a spam list. Hello world from the test suite with enough extra filler words here now yes.\r\n\r\n"
)


def test_crawl_ids_for_range_edges() -> None:
    start = datetime(2024, 6, 1, tzinfo=UTC)
    end = datetime(2024, 6, 10, tzinfo=UTC)
    crawls = crawl_ids_for_range(start, end)
    assert len(crawls) >= 1
    assert "CC-MAIN-2024-23" in crawls or "CC-MAIN-2024-21" in crawls


async def test_run_partition_wet_shard_process_pool(tmp_project: Path) -> None:
    # 1. Setup paths and directories using the settings from tmp_project isolation
    settings = get_settings()
    cache_dir = settings.warc_cache_dir or settings.data_dir / "warc"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # The cached filename format is shard_path.replace("/", "_")
    shard_path = "crawl-data/CC-MAIN-2024-26/segments/123/wet/shard.gz"
    local_file = cache_dir / shard_path.replace("/", "_")
    
    # 2. Pre-populate the cache file so no HTTP downloads are triggered
    local_file.write_bytes(DUMMY_WET_RECORD.encode("utf-8"))

    # 3. Create adapter and input specifications
    adapter = CommonCrawlWetAdapter()
    partition = PartitionSpec(
        source_type=SourceKind.COMMON_CRAWL_WET,
        partition_key="CC-MAIN-2024-26:wet:shard",
        payload={
            "kind": "shard-fetch",
            "crawl_id": "CC-MAIN-2024-26",
            "shard_path": shard_path,
        },
    )
    context = AdapterContext(
        user_agent="TestAgent",
        job_id="job-123",
        task_id="task-123",
        batch_id="batch-123",
        ingest_version="0.1.0",
        checkpoint={},
        is_stopping=lambda: False,
    )

    # 4. Execute the parser (streams via bounded asyncio.Queue on a worker thread)
    captures = []
    async for cap in adapter.run_partition(partition, context):
        captures.append(cap)

    # 5. Verify the results
    assert len(captures) == 1
    cap = captures[0]
    assert cap.url == "https://example.com/test-wet"
    assert cap.canonical_url == "https://example.com/test-wet"
    assert cap.domain == "example.com"
    assert "dummy WET record" in cap.text
    assert cap.language == "en"  # langdetect should identify this as English
