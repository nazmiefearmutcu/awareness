"""Common Crawl WET adapter.

Reads Common Crawl WET (text) shards directly. WET files contain extracted
plaintext for each captured URL plus rich metadata (URL, fetch date, content
type). They are the cheapest path to "historical body" at scale and require
no HTML extraction.

Architecture:
- ``plan(req)`` translates [start, end] into a set of crawl_ids that overlap
  the window, then enumerates WET shards via the canonical
  ``wet.paths.gz`` index file for each crawl.
- ``run_partition(partition)`` streams a single WET file with ``warcio`` one
  record at a time (``ArchiveIterator``), converts each conversion record to a
  ``DocCapture``, and yields through a bounded ``asyncio.Queue`` sized by
  ``settings.bounded_queue_size``. Peak in-flight captures are O(queue depth),
  not O(shard size); the consumer applies backpressure so the parser cannot
  race ahead and materialize the whole shard as a list.
- Checkpoint stores ``last_offset`` or ``last_record_id`` so re-runs resume.

We use the official ``s3://commoncrawl/...`` paths over HTTPS:
``https://data.commoncrawl.org/<path>``. No AWS credentials needed.

Crawl-id ↔ time mapping:
- Crawls are named ``CC-MAIN-YYYY-WW`` (ISO year + ISO week).
- Each crawl runs over ~2 weeks. We map [start, end] to a list of crawl_ids by
  enumerating ISO weeks in the range. If a crawl_id doesn't exist (planned
  but not published), we skip it on first fetch failure.

This file is intentionally framework-friendly: the actual WET fetch happens
in ``run_partition`` so the planner stays cheap.
"""

from __future__ import annotations

import asyncio
import gzip
import threading
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from awareness.config import get_settings
from awareness.normalize.quality import gopher_quality
from awareness.normalize.text import detect_language, normalize_text, safe_title
from awareness.obs.logging import get_logger
from awareness.obs.metrics import get_metrics
from awareness.schemas.doc import DocCapture, RobotsDecision, SourceKind, SourceRef
from awareness.schemas.jobs import BackfillRequest
from awareness.sources.base import Adapter, AdapterContext, PartitionSpec
from awareness.util.hashing import (
    capture_id_for,
    doc_id_for,
    simhash64,
)
from awareness.util.hashing import (
    content_hash as compute_content_hash,
)
from awareness.util.timeutil import to_utc, utcnow
from awareness.util.urls import canonical_url, domain_of

logger = get_logger("sources.cc_wet")

CC_BASE = "https://data.commoncrawl.org"


def _iso_year_weeks(start: datetime, end: datetime) -> list[tuple[int, int]]:
    """Return ISO (year, week) tuples covering ``[start, end]``."""
    cur = to_utc(start) or utcnow()
    end_utc = to_utc(end) or utcnow()
    if cur > end_utc:
        cur, end_utc = end_utc, cur
    seen: list[tuple[int, int]] = []
    last_pair: tuple[int, int] | None = None
    while cur <= end_utc:
        iso = cur.isocalendar()
        pair = (iso.year, iso.week)
        if pair != last_pair:
            seen.append(pair)
            last_pair = pair
        cur += timedelta(days=1)
    return seen


def crawl_ids_for_range(start: datetime, end: datetime) -> list[str]:
    """Convert a date range to candidate crawl_ids like ``CC-MAIN-2024-26``."""
    pairs = _iso_year_weeks(start, end)
    # Common Crawl crawls span ~2 weeks; we coalesce to even-week starts.
    seen: set[tuple[int, int]] = set()
    out: list[str] = []
    for year, week in pairs:
        anchor_week = week if week % 2 == 1 else week - 1
        anchor_week = max(anchor_week, 1)
        key = (year, anchor_week)
        if key in seen:
            continue
        seen.add(key)
        out.append(f"CC-MAIN-{year}-{anchor_week:02d}")
    return out


def _normalize_domain_filter(domains: list[str] | None) -> set[str] | None:
    """Reduce requested domains to their registered eTLD+1 so a subdomain
    request (news.bbc.co.uk) matches records whose domain_of is bbc.co.uk."""
    if not domains:
        return None
    normalized = {domain_of(d) or domain_of(f"http://{d}") or d.lower() for d in domains}
    return {d for d in normalized if d} or None


def _record_passes_domain_filter(url: str, domains_filter: set[str] | None) -> bool:
    if not domains_filter:
        return True
    cu = canonical_url(url)
    dom = domain_of(cu) if cu else None
    return dom in domains_filter


