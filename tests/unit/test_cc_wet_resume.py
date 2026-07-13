"""Mid-shard resume for Common Crawl WET via checkpoint last_record_id/offset."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from warcio.warcwriter import WARCWriter

from awareness.config.settings import reset_settings
from awareness.schemas.doc import SourceKind
from awareness.sources.base import AdapterContext, PartitionSpec
from awareness.sources.commoncrawl_wet import (
    CommonCrawlWetAdapter,
    _iter_wet_captures,
    _resume_cursors,
)

_PROSE = (
    "This is a multi-record WET streaming fixture paragraph with the common "
    "English stopwords the of and to with that have be for quality admission. "
    "Hello world from the test suite with enough extra filler words here now yes "
    "so that normalization and language detection both admit the capture body. "
)


def _write_multi_record_wet(path: Path, n: int) -> tuple[list[str], list[str]]:
    """Write ``n`` conversion records; return (urls, warc_record_ids) in order."""
    urls: list[str] = []
    record_ids: list[str] = []
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
            rid = rec.rec_headers.get_header("WARC-Record-ID") or ""
            record_ids.append(rid)
            writer.write_record(rec)
    return urls, record_ids


def _iter_kwargs(path: Path, checkpoint: dict | None = None) -> dict:
    return dict(
        path=path,
        crawl_id="CC-MAIN-2026-06",
        shard_path="crawl-data/CC-MAIN-2026-06/segments/x/wet/multi.warc.wet.gz",
        domains_filter=None,
        languages_filter=None,
        user_agent="test-agent",
        job_id="job",
        task_id="task",
        batch_id="batch",
        ingest_version="v1",
        checkpoint=checkpoint,
    )


def test_resume_cursors_prefers_record_id() -> None:
    rid, off = _resume_cursors({"last_record_id": "<urn:uuid:abc>", "last_offset": 3})
    assert rid == "<urn:uuid:abc>"
    assert off == 3
    assert _resume_cursors({}) == (None, None)
    assert _resume_cursors({"last_offset": "2"}) == (None, 2)


def test_iter_updates_checkpoint_last_record_id_and_offset(tmp_path: Path) -> None:
    reset_settings()
    wet = tmp_path / "multi.warc"
    urls, record_ids = _write_multi_record_wet(wet, 3)
    checkpoint: dict = {}

    caps = [cap for cap, _off in _iter_wet_captures(**_iter_kwargs(wet, checkpoint))]
    assert [c.url for c in caps] == urls
    assert checkpoint["last_record_id"] == record_ids[-1]
    assert checkpoint["last_offset"] == 3


def test_iter_resumes_after_last_record_id(tmp_path: Path) -> None:
    reset_settings()
    wet = tmp_path / "multi.warc"
    urls, record_ids = _write_multi_record_wet(wet, 4)

    checkpoint = {"last_record_id": record_ids[1], "last_offset": 2}
    caps = [cap for cap, _off in _iter_wet_captures(**_iter_kwargs(wet, checkpoint))]
    assert [c.url for c in caps] == urls[2:]
    assert checkpoint["last_record_id"] == record_ids[-1]
    assert checkpoint["last_offset"] == 4


def test_iter_resumes_after_last_offset_only(tmp_path: Path) -> None:
    reset_settings()
    wet = tmp_path / "multi.warc"
    urls, _record_ids = _write_multi_record_wet(wet, 4)

    checkpoint = {"last_offset": 2}
    caps = [cap for cap, _off in _iter_wet_captures(**_iter_kwargs(wet, checkpoint))]
    assert [c.url for c in caps] == urls[2:]


@pytest.mark.asyncio
async def test_adapter_run_partition_resumes_mid_shard(
    tmp_project: Path,
) -> None:
    reset_settings()
    from awareness.config import get_settings

    settings = get_settings()
    cache_dir = settings.warc_cache_dir or settings.data_dir / "warc"
    cache_dir.mkdir(parents=True, exist_ok=True)
    shard_path = "crawl-data/CC-MAIN-2024-26/segments/123/wet/resume.gz"
    local_file = cache_dir / shard_path.replace("/", "_")
    urls, record_ids = _write_multi_record_wet(local_file, 4)

    adapter = CommonCrawlWetAdapter()
    partition = PartitionSpec(
        source_type=SourceKind.COMMON_CRAWL_WET,
        partition_key="CC-MAIN-2024-26:wet:resume",
        payload={
            "kind": "shard-fetch",
            "crawl_id": "CC-MAIN-2024-26",
            "shard_path": shard_path,
        },
    )

    # First pass: stop after two captures (simulate cooperative partial run).
    checkpoint: dict = {}
    stop_after = {"n": 0}

    def is_stopping() -> bool:
        return stop_after["n"] >= 2

    context = AdapterContext(
        user_agent="TestAgent",
        job_id="job-resume",
        task_id="task-resume-1",
        batch_id="batch-1",
        ingest_version="0.2.0",
        checkpoint=checkpoint,
        is_stopping=is_stopping,
    )
    first: list[str] = []
    async for cap in adapter.run_partition(partition, context):
        first.append(cap.url)
        stop_after["n"] += 1

    assert first == urls[:2]
    assert checkpoint.get("last_record_id") == record_ids[1]
    assert checkpoint.get("last_offset") == 2

    # Restart with the saved checkpoint: remaining records only.
    context2 = AdapterContext(
        user_agent="TestAgent",
        job_id="job-resume",
        task_id="task-resume-2",
        batch_id="batch-2",
        ingest_version="0.2.0",
        checkpoint=dict(checkpoint),
        is_stopping=lambda: False,
    )
    rest: list[str] = []
    async for cap in adapter.run_partition(partition, context2):
        rest.append(cap.url)

    assert rest == urls[2:]
    assert context2.checkpoint["last_record_id"] == record_ids[-1]
    assert context2.checkpoint["last_offset"] == 4
