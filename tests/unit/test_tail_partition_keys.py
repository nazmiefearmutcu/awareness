"""Tail recrawl partition keys must be source-agnostic.

RSS and GDELT both discover article URLs and enqueue TAIL_RECRAWL tasks.
UNIQUE(job_id, partition_key) is the only gate against double HTTP fetch, so
every discovery channel MUST emit:

    tail:{canonical_url(u)}

not channel-prefixed keys like ``tail-gdelt:...``.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from awareness.schemas.doc import SourceKind
from awareness.sources.base import AdapterContext, PartitionSpec
from awareness.sources.feeds import FeedsAdapter
from awareness.sources.gdelt import GdeltAdapter
from awareness.util.urls import canonical_url


def _tail_key(url: str) -> str:
    """Canonical form both adapters must produce."""
    cu = canonical_url(url)
    assert cu is not None
    return f"tail:{cu}"


def _fake_gkg_zip(urls: list[str]) -> bytes:
    """Minimal GKG-shaped TSV zip: DOCUMENTIDENTIFIER is column index 4."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        rows = []
        for u in urls:
            # 5+ tab-separated fields; index 4 is the article URL.
            row = ["x", "y", "z", "w", u, "extra"]
            rows.append("\t".join(row))
        zf.writestr("20260601113000.gkg.csv", "\n".join(rows) + "\n")
    return buf.getvalue()


def _adapter_context() -> AdapterContext:
    return AdapterContext(
        user_agent="test-ua",
        job_id="job-1",
        task_id="task-1",
        batch_id="b1",
        ingest_version="0",
        checkpoint={},
        is_stopping=lambda: False,
        extras={},
    )


async def _run_gdelt_with_urls(urls: list[str], *, max_urls: int | None = None) -> list[PartitionSpec]:
    zipped = _fake_gkg_zip(urls)
    adapter = GdeltAdapter()
    payload: dict[str, Any] = {"slot": "20260601113000"}
    if max_urls is not None:
        payload["max_urls"] = max_urls
    partition = PartitionSpec(
        source_type=SourceKind.GDELT,
        partition_key="gdelt:gkg:20260601113000",
        payload=payload,
    )
    ctx = _adapter_context()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = zipped

    client = AsyncMock()
    client.get = AsyncMock(return_value=mock_response)
    with patch(
        "awareness.sources.gdelt.get_shared_async_client",
        new=AsyncMock(return_value=client),
    ):
        async for _ in adapter.run_partition(partition, ctx):
            pass
    return list(ctx.extras.get("enqueue", []))


def test_canonical_tail_key_strips_tracking_params() -> None:
    raw = "https://News.Example.COM/story?id=1&utm_source=gdelt&fbclid=abc#frag"
    clean = "https://news.example.com/story?id=1"
    assert canonical_url(raw) == clean
    assert _tail_key(raw) == f"tail:{clean}"
    assert _tail_key(raw) == _tail_key(clean)


def test_feed_and_gdelt_style_keys_match_for_same_url() -> None:
    """Same article URL from RSS vs GDELT must collapse under UNIQUE(job_id, pk)."""
    url = "https://www.example.com/article/42?utm_campaign=rss"
    # feeds.py: f"tail:{canonical_url(u)}"
    feed_key = f"tail:{canonical_url(url)}"
    # After C1-T3, GDELT uses the same formula (not tail-gdelt:raw).
    gdelt_key = f"tail:{canonical_url(url)}"
    assert feed_key == gdelt_key
    assert feed_key.startswith("tail:")
    assert not feed_key.startswith("tail-gdelt:")
    assert "utm_campaign" not in feed_key


def test_feeds_and_gdelt_formulas_equal_after_host_and_query_normalize() -> None:
    article = "HTTPS://News.Example.COM/world/1?utm_medium=feed&ref=home"
    feed_key = f"tail:{canonical_url(article)}"
    gdelt_key = f"tail:{canonical_url(article)}"
    assert feed_key == gdelt_key == "tail:https://news.example.com/world/1"
    # FeedsAdapter is still the RSS discovery path (key formula only).
    assert FeedsAdapter.source_type == SourceKind.RSS


@pytest.mark.asyncio
async def test_gdelt_adapter_emits_tail_not_tail_gdelt_keys() -> None:
    """GdeltAdapter.run_partition must enqueue tail:{canonical} keys only."""
    raw_url = "https://Example.COM/a?utm_source=twitter"
    enqueued = await _run_gdelt_with_urls([raw_url])

    assert enqueued, "GDELT should enqueue at least one tail_recrawl partition"
    for spec in enqueued:
        assert isinstance(spec, PartitionSpec)
        assert spec.source_type == SourceKind.TAIL_RECRAWL
        assert spec.partition_key.startswith("tail:")
        assert not spec.partition_key.startswith("tail-gdelt:")
        assert "utm_source" not in spec.partition_key
        # Provenance preserved in payload, not the partition key.
        assert spec.payload.get("source_kind") == "gdelt"
        assert str(spec.payload.get("discovery_channel", "")).startswith("gdelt:")
        assert spec.payload.get("url") == raw_url

    expected = f"tail:{canonical_url(raw_url)}"
    assert enqueued[0].partition_key == expected
    # Cross-source equality with feed-style construction.
    assert enqueued[0].partition_key == f"tail:{canonical_url(raw_url)}"


@pytest.mark.asyncio
async def test_gdelt_canonicalizes_and_skips_non_http() -> None:
    raw_url = "http://Example.COM:80/path?b=2&a=1&utm_id=9"
    enqueued = await _run_gdelt_with_urls(
        [raw_url, "not-a-url", "ftp://skip.me/x"],
        max_urls=10,
    )
    # Only the http(s) URL makes it into the zip extract; ftp is rejected at extract.
    # Canonical key must match feeds for the same article.
    assert len(enqueued) == 1
    pk = enqueued[0].partition_key
    # http→https identity upgrade + port/trackers stripped, query sorted.
    assert pk == "tail:https://example.com/path?a=1&b=2"
    assert "tail-gdelt" not in pk
    assert pk == f"tail:{canonical_url(raw_url)}"
