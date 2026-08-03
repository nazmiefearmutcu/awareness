"""WARC targeted repair adapter — fetch one WARC record by byte range and extract."""

from __future__ import annotations

import asyncio
import io
import time
from collections.abc import AsyncIterator

import httpx

from awareness.config import get_settings
from awareness.normalize.html import html_to_text
from awareness.normalize.text import detect_language
from awareness.obs.logging import get_logger
from awareness.obs.metrics import get_metrics
from awareness.schemas.doc import DocCapture, RobotsDecision, SourceKind, SourceRef
from awareness.schemas.jobs import BackfillRequest
from awareness.sources.base import Adapter, AdapterContext, PartitionSpec
from awareness.sources.commoncrawl_wet import CC_BASE
from awareness.util.hashing import (
    capture_id_for,
    doc_id_for,
    simhash64,
)
from awareness.util.hashing import (
    content_hash as compute_content_hash,
)
from awareness.util.http import (
    RETRYABLE_STATUS,
    RetryableHTTPError,
    acquire_fetch_slot,
    get_shared_async_client,
)
from awareness.util.timeutil import to_utc, utcnow
from awareness.util.urls import canonical_url, domain_of

logger = get_logger("sources.warc_repair")


class WarcRepairAdapter(Adapter):
    source_type = SourceKind.COMMON_CRAWL_WARC

    def plan(self, request: BackfillRequest) -> list[PartitionSpec]:
        # Repair is reactive — never planned directly. The CC index adapter
        # enqueues these as sub-partitions.
        return []

    async def run_partition(  # noqa: PLR0915
        self,
        partition: PartitionSpec,
        context: AdapterContext,
    ) -> AsyncIterator[DocCapture]:
        warc_path = partition.payload["warc_path"]
        offset = int(partition.payload["offset"])
        length = int(partition.payload["length"])
        url = partition.payload.get("url")
        crawl_id = partition.payload.get("crawl_id", "")

        end = offset + length - 1
        full_url = f"{CC_BASE}/{warc_path}"
        crawl_label = str(crawl_id or "unknown")
        metrics = get_metrics()
        t_fetch = time.perf_counter()
        fetch_outcome = "ok"
        payload: bytes | None = None
        try:
            # M-04 variant: shared pooled client + process-wide fetch slot
            # (no per-request AsyncClient churn).
            client = await get_shared_async_client(timeout=60.0, follow_redirects=True)
            async with acquire_fetch_slot():
                resp = await client.get(
                    full_url,
                    headers={"Range": f"bytes={offset}-{end}", "User-Agent": context.user_agent},
                )
            if resp.status_code in RETRYABLE_STATUS:
                # M-02: transient status (408/429/5xx) → raise so the task
                # layer retries with backoff instead of skipping the record.
                fetch_outcome = "network_error"
                await resp.aclose()
                raise RetryableHTTPError(f"{full_url} -> {resp.status_code}")
            if resp.status_code in (404, 410):
                # Record gone permanently — skip (no retry).
                fetch_outcome = "http_error"
                logger.warning("warc_range_gone", status=resp.status_code, path=warc_path)
                await resp.aclose()
                return
            if resp.status_code != 206:
                # H-25: only a 206 is a valid byte-range response. A 200 means
                # the server ignored Range and "the first record" of a
                # full-file payload would be the WRONG record — treat as an
                # http error, never parse it.
                fetch_outcome = "http_error"
                logger.warning("warc_range_unexpected", status=resp.status_code, path=warc_path)
                await resp.aclose()
                return
            payload = resp.content
        except httpx.HTTPError as exc:
            # M-02: transient transport failure → raise so the task retries.
            fetch_outcome = "network_error"
            logger.warning("warc_range_exception", err=str(exc))
            raise RetryableHTTPError(f"{full_url} -> {exc}") from exc
        finally:
            fetch_elapsed = max(0.0, time.perf_counter() - t_fetch)
            metrics.inc(
                "warc_repair.fetch_attempts",
                labels={"outcome": fetch_outcome, "crawl_id": crawl_label},
            )
            metrics.observe(
                "warc_repair.fetch_seconds",
                fetch_elapsed,
                labels={"outcome": fetch_outcome},
            )

        if payload is None:
            return

        # Parse the WARC record from the byte range.
        settings = get_settings()
        t_parse = time.perf_counter()
        try:
            cap = await asyncio.get_event_loop().run_in_executor(
                None,
                _parse_warc_record,
                payload,
                warc_path,
                offset,
                url,
                crawl_id,
                context.user_agent,
                context.job_id,
                context.task_id,
                context.batch_id,
                context.ingest_version,
                settings.text_min_chars,
                settings.text_max_chars,
            )
        except Exception as exc:
            # M-04 variant: a parse exception is NOT "empty" — record outcome
            # "error" (distinct) and raise so the task layer retries.
            parse_elapsed = max(0.0, time.perf_counter() - t_parse)
            metrics.inc(
                "warc_repair.parse_attempts",
                labels={"outcome": "error", "crawl_id": crawl_label},
            )
            metrics.observe(
                "warc_repair.parse_seconds",
                parse_elapsed,
                labels={"outcome": "error"},
            )
            logger.warning("warc_parse_failed", err=str(exc), path=warc_path)
            raise RetryableHTTPError(f"warc parse failed for {warc_path}: {exc}") from exc
        parse_elapsed = max(0.0, time.perf_counter() - t_parse)
        parse_outcome = "emitted" if cap is not None else "empty"
        metrics.inc(
            "warc_repair.parse_attempts",
            labels={"outcome": parse_outcome, "crawl_id": crawl_label},
        )
        metrics.observe(
            "warc_repair.parse_seconds",
            parse_elapsed,
            labels={"outcome": parse_outcome},
        )
        if cap is not None:
            metrics.inc("warc_repair.docs_emitted", labels={"crawl_id": crawl_label})
            yield cap


