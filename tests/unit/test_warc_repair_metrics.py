"""WARC repair adapter emits range-fetch + parse process metrics."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from awareness.obs.metrics import get_metrics
from awareness.schemas.jobs import SourceKind
from awareness.sources.base import AdapterContext, PartitionSpec
from awareness.sources.warc_repair import WarcRepairAdapter
from awareness.util.http import RetryableHTTPError


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


def _mock_shared_client(resp: object) -> MagicMock:
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    return client


def _http_resp(status: int, content: bytes = b"") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.content = content
    resp.aclose = AsyncMock(return_value=None)
    return resp


def _minimal_warc(body: str) -> bytes:
    """Build a single WARC/1.0 response record wrapping an HTTP response."""
    http_block = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/html\r\n"
        f"Content-Length: {len(body)}\r\n"
        "\r\n"
        f"{body}"
    )
    warc = (
        "WARC/1.0\r\n"
        "WARC-Type: response\r\n"
        "WARC-Target-URI: https://example.test/page\r\n"
        "WARC-Date: 2024-01-01T00:00:00Z\r\n"
        "Content-Type: application/http; msgtype=response\r\n"
        f"Content-Length: {len(http_block)}\r\n"
        "\r\n"
        f"{http_block}\r\n\r\n"
    )
    return warc.encode("utf-8")


@pytest.mark.asyncio
async def test_warc_repair_http_error_metrics() -> None:
    """404/410 range responses are permanent — skipped, no retry, http_error."""
    m = get_metrics()
    before = m.counter_value(
        "warc_repair.fetch_attempts",
        labels={"outcome": "http_error", "crawl_id": "CC-MAIN-2024-10"},
    )

    adapter = WarcRepairAdapter()
    with patch(
        "awareness.sources.warc_repair.get_shared_async_client",
        AsyncMock(return_value=_mock_shared_client(_http_resp(404))),
    ):
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
async def test_warc_repair_transient_status_raises_retryable() -> None:
    """M-02: a 503 range response must raise so the task layer retries."""
    m = get_metrics()
    before = m.counter_value(
        "warc_repair.fetch_attempts",
        labels={"outcome": "network_error", "crawl_id": "CC-MAIN-2024-10"},
    )

    adapter = WarcRepairAdapter()
    with patch(
        "awareness.sources.warc_repair.get_shared_async_client",
        AsyncMock(return_value=_mock_shared_client(_http_resp(503))),
    ):
        with pytest.raises(RetryableHTTPError):
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
async def test_warc_repair_network_error_raises_retryable() -> None:
    """M-02: transient transport failure raises (task retries), metric recorded."""
    m = get_metrics()
    before = m.counter_value(
        "warc_repair.fetch_attempts",
        labels={"outcome": "network_error", "crawl_id": "CC-MAIN-2024-10"},
    )

    client = MagicMock()
    client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))

    adapter = WarcRepairAdapter()
    with patch(
        "awareness.sources.warc_repair.get_shared_async_client",
        AsyncMock(return_value=client),
    ):
        with pytest.raises(RetryableHTTPError):
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
async def test_warc_repair_200_full_file_is_http_error() -> None:
    """H-25: a 200 to a byte-range request is never parsed — wrong record."""
    m = get_metrics()
    before = m.counter_value(
        "warc_repair.fetch_attempts",
        labels={"outcome": "http_error", "crawl_id": "CC-MAIN-2024-10"},
    )

    adapter = WarcRepairAdapter()
    with patch(
        "awareness.sources.warc_repair.get_shared_async_client",
        AsyncMock(return_value=_mock_shared_client(_http_resp(200, _minimal_warc("x" * 300)))),
    ):
        out = [c async for c in adapter.run_partition(_partition(), _context())]

    assert out == []
    assert (
        m.counter_value(
            "warc_repair.fetch_attempts",
            labels={"outcome": "http_error", "crawl_id": "CC-MAIN-2024-10"},
        )
        >= before + 1
    )
    assert m.counter_sum("warc_repair.parse_attempts") == 0


@pytest.mark.asyncio
async def test_warc_repair_ok_fetch_empty_parse_metrics() -> None:
    """206 range with a record too short to extract still counts parse empty."""
    m = get_metrics()
    before_ok = m.counter_value(
        "warc_repair.fetch_attempts",
        labels={"outcome": "ok", "crawl_id": "CC-MAIN-2024-10"},
    )
    before_empty = m.counter_value(
        "warc_repair.parse_attempts",
        labels={"outcome": "empty", "crawl_id": "CC-MAIN-2024-10"},
    )

    resp = _http_resp(206, _minimal_warc("tiny body under the min floor"))
    adapter = WarcRepairAdapter()
    with patch(
        "awareness.sources.warc_repair.get_shared_async_client",
        AsyncMock(return_value=_mock_shared_client(resp)),
    ):
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


@pytest.mark.asyncio
async def test_warc_repair_parse_exception_is_error_not_empty() -> None:
    """M-04 variant: a malformed payload raises; outcome 'error' != 'empty'."""
    m = get_metrics()
    before_error = m.counter_value(
        "warc_repair.parse_attempts",
        labels={"outcome": "error", "crawl_id": "CC-MAIN-2024-10"},
    )

    resp = _http_resp(206, b"not-a-warc")
    adapter = WarcRepairAdapter()
    with patch(
        "awareness.sources.warc_repair.get_shared_async_client",
        AsyncMock(return_value=_mock_shared_client(resp)),
    ):
        with pytest.raises(RetryableHTTPError):
            out = [c async for c in adapter.run_partition(_partition(), _context())]
            assert out == []

    assert (
        m.counter_value(
            "warc_repair.parse_attempts",
            labels={"outcome": "error", "crawl_id": "CC-MAIN-2024-10"},
        )
        >= before_error + 1
    )
