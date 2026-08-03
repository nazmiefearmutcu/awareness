"""Common Crawl CDX index adapter.

Uses the public CDX server (``https://index.commoncrawl.org/<crawl_id>-index``)
to selectively discover URLs matching domain/URL prefix filters in a window,
then enqueues WARC-repair sub-partitions for byte-range fetches.

Acts as a *planner-style* adapter: it does the URL discovery only. The actual
text extraction is performed by the WARC-repair adapter, which receives one
partition per matched record.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx

from awareness.obs.logging import get_logger
from awareness.obs.metrics import get_metrics
from awareness.schemas.doc import DocCapture, SourceKind
from awareness.schemas.jobs import BackfillRequest
from awareness.sources.base import Adapter, AdapterContext, PartitionSpec
from awareness.sources.cc_crawls import crawl_window_for
from awareness.sources.commoncrawl_wet import crawl_ids_for_range
from awareness.util.http import get_with_retries
from awareness.util.timeutil import to_utc

logger = get_logger("sources.cc_index")
CDX_BASE = "https://index.commoncrawl.org"
# Sane ceiling on total records fetched per partition (CDX pagination cap).
CDX_MAX_RECORDS = 5000
# CDX pageSize is capped at 500 by the server; 100 is a safe default.
CDX_PAGE_SIZE = 100


class CommonCrawlIndexAdapter(Adapter):
    source_type = SourceKind.COMMON_CRAWL_INDEX

    def __init__(self, max_results_per_crawl: int = 200) -> None:
        super().__init__()
        self._max_results = max_results_per_crawl

    def plan(self, request: BackfillRequest) -> list[PartitionSpec]:
        if not request.domains:
            return []  # only meaningful for domain-narrowed backfills
        crawls = crawl_ids_for_range(request.start, request.end)
        out: list[PartitionSpec] = []
        for crawl_id in crawls:
            for dom in request.domains:
                out.append(
                    PartitionSpec(
                        source_type=self.source_type,
                        partition_key=f"{crawl_id}:cdx:{dom}",
                        payload={
                            "crawl_id": crawl_id,
                            "url_filter": f"*.{dom}/*",
                            "max_results": self._max_results,
                            # H-16: carry the request window so the CDX query
                            # can be restricted to the actual backfill range.
                            "start": request.start.isoformat(),
                            "end": request.end.isoformat(),
                        },
                    )
                )
        return out

    async def run_partition(  # noqa: PLR0912
        self,
        partition: PartitionSpec,
        context: AdapterContext,
    ) -> AsyncIterator[DocCapture]:
        crawl_id = partition.payload["crawl_id"]
        url_filter = partition.payload["url_filter"]
        max_results = int(partition.payload.get("max_results", 200))
        # plan() always carries start/end; the tz-aware fallbacks keep a
        # manually-constructed partition from crashing the max() comparison.
        start = to_utc(partition.payload.get("start")) or datetime.min.replace(tzinfo=UTC)
        end = to_utc(partition.payload.get("end")) or datetime.max.replace(tzinfo=UTC)

        cdx_url = f"{CDX_BASE}/{crawl_id}-index"
        params: dict[str, str] = {
            "url": url_filter,
            "output": "json",
            "limit": str(max_results),
            "pageSize": str(CDX_PAGE_SIZE),
        }
        # H-16: restrict the query to the intersection of the crawl's own
        # window and the requested [start, end] so we never capture records
        # outside the backfill window.
        crawl_window = crawl_window_for(crawl_id)
        if crawl_window:
            cf, ct = crawl_window
            from_ts, to_ts = max(cf, start), min(ct, end)
            if from_ts <= to_ts:
                params["from"] = from_ts.strftime("%Y%m%d%H%M%S")
                params["to"] = to_ts.strftime("%Y%m%d%H%M%S")
        logger.info("cc_index_query", crawl_id=crawl_id, filter=url_filter, params=params)
        records: list[dict[str, Any]] = []
        page = 0
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            while len(records) < max(1, min(max_results, CDX_MAX_RECORDS)):
                page_params = {**params, "page": str(page)}
                # H-17: get_with_retries retries 429/5xx with backoff honoring
                # Retry-After and raises RetryableHTTPError when the transient
                # failure persists (task layer retries the whole partition).
                resp = await get_with_retries(
                    client,
                    str(httpx.URL(cdx_url, params=page_params)),
                    headers={"User-Agent": context.user_agent},
                )
                if resp.status_code != 200:
                    logger.warning(
                        "cc_index_query_failed",
                        crawl_id=crawl_id,
                        status=resp.status_code,
                    )
                    return
                parsed = 0
                for raw_line in resp.text.splitlines():
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(rec, dict):
                        continue
                    records.append(rec)
                    parsed += 1
                if parsed == 0:
                    break  # last page reached
                page += 1
                if page * CDX_PAGE_SIZE >= CDX_MAX_RECORDS:
                    break

        get_metrics().inc("cc_index.matches", value=len(records), labels={"crawl_id": crawl_id})

        # Enqueue WARC-repair partitions for the discovered records.
        enqueue = context.extras.setdefault("enqueue", [])
        for r in records:
            warc_path = r.get("filename")
            offset = r.get("offset")
            length = r.get("length")
            if not warc_path or offset is None or length is None:
                continue
            enqueue.append(
                PartitionSpec(
                    source_type=SourceKind.COMMON_CRAWL_WARC,
                    partition_key=f"warc:{warc_path}:{offset}",
                    payload={
                        "warc_path": warc_path,
                        "offset": int(offset),
                        "length": int(length),
                        "url": r.get("url"),
                        "timestamp": r.get("timestamp"),
                        "crawl_id": crawl_id,
                    },
                )
            )
        return
        if False:  # pragma: no cover
            yield