def _record_passes_quality(text: str, *, enabled: bool, lang: str | None = None) -> bool:
    """WET records below Gopher/C4 content quality are dropped when ``enabled``.

    English-leaning Gopher gates only judge English; a record admitted in
    another language passes through unjudged (no silent data loss for
    non-English WET text).
    """
    if not enabled:
        return True
    if lang is not None and not str(lang).lower().startswith("en"):
        return True
    return gopher_quality(text).ok


class CommonCrawlWetAdapter(Adapter):
    source_type = SourceKind.COMMON_CRAWL_WET

    def __init__(self, max_shards_per_crawl: int = 4) -> None:
        super().__init__()
        # Default to 4 shards per crawl (configurable via AW_CC_WET_MAX_SHARDS_PER_CRAWL).
        # CLI/config can override via the ``BackfillRequest.notes`` payload or per-partition.
        self._max_shards_per_crawl = max(1, max_shards_per_crawl)

    # ── planner ──────────────────────────────────────────────────────────
    def plan(self, request: BackfillRequest) -> list[PartitionSpec]:
        crawls = crawl_ids_for_range(request.start, request.end)
        partitions: list[PartitionSpec] = []
        for crawl_id in crawls:
            partitions.append(
                PartitionSpec(
                    source_type=self.source_type,
                    partition_key=f"{crawl_id}:wet-paths",
                    payload={
                        "kind": "shard-discovery",
                        "crawl_id": crawl_id,
                        "max_shards": self._max_shards_per_crawl,
                        "domains": request.domains,
                        "languages": request.languages,
                    },
                )
            )
        return partitions

    # ── runner ───────────────────────────────────────────────────────────
    async def run_partition(
        self,
        partition: PartitionSpec,
        context: AdapterContext,
    ) -> AsyncIterator[DocCapture]:
        kind = partition.payload.get("kind")
        if kind == "shard-discovery":
            async for cap in self._run_discovery(partition, context):
                yield cap
        elif kind == "shard-fetch":
            async for cap in self._run_shard(partition, context):
                yield cap
        else:
            logger.warning("cc_wet_unknown_partition", kind=kind)

    async def _run_discovery(
        self,
        partition: PartitionSpec,
        context: AdapterContext,
    ) -> AsyncIterator[DocCapture]:
        """Fetch the crawl's wet.paths.gz, enqueue shard partitions, yield nothing.

        Because the adapter contract is to *yield captures*, but discovery
        emits sub-partitions, we store the discovered shards in the worker
        extras via ``context.extras["enqueue"]``. The worker reads them.
        """
        crawl_id = partition.payload["crawl_id"]
        max_shards = int(partition.payload.get("max_shards", 1))
        url = f"{CC_BASE}/crawl-data/{crawl_id}/wet.paths.gz"
        logger.info("cc_wet_discovery_start", crawl_id=crawl_id, url=url)

        from awareness.util.http import get_with_retries  # noqa: PLC0415

        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            # Transient failures raise RetryableHTTPError (task retries with
            # backoff); a genuine 404 means this crawl has no wet.paths — skip.
            resp = await get_with_retries(
                client, url, headers={"User-Agent": context.user_agent}
            )
            if resp.status_code != 200:
                logger.info("cc_wet_paths_not_found", crawl_id=crawl_id, status=resp.status_code)
                return
            try:
                body = gzip.decompress(resp.content).decode("utf-8", "replace")
            except OSError as exc:
                logger.warning("cc_wet_paths_decode_failed", crawl_id=crawl_id, err=str(exc))
                return

        shards = [line.strip() for line in body.splitlines() if line.strip()]
        chosen = shards[:max_shards]
        get_metrics().inc("cc_wet.shards_discovered", value=len(shards), labels={"crawl_id": crawl_id})
        get_metrics().inc("cc_wet.shards_enqueued", value=len(chosen), labels={"crawl_id": crawl_id})

        enqueue = context.extras.setdefault("enqueue", [])
        for shard in chosen:
            enqueue.append(
                PartitionSpec(
                    source_type=self.source_type,
                    partition_key=f"{crawl_id}:wet:{shard.split('/')[-1]}",
                    payload={
                        "kind": "shard-fetch",
                        "crawl_id": crawl_id,
                        "shard_path": shard,
                        "domains": partition.payload.get("domains"),
                        "languages": partition.payload.get("languages"),
                    },
                )
            )
        return
        if False:  # pragma: no cover
            yield

    async def _run_shard(
        self,
        partition: PartitionSpec,
        context: AdapterContext,
    ) -> AsyncIterator[DocCapture]:
        crawl_id = partition.payload["crawl_id"]
        shard_path = partition.payload["shard_path"]
        domains_filter = _normalize_domain_filter(partition.payload.get("domains"))
        languages_filter = set(partition.payload.get("languages") or []) or None

        url = f"{CC_BASE}/{shard_path}"
        logger.info("cc_wet_shard_start", crawl_id=crawl_id, shard=shard_path, url=url)

        settings = get_settings()
        # Stream the shard to a local file, then parse with warcio. WET files
        # are typically 100-500 MB so streaming-to-disk is the cheap path.
        assert settings.data_dir is not None
        cache_dir = settings.warc_cache_dir or settings.data_dir / "warc"
        cache_dir.mkdir(parents=True, exist_ok=True)
        local = cache_dir / shard_path.replace("/", "_")
        if not local.exists():
            try:
                async with httpx.AsyncClient(timeout=180.0, follow_redirects=True) as client:
                    async with client.stream(
                        "GET", url, headers={"User-Agent": context.user_agent}
                    ) as resp:
                        if resp.status_code != 200:
                            logger.warning(
                                "cc_wet_shard_not_found",
                                crawl_id=crawl_id,
                                shard=shard_path,
                                status=resp.status_code,
                            )
                            return
                        tmp = local.with_suffix(local.suffix + ".tmp")
                        with open(tmp, "wb") as fh:  # noqa: ASYNC230
                            async for chunk in resp.aiter_bytes(1 << 20):
                                if context.is_stopping():
                                    fh.close()
                                    tmp.unlink(missing_ok=True)
                                    return
                                fh.write(chunk)
                        tmp.rename(local)
                logger.info("cc_wet_shard_cached", path=str(local))
            except httpx.HTTPError as exc:
                logger.warning("cc_wet_shard_download_failed", err=str(exc))
                return

        # warcio import check off the event loop; parse streams on a worker
        # thread into a bounded queue (never materialize the full shard).
        await asyncio.get_running_loop().run_in_executor(None, _ensure_warcio_available)

        async for cap in _stream_wet_captures(
            path=local,
            crawl_id=crawl_id,
            shard_path=shard_path,
            domains_filter=domains_filter,
            languages_filter=languages_filter,
            user_agent=context.user_agent,
            job_id=context.job_id,
            task_id=context.task_id,
            batch_id=context.batch_id,
            ingest_version=context.ingest_version,
            is_stopping=context.is_stopping,
            queue_maxsize=max(1, int(settings.bounded_queue_size)),
        ):
            yield cap