def _parse_warc_record(
    payload: bytes,
    warc_path: str,
    offset: int,
    url: str | None,
    crawl_id: str,
    user_agent: str,
    job_id: str,
    task_id: str,
    batch_id: str,
    ingest_version: str,
    min_chars: int = 200,
    max_chars: int = 1_500_000,
) -> DocCapture | None:
    from warcio.archiveiterator import ArchiveIterator  # noqa: PLC0415

    for record in ArchiveIterator(io.BytesIO(payload)):
        if record.rec_type != "response":
            continue
        target = url or record.rec_headers.get_header("WARC-Target-URI")
        if not target:
            return None
        content_type = (record.http_headers.get_header("Content-Type") or "") if record.http_headers else ""
        if "html" not in content_type.lower() and "text" not in content_type.lower():
            return None
        try:
            html = record.content_stream().read().decode("utf-8", "replace")
        except (UnicodeDecodeError, AttributeError, OSError):
            return None
        ext = html_to_text(html, url=target, min_chars=min_chars, max_chars=max_chars)
        if ext is None:
            return None
        text = ext.text.text
        cu = canonical_url(target)
        ch = compute_content_hash(text)
        sim = simhash64(text)
        fetch_ts = to_utc(record.rec_headers.get_header("WARC-Date")) or utcnow()
        observed_ts = utcnow()
        did = doc_id_for(cu, ch)
        return DocCapture(
            doc_id=did,
            capture_id=capture_id_for(did, observed_ts.isoformat(), warc_path),
            source=SourceRef(
                source_type=SourceKind.COMMON_CRAWL_WARC,
                source_name=crawl_id or warc_path.split("/")[-3],
                source_locator=f"{CC_BASE}/{warc_path}",
                source_shard=warc_path,
                source_offset_or_record_id=str(offset),
            ),
            discovery_channel=f"cc-warc:{warc_path}",
            job_id=job_id,
            batch_id=batch_id,
            ingest_version=ingest_version,
            url=target,
            canonical_url=cu,
            domain=domain_of(cu),
            fetch_ts=fetch_ts,
            observed_ts=observed_ts,
            published_ts=ext.published_ts,
            title=ext.title,
            text=text,
            language=ext.language_hint or detect_language(text),
            content_hash=ch,
            near_dup_hash=sim,
            content_type=content_type or "text/html",
            http_status=record.http_headers.get_statuscode() if record.http_headers else None,
            robots_decision=RobotsDecision.NOT_APPLICABLE,
        )
    return None
