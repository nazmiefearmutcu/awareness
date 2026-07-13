"""URL fetch gate: skip tail_recrawl HTTP when canonical URL already fetched."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from awareness.normalize.html import HtmlExtraction
from awareness.normalize.text import NormalizedText
from awareness.schemas.doc import SourceKind
from awareness.sources.base import AdapterContext, PartitionSpec
from awareness.sources.tail_recrawl import TailRecrawlAdapter
from awareness.storage.state import StateDB
from awareness.util.urls import canonical_url


def _state(tmp_path) -> StateDB:
    state = StateDB(f"sqlite:///{tmp_path / 'url_fetch.db'}")
    state.init()
    return state


class _FakeLimiter:
    def domain(self, dom: str, override_delay: float | None = None) -> _FakeLimiter:
        return self

    async def __aenter__(self) -> _FakeLimiter:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _FakeRobots:
    async def is_allowed(self, url: str, ua: str) -> bool:
        return True

    def crawl_delay(self, url: str) -> float | None:
        return None


def _context(state: StateDB | None) -> AdapterContext:
    extras: dict[str, Any] = {
        "limiter": _FakeLimiter(),
        "robots": _FakeRobots(),
    }
    if state is not None:
        extras["state"] = state
    return AdapterContext(
        user_agent="test-ua",
        job_id="job-1",
        task_id="task-1",
        batch_id="b1",
        ingest_version="0",
        checkpoint={},
        is_stopping=lambda: False,
        extras=extras,
    )


def _partition(url: str, **payload_extra: Any) -> PartitionSpec:
    payload = {"url": url, "discovery_channel": "test"}
    payload.update(payload_extra)
    return PartitionSpec(
        source_type=SourceKind.TAIL_RECRAWL,
        partition_key=f"tail:{canonical_url(url) or url}",
        payload=payload,
    )


def _fake_extraction(url: str) -> HtmlExtraction:
    body = "word " * 80  # comfortably over min extraction length
    return HtmlExtraction(
        text=NormalizedText(text=body, n_chars=len(body), n_words=80, n_lines=1),
        title="Example Story",
        published_ts=None,
        canonical_url_hint=url,
        language_hint="en",
        raw_metadata={},
    )


def _ok_response(url: str) -> httpx.Response:
    request = httpx.Request("GET", url)
    return httpx.Response(
        200,
        text="<html><body><p>" + ("news " * 100) + "</p></body></html>",
        headers={"Content-Type": "text/html; charset=utf-8"},
        request=request,
    )


async def _collect(
    adapter: TailRecrawlAdapter,
    partition: PartitionSpec,
    ctx: AdapterContext,
    *,
    get_mock: AsyncMock,
) -> list[Any]:
    with (
        patch("awareness.sources.tail_recrawl.is_public_http_url", return_value=True),
        patch("awareness.sources.tail_recrawl._get_public_url", get_mock),
        patch(
            "awareness.sources.tail_recrawl.html_to_text",
            side_effect=lambda html, url=None: _fake_extraction(url or partition.payload["url"]),
        ),
    ):
        out = []
        async for cap in adapter.run_partition(partition, ctx):
            out.append(cap)
        return out


def test_record_url_fetch_then_was_url_fetched(tmp_path) -> None:
    state = _state(tmp_path)
    cu = "https://news.example.com/story/1"
    assert state.was_url_fetched(cu) is False
    assert state.get_url_fetch(cu) is None

    state.record_url_fetch(cu, doc_id="doc-abc", content_hash="hash-xyz", http_status=200)

    assert state.was_url_fetched(cu) is True
    row = state.get_url_fetch(cu)
    assert row is not None
    assert row.canonical_url == cu
    assert row.first_doc_id == "doc-abc"
    assert row.last_content_hash == "hash-xyz"
    assert row.http_status == 200
    assert row.fetched_at is not None

    # Upsert keeps first_doc_id, updates hash.
    state.record_url_fetch(cu, doc_id="doc-other", content_hash="hash-new", http_status=200)
    row2 = state.get_url_fetch(cu)
    assert row2 is not None
    assert row2.first_doc_id == "doc-abc"
    assert row2.last_content_hash == "hash-new"


@pytest.mark.asyncio
async def test_second_run_skips_http_for_same_url(tmp_path) -> None:
    state = _state(tmp_path)
    adapter = TailRecrawlAdapter()
    url = "https://news.example.com/article/42"
    get_mock = AsyncMock(side_effect=lambda client, u, **kw: _ok_response(u))

    caps1 = await _collect(adapter, _partition(url), _context(state), get_mock=get_mock)
    assert len(caps1) == 1
    assert get_mock.await_count == 1
    assert state.was_url_fetched(canonical_url(url) or url)

    caps2 = await _collect(adapter, _partition(url), _context(state), get_mock=get_mock)
    assert caps2 == []
    assert get_mock.await_count == 1  # no second HTTP GET


@pytest.mark.asyncio
async def test_different_urls_still_fetch(tmp_path) -> None:
    state = _state(tmp_path)
    adapter = TailRecrawlAdapter()
    url_a = "https://news.example.com/a"
    url_b = "https://news.example.com/b"
    get_mock = AsyncMock(side_effect=lambda client, u, **kw: _ok_response(u))

    caps_a = await _collect(adapter, _partition(url_a), _context(state), get_mock=get_mock)
    caps_b = await _collect(adapter, _partition(url_b), _context(state), get_mock=get_mock)

    assert len(caps_a) == 1
    assert len(caps_b) == 1
    assert get_mock.await_count == 2
    assert state.was_url_fetched(canonical_url(url_a) or url_a)
    assert state.was_url_fetched(canonical_url(url_b) or url_b)


@pytest.mark.asyncio
async def test_force_refresh_bypasses_gate(tmp_path) -> None:
    state = _state(tmp_path)
    adapter = TailRecrawlAdapter()
    url = "https://news.example.com/refresh-me"
    get_mock = AsyncMock(side_effect=lambda client, u, **kw: _ok_response(u))

    await _collect(adapter, _partition(url), _context(state), get_mock=get_mock)
    assert get_mock.await_count == 1

    caps = await _collect(
        adapter,
        _partition(url, force_refresh=True),
        _context(state),
        get_mock=get_mock,
    )
    assert len(caps) == 1
    assert get_mock.await_count == 2


@pytest.mark.asyncio
async def test_no_state_does_not_gate(tmp_path) -> None:
    """Without state in extras, adapter behaves as before (always fetches)."""
    adapter = TailRecrawlAdapter()
    url = "https://news.example.com/no-state"
    get_mock = AsyncMock(side_effect=lambda client, u, **kw: _ok_response(u))

    await _collect(adapter, _partition(url), _context(None), get_mock=get_mock)
    await _collect(adapter, _partition(url), _context(None), get_mock=get_mock)
    assert get_mock.await_count == 2