def _ensure_warcio_available() -> None:
    import warcio  # noqa: F401, PLC0415


def _iter_wet_captures(
    path: Path,
    crawl_id: str,
    shard_path: str,
    domains_filter: set[str] | None,
    languages_filter: set[str] | None,
    user_agent: str,
    job_id: str,
    task_id: str,
    batch_id: str,
    ingest_version: str,
    *,
    should_stop: threading.Event | None = None,
) -> Iterator[DocCapture]:
    """Yield ``DocCapture`` one WARC conversion record at a time.

    Uses ``warcio.ArchiveIterator`` over a file handle so the WET/WARC body is
    never loaded wholesale. Each record's payload is read, converted, and
    yielded; the caller (bounded queue consumer) decides retention. No list of
    captures is accumulated here.
    """
    from warcio.archiveiterator import ArchiveIterator  # noqa: PLC0415

    settings = get_settings()
    seen_in_shard = 0
    captures_emitted = 0

    with open(path, "rb") as fh:
        for record in ArchiveIterator(fh):
            if should_stop is not None and should_stop.is_set():
                break
            seen_in_shard += 1
            if record.rec_type != "conversion":
                continue
            url = record.rec_headers.get_header("WARC-Target-URI")
            if not url:
                continue
            cu = canonical_url(url)
            dom = domain_of(cu) if cu else None
            if domains_filter and dom not in domains_filter:
                continue
            try:
                raw = record.content_stream().read()
            except (OSError, ValueError):
                continue
            try:
                text_raw = raw.decode("utf-8", "replace")
            except (UnicodeDecodeError, AttributeError):
                continue
            # Free the raw bytes as soon as we have text; text_raw may still be
            # large but is one record, not the whole shard.
            del raw
            norm = normalize_text(
                text_raw,
                min_chars=settings.text_min_chars,
                max_chars=settings.text_max_chars,
            )
            del text_raw
            if norm.discarded_reason:
                continue
            lang = detect_language(norm.text) or None
            if languages_filter and lang not in languages_filter:
                continue
            # Quality gating runs DOWNSTREAM of language selection so the
            # English-leaning Gopher gates only judge text the language filter
            # has already admitted (see normalize/quality.py docstring).
            if not _record_passes_quality(
                norm.text, enabled=settings.wet_quality_filter, lang=lang
            ):
                get_metrics().inc("cc_wet.quality_filtered", labels={"crawl_id": crawl_id})
                continue

            ch = compute_content_hash(norm.text)
            sim = simhash64(norm.text)
            fetched_at = record.rec_headers.get_header("WARC-Date") or ""
            fetch_ts = to_utc(fetched_at) or utcnow()
            observed_ts = utcnow()
            record_id = record.rec_headers.get_header("WARC-Record-ID") or ""

            did = doc_id_for(cu, ch)
            cap = DocCapture(
                doc_id=did,
                capture_id=capture_id_for(did, observed_ts.isoformat(), shard_path),
                source=SourceRef(
                    source_type=SourceKind.COMMON_CRAWL_WET,
                    source_name=crawl_id,
                    source_locator=f"{CC_BASE}/{shard_path}",
                    source_shard=shard_path,
                    source_offset_or_record_id=record_id,
                ),
                discovery_channel=f"cc-wet:{crawl_id}",
                job_id=job_id,
                batch_id=batch_id,
                ingest_version=ingest_version,
                url=url,
                canonical_url=cu,
                domain=dom,
                fetch_ts=fetch_ts,
                observed_ts=observed_ts,
                title=safe_title(None, norm.text),
                text=norm.text,
                language=lang,
                content_hash=ch,
                near_dup_hash=sim,
                robots_decision=RobotsDecision.NOT_APPLICABLE,  # bulk corpus
                content_type="text/plain",
                http_status=200,
            )
            captures_emitted += 1
            yield cap

    logger.info(
        "cc_wet_shard_parsed",
        crawl_id=crawl_id,
        shard=shard_path,
        records_seen=seen_in_shard,
        captures_emitted=captures_emitted,
    )


