"""H-13/H-14/M-04 regression: shard download transient errors and stop."""

from __future__ import annotations

import asyncio

import httpx
import pytest

import awareness.sources.commoncrawl_wet as mod
from awareness.config import get_settings
from awareness.schemas.doc import SourceKind
from awareness.sources.base import AdapterContext, PartitionSpec
from awareness.sources.commoncrawl_wet import (
    CommonCrawlWetAdapter,
    _stream_shard_with_retries,
)
from awareness.util.http import RetryableHTTPError


class _FakeResp:
    def __init__(self, status_code: int = 200, chunks=None, read_error: Exception | None = None):
        self.status_code = status_code
        self._chunks = chunks or [b"data"]
        self._read_error = read_error
        self.closed = False
        self.headers = {}

    async def __aenter__(self):
        return self

    async def aclose(self):
        self.closed = True

    async def aiter_bytes(self, _chunk: int):
        if self._read_error is not None:
            raise self._read_error
        for c in self._chunks:
            yield c


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def stream(self, *a, **k):
        return self._resp


def _context(is_stopping=None) -> AdapterContext:
    return AdapterContext(
        user_agent="TestAgent",
        job_id="job-r",
        task_id="task-r",
        batch_id="batch-r",
        ingest_version="0.1.0",
        checkpoint={},
        is_stopping=is_stopping or (lambda: False),
    )


def _partition(shard_path: str) -> PartitionSpec:
    return PartitionSpec(
        source_type=SourceKind.COMMON_CRAWL_WET,
        partition_key=f"CC-MAIN-2024-26:wet:{shard_path}",
        payload={
            "kind": "shard-fetch",
            "crawl_id": "CC-MAIN-2024-26",
            "shard_path": shard_path,
        },
    )


@pytest.mark.asyncio
async def test_stream_retry_helper_raises_on_persistent_503() -> None:
    class _FakeClient2:
        def stream(self, *a, **k):
            return _FakeResp(status_code=503)

    with pytest.raises(RetryableHTTPError):
        await _stream_shard_with_retries(
            _FakeClient2(), "https://data.commoncrawl.test/x", max_attempts=2, base_delay=0.0
        )


@pytest.mark.asyncio
async def test_stream_retry_helper_returns_on_404() -> None:
    class _FakeClient2:
        def stream(self, *a, **k):
            return _FakeResp(status_code=404)

    resp = await _stream_shard_with_retries(
        _FakeClient2(), "https://data.commoncrawl.test/x", max_attempts=2, base_delay=0.0
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_run_partition_503_raises_and_leaves_no_tmp(tmp_project, monkeypatch) -> None:
    """H-13: a 503 shard download must raise RetryableHTTPError (task retries),
    and no stale .tmp may remain (M-04)."""
    settings = get_settings()
    cache_dir = settings.warc_cache_dir or settings.data_dir / "warc"
    cache_dir.mkdir(parents=True, exist_ok=True)
    shard_path = "crawl-data/CC-MAIN-2024-26/segments/1/wet/retry503.gz"
    local = cache_dir / shard_path.replace("/", "_")
    local.unlink(missing_ok=True)
    (local.with_suffix(local.suffix + ".tmp")).unlink(missing_ok=True)

    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda *a, **k: _FakeClient(_FakeResp(status_code=503)))

    adapter = CommonCrawlWetAdapter()
    with pytest.raises(RetryableHTTPError):
        async for _ in adapter.run_partition(_partition(shard_path), _context()):
            pass
    assert not local.exists()
    assert not local.with_suffix(local.suffix + ".tmp").exists()


@pytest.mark.asyncio
async def test_run_partition_stream_read_error_raises_and_cleans_tmp(tmp_project, monkeypatch) -> None:
    """H-13: a mid-stream transport error must raise, and tmp is unlinked."""
    settings = get_settings()
    cache_dir = settings.warc_cache_dir or settings.data_dir / "warc"
    cache_dir.mkdir(parents=True, exist_ok=True)
    shard_path = "crawl-data/CC-MAIN-2024-26/segments/1/wet/readerr.gz"
    local = cache_dir / shard_path.replace("/", "_")
    local.unlink(missing_ok=True)

    resp = _FakeResp(status_code=200, read_error=httpx.ReadError("connection reset"))
    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda *a, **k: _FakeClient(resp))

    adapter = CommonCrawlWetAdapter()
    with pytest.raises(RetryableHTTPError):
        async for _ in adapter.run_partition(_partition(shard_path), _context()):
            pass
    assert not local.exists()
    assert not local.with_suffix(local.suffix + ".tmp").exists()


@pytest.mark.asyncio
async def test_stop_mid_download_raises_cancelled_and_cleans_tmp(tmp_project, monkeypatch) -> None:
    """H-14: stop during download → asyncio.CancelledError (task re-queues),
    tmp deleted first."""
    settings = get_settings()
    cache_dir = settings.warc_cache_dir or settings.data_dir / "warc"
    cache_dir.mkdir(parents=True, exist_ok=True)
    shard_path = "crawl-data/CC-MAIN-2024-26/segments/1/wet/stop.gz"
    local = cache_dir / shard_path.replace("/", "_")
    local.unlink(missing_ok=True)

    stopped = {"flag": False}

    def _is_stopping() -> bool:
        stopped["flag"] = True
        return True

    resp = _FakeResp(status_code=200, chunks=[b"first", b"second"])
    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda *a, **k: _FakeClient(resp))

    adapter = CommonCrawlWetAdapter()
    with pytest.raises(asyncio.CancelledError):
        async for _ in adapter.run_partition(_partition(shard_path), _context(_is_stopping)):
            pass
    assert stopped["flag"] is True
    assert not local.exists()
    assert not local.with_suffix(local.suffix + ".tmp").exists()
    assert resp.closed is True
