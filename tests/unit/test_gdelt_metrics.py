"""GDELT slot fetch / extract / enqueue process-local metrics."""

from __future__ import annotations

import io
import zipfile
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from awareness.obs.metrics import MetricsRegistry
from awareness.schemas.doc import SourceKind
from awareness.sources.base import AdapterContext, PartitionSpec
from awareness.sources.gdelt import (
    GdeltAdapter,
    _extract_gkg_urls,
    _extract_gkg_urls_with_status,
)


def _gkg_zip(urls: list[str]) -> bytes:
    """Build a minimal GKG-like TSV zip with DOCUMENTIDENTIFIER at column 4."""
    lines: list[str] = []
    for u in urls:
        # cols 0-3 pad, col 4 = url
        row = ["", "", "", "", u, "extra"]
        lines.append("\t".join(row))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("20260601113000.gkg.csv", "\n".join(lines) + "\n")
    return buf.getvalue()


@pytest.fixture()
def metrics(monkeypatch: pytest.MonkeyPatch) -> MetricsRegistry:
    reg = MetricsRegistry()
    monkeypatch.setattr("awareness.sources.gdelt.get_metrics", lambda: reg)
    return reg


def test_extract_gkg_urls_ok() -> None:
    zipped = _gkg_zip(["https://news.example/a", "https://news.example/b", "not-a-url"])
    urls, ok = _extract_gkg_urls_with_status(zipped)
    assert ok is True
    assert set(urls) == {"https://news.example/a", "https://news.example/b"}
    assert set(_extract_gkg_urls(zipped)) == set(urls)


def test_extract_gkg_urls_bad_zip() -> None:
    urls, ok = _extract_gkg_urls_with_status(b"not-a-zip")
    assert ok is False
    assert urls == []


def _ctx() -> AdapterContext:
    return AdapterContext(
        user_agent="test-agent",
        job_id="job-g",
        task_id="task-g",
        batch_id="batch-g",
        ingest_version="test",
        checkpoint={},
        is_stopping=lambda: False,
        extras={},
    )


@pytest.mark.asyncio
async def test_run_partition_ok_records_metrics(
    metrics: MetricsRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    zipped = _gkg_zip(
        [
            "https://news.example/story-1",
            "https://news.example/story-2",
            "ftp://bad.example/x",  # skipped by scheme gate
        ]
    )

    class _Resp:
        status_code = 200
        content = zipped

    client = MagicMock()
    client.get = AsyncMock(return_value=_Resp())
    monkeypatch.setattr(
        "awareness.sources.gdelt.get_shared_async_client",
        AsyncMock(return_value=client),
    )

    adapter = GdeltAdapter()
    ctx = _ctx()
    part = PartitionSpec(
        source_type=SourceKind.GDELT,
        partition_key="gdelt:gkg:20260601113000",
        payload={"slot": "20260601113000", "max_urls": 10},
    )
    # Consume async generator (yields nothing; side-effects via extras + metrics).
    async for _ in adapter.run_partition(part, ctx):
        pass

    assert metrics.counter_sum("gdelt.fetch_attempts") == 1.0
    assert metrics.counter_value(
        "gdelt.fetch_attempts", labels={"outcome": "ok", "status_class": "2xx"}
    ) == 1.0
    assert metrics.counter_sum("gdelt.urls_discovered") == 2.0
    assert metrics.counter_sum("gdelt.urls_enqueued") == 2.0
    # Histogram recorded.
    snap = metrics.snapshot()
    hists = [h for h in snap["histograms"] if h["name"] == "gdelt.fetch_seconds"]
    assert hists and hists[0]["count"] >= 1
    enqueue: list[Any] = ctx.extras.get("enqueue") or []
    assert len(enqueue) == 2
    assert all(p.source_type == SourceKind.TAIL_RECRAWL for p in enqueue)


@pytest.mark.asyncio
async def test_run_partition_missing_slot(
    metrics: MetricsRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Resp:
        status_code = 404
        content = b""

    client = MagicMock()
    client.get = AsyncMock(return_value=_Resp())
    monkeypatch.setattr(
        "awareness.sources.gdelt.get_shared_async_client",
        AsyncMock(return_value=client),
    )

    adapter = GdeltAdapter()
    ctx = _ctx()
    part = PartitionSpec(
        source_type=SourceKind.GDELT,
        partition_key="gdelt:gkg:20260601113000",
        payload={"slot": "20260601113000"},
    )
    async for _ in adapter.run_partition(part, ctx):
        pass

    assert metrics.counter_value(
        "gdelt.fetch_attempts", labels={"outcome": "missing", "status_class": "4xx"}
    ) == 1.0
    assert metrics.counter_sum("gdelt.urls_discovered") == 0.0
    assert not (ctx.extras.get("enqueue") or [])


@pytest.mark.asyncio
async def test_run_partition_transport_error(
    metrics: MetricsRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = MagicMock()
    client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
    monkeypatch.setattr(
        "awareness.sources.gdelt.get_shared_async_client",
        AsyncMock(return_value=client),
    )

    adapter = GdeltAdapter()
    ctx = _ctx()
    part = PartitionSpec(
        source_type=SourceKind.GDELT,
        partition_key="gdelt:gkg:20260601113000",
        payload={"slot": "20260601113000"},
    )
    async for _ in adapter.run_partition(part, ctx):
        pass

    assert metrics.counter_value(
        "gdelt.fetch_attempts",
        labels={"outcome": "transport_error", "status_class": "transport"},
    ) == 1.0


@pytest.mark.asyncio
async def test_run_partition_bad_zip_extract_error(
    metrics: MetricsRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Resp:
        status_code = 200
        content = b"definitely-not-zip"

    client = MagicMock()
    client.get = AsyncMock(return_value=_Resp())
    monkeypatch.setattr(
        "awareness.sources.gdelt.get_shared_async_client",
        AsyncMock(return_value=client),
    )

    adapter = GdeltAdapter()
    ctx = _ctx()
    part = PartitionSpec(
        source_type=SourceKind.GDELT,
        partition_key="gdelt:gkg:20260601113000",
        payload={"slot": "20260601113000"},
    )
    async for _ in adapter.run_partition(part, ctx):
        pass

    assert metrics.counter_sum("gdelt.extract_errors") == 1.0
    assert metrics.counter_sum("gdelt.urls_discovered") == 0.0
