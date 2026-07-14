"""WARC repair adapter emits range-fetch + parse process metrics."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from awareness.obs.metrics import get_metrics
from awareness.schemas.jobs import SourceKind
from awareness.sources.base import AdapterContext, PartitionSpec
from awareness.sources.warc_repair import WarcRepairAdapter


def _context() -> AdapterContext:
    return AdapterContext(
        user_agent="awareness-test/0.0",
        job_id="j1",
        task_id="t1",
        batch_id="b1",
        ingest_version="v1",
        checkpoint={},
        is_stopping=lambda: False,
    )


def _partition(**extra: object) -> PartitionSpec:
    payload = {
        "warc_path": "crawl-data/CC-MAIN-2024-10/segments/x/warc/file.warc.gz",
        "offset": 100,
        "length": 50,
        "url": "https://example.test/page",
        "crawl_id": "CC-MAIN-2024-10",
    }
    payload.update(extra)
    return PartitionSpec(
        source_type=SourceKind.COMMON_CRAWL_WARC,
        partition_key="repair:file:100",
        payload=payload,
    )


@pytest.mark.asyncio
async def test_warc_repair_http_error_metrics() -> None:
    m = get_metrics()
    before = m.counter_value(
        "warc_repair.fetch_attempts",
        labels={"outcome": "http_error", "crawl_id": "CC-MAIN-2024-10"},
    )

    resp = MagicMock()
    resp.status_code = 404
    resp.content = b""

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    adapter = WarcRepairAdapter()
    with patch("awareness.sources.warc_repair.httpx.AsyncClient", return_value=mock_client):
        out = [c async for c in adapter.run_partition(_partition(), _context())]

    assert out == []
    assert (
        m.counter_value(
            "warc_repair.fetch_attempts",
            labels={"outcome": "http_error", "crawl_id": "CC-MAIN-2024-10"},
        )
        >= before + 1
    )
    snap = m.snapshot()
    hists = [
        h
        for h in snap["histograms"]
        if h["name"] == "warc_repair.fetch_seconds"
        and (h.get("labels") or {}).get("outcome") == "http_error"
    ]
    assert hists and sum(h["count"] for h in hists) >= 1


@pytest.mark.asyncio
async def test_warc_repair_network_error_metrics() -> None:
    m = get_metrics()
    before = m.counter_value(
        "warc_repair.fetch_attempts",
        labels={"outcome": "network_error", "crawl_id": "CC-MAIN-2024-10"},
    )

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    adapter = WarcRepairAdapter()
    with patch("awareness.sources.warc_repair.httpx.AsyncClient", return_value=mock_client):
        out = [c async for c in adapter.run_partition(_partition(), _context())]

    assert out == []
    assert (
        m.counter_value(
            "warc_repair.fetch_attempts",
            labels={"outcome": "network_error", "crawl_id": "CC-MAIN-2024-10"},
        )
        >= before + 1
    )


@pytest.mark.asyncio
async def test_warc_repair_ok_fetch_empty_parse_metrics() -> None:
    """Successful range GET with non-parseable payload still counts parse empty."""
    m = get_metrics()
    before_ok = m.counter_value(
        "warc_repair.fetch_attempts",
        labels={"outcome": "ok", "crawl_id": "CC-MAIN-2024-10"},
    )
    before_empty = m.counter_value(
        "warc_repair.parse_attempts",
        labels={"outcome": "empty", "crawl_id": "CC-MAIN-2024-10"},
    )

    resp = MagicMock()
    resp.status_code = 206
    resp.content = b"not-a-warc"

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    adapter = WarcRepairAdapter()
    with patch("awareness.sources.warc_repair.httpx.AsyncClient", return_value=mock_client):
        out = [c async for c in adapter.run_partition(_partition(), _context())]

    assert out == []
    assert (
        m.counter_value(
            "warc_repair.fetch_attempts",
            labels={"outcome": "ok", "crawl_id": "CC-MAIN-2024-10"},
        )
        >= before_ok + 1
    )
    assert (
        m.counter_value(
            "warc_repair.parse_attempts",
            labels={"outcome": "empty", "crawl_id": "CC-MAIN-2024-10"},
        )
        >= before_empty + 1
    )
    snap = m.snapshot()
    parse_hists = [h for h in snap["histograms"] if h["name"] == "warc_repair.parse_seconds"]
    assert parse_hists and sum(h["count"] for h in parse_hists) >= 1
