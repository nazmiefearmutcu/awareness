"""C3-T3: WET parse streams records with bounded in-flight memory.

Regression guard for the OOM pattern where ``_parse_wet_to_captures`` (and the
adapter's process-pool path) materialised every ``DocCapture`` from a shard
into one list before any yield. Production path must:

1. Iterate warcio records one-at-a-time (no whole-file load of capture objects).
2. Bridge producer → consumer through a bounded ``asyncio.Queue``.
3. Apply backpressure so peak queued captures ≤ queue maxsize.

These tests use a multi-record WARC fixture and instrument the queue, not RSS
(which is noisy under pytest).
"""

from __future__ import annotations

import asyncio
import inspect
import io
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from warcio.warcwriter import WARCWriter

from awareness.config import get_settings
from awareness.config.settings import reset_settings
from awareness.schemas.doc import SourceKind
from awareness.sources import commoncrawl_wet as cc_wet
from awareness.sources.base import AdapterContext, PartitionSpec
from awareness.sources.commoncrawl_wet import (
    CommonCrawlWetAdapter,
    _iter_wet_captures,
    _parse_wet_to_captures,
    _stream_wet_captures,
)

# Enough prose to clear text_min_chars + English Gopher quality when filter is on.
_PROSE = (
    "This is a multi-record WET streaming fixture paragraph with the common "
    "English stopwords the of and to with that have be for quality admission. "
    "Hello world from the test suite with enough extra filler words here now yes "
    "so that normalization and language detection both admit the capture body. "
)


def _write_multi_record_wet(path: Path, n: int) -> list[str]:
    """Write ``n`` conversion records; return target URLs in order."""
    urls: list[str] = []
    with open(path, "wb") as fh:
        writer = WARCWriter(fh, gzip=False)
        for i in range(n):
            url = f"https://example.com/page-{i}"
            urls.append(url)
            text = f"{_PROSE} Record index {i} unique token u{i}.\n"
            payload = text.encode("utf-8")
            rec = writer.create_warc_record(
                url,
                "conversion",
                payload=io.BytesIO(payload),
                length=len(payload),
                warc_content_type="text/plain",
            )
            writer.write_record(rec)
    return urls


def test_iter_wet_captures_is_streaming_generator(tmp_path: Path) -> None:
    reset_settings()
    wet = tmp_path / "multi.warc"
    urls = _write_multi_record_wet(wet, 5)

    gen = _iter_wet_captures(
        path=wet,
        crawl_id="CC-MAIN-2026-06",
        shard_path="crawl-data/CC-MAIN-2026-06/segments/x/wet/multi.warc.wet.gz",
        domains_filter=None,
        languages_filter=None,
        user_agent="test-agent",
        job_id="job",
        task_id="task",
        batch_id="batch",
        ingest_version="v1",
    )
    assert inspect.isgenerator(gen)

    first = next(gen)
    assert first.url == urls[0]
    # Remaining records still unread: generator has not buffered them as a list.
    rest = list(gen)
    assert len(rest) == 4
    assert [c.url for c in rest] == urls[1:]


def test_parse_wet_collects_multi_record_fixture(tmp_path: Path) -> None:
    reset_settings()
    wet = tmp_path / "multi.warc"
    urls = _write_multi_record_wet(wet, 3)

    captures = _parse_wet_to_captures(
        path=wet,
        crawl_id="CC-MAIN-2026-06",
        shard_path="crawl-data/CC-MAIN-2026-06/segments/x/wet/multi.warc.wet.gz",
        domains_filter=None,
        languages_filter=None,
        user_agent="test-agent",
        job_id="job",
        task_id="task",
        batch_id="batch",
        ingest_version="v1",
    )
    assert [c.url for c in captures] == urls


@pytest.mark.asyncio
async def test_stream_wet_queue_peak_bounded(tmp_path: Path) -> None:
    """Producer must not race ahead of a slow consumer past queue maxsize."""
    reset_settings()
    wet = tmp_path / "multi.warc"
    n = 12
    urls = _write_multi_record_wet(wet, n)
    queue_max = 2
    peak_qsize = 0
    real_queue_cls = asyncio.Queue

    class TrackingQueue(real_queue_cls):  # type: ignore[valid-type,misc]
        def __init__(self, maxsize: int = 0) -> None:
            super().__init__(maxsize=maxsize)
            nonlocal peak_qsize
            peak_qsize = 0

        async def put(self, item: Any) -> None:
            nonlocal peak_qsize
            await super().put(item)
            # qsize includes the item just put (and may include sentinel None).
            peak_qsize = max(peak_qsize, self.qsize())

    async def slow_consume() -> list[str]:
        out: list[str] = []
        with patch.object(cc_wet.asyncio, "Queue", TrackingQueue):
            stream = _stream_wet_captures(
                path=wet,
                crawl_id="CC-MAIN-2026-06",
                shard_path="crawl-data/CC-MAIN-2026-06/segments/x/wet/multi.warc.wet.gz",
                domains_filter=None,
                languages_filter=None,
                user_agent="test-agent",
                job_id="job",
                task_id="task",
                batch_id="batch",
                ingest_version="v1",
                is_stopping=lambda: False,
                queue_maxsize=queue_max,
            )
            async for cap in stream:
                out.append(cap.url)
                # Slow consumer: if the producer ignored maxsize it would still
                # finish immediately; with a real bound, put blocks until we get.
                await asyncio.sleep(0.01)
        return out

    got = await slow_consume()
    assert got == urls
    # Peak occupancy must respect the bound (sentinel may briefly occupy a slot
    # after the last capture; still ≤ maxsize).
    assert peak_qsize <= queue_max, f"queue peak {peak_qsize} exceeded maxsize {queue_max}"
    assert peak_qsize >= 1


@pytest.mark.asyncio
async def test_adapter_run_partition_streams_multi_record(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reset_settings()
    monkeypatch.setenv("AW_BOUNDED_QUEUE_SIZE", "3")
    reset_settings()
    settings = get_settings()
    assert settings.bounded_queue_size == 3

    cache_dir = settings.warc_cache_dir or settings.data_dir / "warc"
    cache_dir.mkdir(parents=True, exist_ok=True)
    shard_path = "crawl-data/CC-MAIN-2024-26/segments/123/wet/multi.gz"
    local_file = cache_dir / shard_path.replace("/", "_")
    urls = _write_multi_record_wet(local_file, 4)

    adapter = CommonCrawlWetAdapter()
    partition = PartitionSpec(
        source_type=SourceKind.COMMON_CRAWL_WET,
        partition_key="CC-MAIN-2024-26:wet:multi",
        payload={
            "kind": "shard-fetch",
            "crawl_id": "CC-MAIN-2024-26",
            "shard_path": shard_path,
        },
    )
    context = AdapterContext(
        user_agent="TestAgent",
        job_id="job-stream",
        task_id="task-stream",
        batch_id="batch-stream",
        ingest_version="0.2.0",
        checkpoint={},
        is_stopping=lambda: False,
    )

    captures = []
    async for cap in adapter.run_partition(partition, context):
        captures.append(cap)

    assert [c.url for c in captures] == urls
    assert all(c.language == "en" for c in captures)
