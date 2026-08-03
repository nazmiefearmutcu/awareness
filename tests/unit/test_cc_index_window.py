"""H-16/H-17 regression: cc_index restricts CDX queries to the window."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import unquote

import pytest

from awareness.schemas.doc import SourceKind
from awareness.schemas.jobs import BackfillRequest
from awareness.sources.base import AdapterContext, PartitionSpec
from awareness.sources.cc_index import CommonCrawlIndexAdapter
from awareness.util.http import RetryableHTTPError


def _ctx() -> AdapterContext:
    return AdapterContext(
        user_agent="test-ua",
        job_id="job-c",
        task_id="task-c",
        batch_id="batch-c",
        ingest_version="test",
        checkpoint={},
        is_stopping=lambda: False,
        extras={},
    )


def test_plan_carries_start_end_in_payload() -> None:
    req = BackfillRequest(
        start=datetime(2024, 6, 1, tzinfo=UTC),
        end=datetime(2024, 6, 10, tzinfo=UTC),
        domains=["example.com"],
        sources=[SourceKind.COMMON_CRAWL_INDEX],
    )
    parts = CommonCrawlIndexAdapter().plan(req)
    assert parts
    for p in parts:
        assert p.payload["start"] == "2024-06-01T00:00:00+00:00"
        assert p.payload["end"] == "2024-06-10T00:00:00+00:00"


def _fake_get_with_retries(pages: list[list[dict[str, Any]]]):
    calls: list[str] = []

    async def _get(client, url, **kwargs):
        calls.append(url)
        idx = len(calls) - 1
        records = pages[idx] if idx < len(pages) else []
        text = "\n".join(json.dumps(r) for r in records)
        resp = MagicMock()
        resp.status_code = 200
        resp.text = text
        return resp

    return _get, calls


@pytest.mark.asyncio
async def test_run_partition_sends_from_to_window() -> None:
    """H-16: CDX params include from/to = crawl-window ∩ request-window."""
    fake_get, calls = _fake_get_with_retries([[]])
    with patch("awareness.sources.cc_index.get_with_retries", fake_get):
        adapter = CommonCrawlIndexAdapter()
        partition = PartitionSpec(
            source_type=SourceKind.COMMON_CRAWL_INDEX,
            partition_key="CC-MAIN-2024-22:cdx:example.com",
            payload={
                "crawl_id": "CC-MAIN-2024-22",
                "url_filter": "*.example.com/*",
                "max_results": 200,
                # 2024-06-01..2024-06-10 — crawl CC-MAIN-2024-22 spans
                # 2024-05-27..2024-06-17, so the window is the request range.
                "start": "2024-06-01T00:00:00+00:00",
                "end": "2024-06-10T00:00:00+00:00",
            },
        )
        out: list[Any] = []
        async for cap in adapter.run_partition(partition, _ctx()):
            out.append(cap)
    assert out == []
    assert len(calls) == 1
    assert "from=20240601000000" in calls[0]
    assert "to=20240610000000" in calls[0]
    assert "*.example.com/*" in unquote(calls[0])


@pytest.mark.asyncio
async def test_run_partition_paginates_until_empty() -> None:
    """H-17: results are paged (page/pageSize) until a short page."""
    page0 = [{"filename": f"f{i}", "offset": i, "length": 10, "url": f"https://e.example/{i}"} for i in range(3)]
    fake_get, calls = _fake_get_with_retries([page0, []])
    with patch("awareness.sources.cc_index.get_with_retries", fake_get):
        adapter = CommonCrawlIndexAdapter()
        partition = PartitionSpec(
            source_type=SourceKind.COMMON_CRAWL_INDEX,
            partition_key="CC-MAIN-2024-22:cdx:example.com",
            payload={
                "crawl_id": "CC-MAIN-2024-22",
                "url_filter": "*.example.com/*",
                "max_results": 200,
                "start": "2024-06-01T00:00:00+00:00",
                "end": "2024-06-10T00:00:00+00:00",
            },
        )
        ctx = _ctx()
        out: list[Any] = []
        async for cap in adapter.run_partition(partition, ctx):
            out.append(cap)
    assert out == []
    assert len(calls) == 2
    assert "page=0" in calls[0]
    assert "page=1" in calls[1]
    assert "pageSize=100" in calls[0]
    enqueued = ctx.extras.get("enqueue") or []
    assert len(enqueued) == 3
    assert enqueued[0].payload["warc_path"] == "f0"


@pytest.mark.asyncio
async def test_run_partition_404_skips() -> None:
    async def _get(client, url, **kwargs):
        resp = MagicMock()
        resp.status_code = 404
        resp.text = ""
        return resp

    with patch("awareness.sources.cc_index.get_with_retries", _get):
        adapter = CommonCrawlIndexAdapter()
        partition = PartitionSpec(
            source_type=SourceKind.COMMON_CRAWL_INDEX,
            partition_key="CC-MAIN-2024-22:cdx:example.com",
            payload={
                "crawl_id": "CC-MAIN-2024-22",
                "url_filter": "*.example.com/*",
                "max_results": 200,
                "start": "2024-06-01T00:00:00+00:00",
                "end": "2024-06-10T00:00:00+00:00",
            },
        )
        out: list[Any] = []
        async for cap in adapter.run_partition(partition, _ctx()):
            out.append(cap)
    assert out == []


@pytest.mark.asyncio
async def test_run_partition_persistent_503_raises() -> None:
    """H-17: persistent transient failure surfaces as RetryableHTTPError."""
    async def _get(client, url, **kwargs):
        raise RetryableHTTPError(f"{url} -> 503 after 4 attempts")

    with patch("awareness.sources.cc_index.get_with_retries", _get):
        adapter = CommonCrawlIndexAdapter()
        partition = PartitionSpec(
            source_type=SourceKind.COMMON_CRAWL_INDEX,
            partition_key="CC-MAIN-2024-22:cdx:example.com",
            payload={
                "crawl_id": "CC-MAIN-2024-22",
                "url_filter": "*.example.com/*",
                "max_results": 200,
                "start": "2024-06-01T00:00:00+00:00",
                "end": "2024-06-10T00:00:00+00:00",
            },
        )
        with pytest.raises(RetryableHTTPError):
            async for _ in adapter.run_partition(partition, _ctx()):
                pass