def _parse_wet_to_captures(
    path: Path,
    crawl_id: str,
    shard_path: str,
    domains_filter: set[str] | None,
    languages_filter: set[str] | None,
    user_agent: str,
    job_id: str,
    task_id: str,
    batch_id: str,
    ingest_version: str,
) -> list[DocCapture]:
    """Collect all captures from a WET file (tests / sync callers).

    Production shard execution uses :func:`_stream_wet_captures` so memory stays
    bounded by ``settings.bounded_queue_size`` rather than shard length.
    """
    return list(
        _iter_wet_captures(
            path,
            crawl_id,
            shard_path,
            domains_filter,
            languages_filter,
            user_agent,
            job_id,
            task_id,
            batch_id,
            ingest_version,
        )
    )


async def _stream_wet_captures(
    *,
    path: Path,
    crawl_id: str,
    shard_path: str,
    domains_filter: set[str] | None,
    languages_filter: set[str] | None,
    user_agent: str,
    job_id: str,
    task_id: str,
    batch_id: str,
    ingest_version: str,
    is_stopping,
    queue_maxsize: int = 1024,
) -> AsyncIterator[DocCapture]:
    """Parse a WET shard on a worker thread; yield via a bounded asyncio queue.

    The producer thread iterates warcio records and ``put``s each capture into
    ``asyncio.Queue(maxsize=queue_maxsize)``. A full queue blocks the producer
    (via ``run_coroutine_threadsafe(...).result()``), so in-flight
    ``DocCapture`` objects never exceed the queue depth plus one in-flight put.
    ``None`` is the end sentinel. Early stop drains the queue so the producer
    cannot hang on a blocked put.
    """
    queue: asyncio.Queue[DocCapture | None] = asyncio.Queue(maxsize=max(1, queue_maxsize))
    loop = asyncio.get_running_loop()
    stop = threading.Event()
    errors: list[BaseException] = []

    def _produce() -> None:
        try:
            for cap in _iter_wet_captures(
                path,
                crawl_id,
                shard_path,
                domains_filter,
                languages_filter,
                user_agent,
                job_id,
                task_id,
                batch_id,
                ingest_version,
                should_stop=stop,
            ):
                if stop.is_set():
                    break
                # Backpressure: block until the async consumer drains a slot.
                fut = asyncio.run_coroutine_threadsafe(queue.put(cap), loop)
                fut.result()
        except BaseException as exc:
            errors.append(exc)
        finally:
            try:
                asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()
            except Exception as exc:  # loop may already be closed on shutdown
                logger.debug("cc_wet_stream_sentinel_failed", err=str(exc))

    producer = loop.run_in_executor(None, _produce)
    try:
        while True:
            if is_stopping():
                stop.set()
                # Drain so a blocked producer put can complete, then exit.
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                break
            item = await queue.get()
            if item is None:
                break
            yield item
    finally:
        stop.set()
        # If the consumer aborted (GeneratorExit / cancel), keep draining so
        # the producer is not stuck forever on a full queue.
        if not producer.done():
            while not producer.done():
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=0.05)
                except TimeoutError:
                    continue
                if item is None:
                    break
        await producer

    if errors:
        raise errors[0]
