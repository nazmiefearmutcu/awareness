"""GDELT supplemental adapter.

GDELT publishes 15-minute master files at:
    http://data.gdeltproject.org/gdeltv2/<YYYYMMDDHHMMSS>.gkg.csv.zip
    http://data.gdeltproject.org/gdeltv2/<YYYYMMDDHHMMSS>.export.CSV.zip

We only consume the GKG (Global Knowledge Graph) CSV, which lists news article
URLs with timestamps. We then enqueue tail_recrawl partitions per URL. We do
NOT persist GDELT's analytical fields; we use it strictly as a discovery
channel for the public text web.

For the BODY backfill, this adapter walks 15-minute slots in the range; for
TAIL it's driven by the tail engine.
"""

from __future__ import annotations

import asyncio
import csv
import io
import time
import zipfile
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import httpx

from awareness.obs.logging import get_logger
from awareness.obs.metrics import get_metrics
from awareness.schemas.doc import DocCapture, SourceKind
from awareness.schemas.jobs import BackfillRequest
from awareness.util.http import get_shared_async_client
from awareness.sources.base import Adapter, AdapterContext, PartitionSpec
from awareness.util.timeutil import to_utc, utcnow
from awareness.util.urls import canonical_url

logger = get_logger("sources.gdelt")
GDELT_BASE = "http://data.gdeltproject.org/gdeltv2"


def _status_class(code: int) -> str:
    """Map an HTTP status to a coarse class label (``2xx``, ``4xx``, …)."""
    if code < 100:
        return "unknown"
    return f"{code // 100}xx"


def _record_gdelt_fetch(
    *,
    outcome: str,
    status_class: str,
    elapsed: float,
    slot: str,
) -> None:
    """Emit process-local GDELT slot-fetch counters + latency histogram."""
    m = get_metrics()
    labels = {"outcome": outcome, "status_class": status_class}
    m.inc("gdelt.fetch_attempts", labels=labels)
    m.observe("gdelt.fetch_seconds", max(0.0, elapsed), labels=labels)
    # Slot label is high-cardinality; keep on a separate counter for debugging.
    m.inc("gdelt.slots", labels={"outcome": outcome, "slot": slot})


def _quarter_hours(start: datetime, end: datetime) -> list[str]:
    """Yield 15-minute slot ids in ``yyyymmddhhmmss`` form."""
    cur = to_utc(start) or utcnow()
    end = to_utc(end) or utcnow()
    # Round down to nearest 15 minutes.
    cur = cur.replace(minute=cur.minute - (cur.minute % 15), second=0, microsecond=0)
    out: list[str] = []
    while cur <= end:
        out.append(cur.strftime("%Y%m%d%H%M%S"))
        cur += timedelta(minutes=15)
    return out


def latest_gkg_slot(now: datetime | None = None, lag_minutes: int = 30) -> str:
    """Most recent 15-minute GKG slot that is likely already published.

    GDELT v2 lags real time by ~15 min; we subtract ``lag_minutes`` (default
    30) for safety, then round down to the nearest quarter hour. Used by the
    tail engine to follow the live global-news firehose.
    """
    base = (to_utc(now) if now else utcnow()) or utcnow()
    base = base - timedelta(minutes=max(0, lag_minutes))
    base = base.replace(minute=base.minute - (base.minute % 15), second=0, microsecond=0)
    return base.strftime("%Y%m%d%H%M%S")


class GdeltAdapter(Adapter):
    source_type = SourceKind.GDELT

    def plan(self, request: BackfillRequest) -> list[PartitionSpec]:
        slots = _quarter_hours(request.start, request.end)
        # Cap to avoid runaway tasks in smoke runs.
        cap = request.max_tasks or 8
        slots = slots[:cap]
        return [
            PartitionSpec(
                source_type=self.source_type,
                partition_key=f"gdelt:gkg:{slot}",
                payload={"slot": slot},
            )
            for slot in slots
        ]

    async def run_partition(
        self,
        partition: PartitionSpec,
        context: AdapterContext,
    ) -> AsyncIterator[DocCapture]:
        slot = partition.payload["slot"]
        url = f"{GDELT_BASE}/{slot}.gkg.csv.zip"
        t0 = time.perf_counter()
        try:
            client = await get_shared_async_client(timeout=60.0, follow_redirects=True)
            r = await client.get(url, headers={"User-Agent": context.user_agent})
            elapsed = time.perf_counter() - t0
            sc = _status_class(r.status_code)
            if r.status_code != 200:
                outcome = "missing" if r.status_code == 404 else "http_error"
                _record_gdelt_fetch(
                    outcome=outcome, status_class=sc, elapsed=elapsed, slot=slot
                )
                logger.info("gdelt_slot_missing", slot=slot, status=r.status_code)
                return
            payload = r.content
            _record_gdelt_fetch(
                outcome="ok", status_class=sc, elapsed=elapsed, slot=slot
            )
        except httpx.HTTPError as exc:
            elapsed = time.perf_counter() - t0
            _record_gdelt_fetch(
                outcome="transport_error",
                status_class="transport",
                elapsed=elapsed,
                slot=slot,
            )
            logger.warning("gdelt_fetch_failed", err=str(exc))
            return

        urls, extract_ok = await asyncio.get_event_loop().run_in_executor(
            None, _extract_gkg_urls_with_status, payload
        )
        if not extract_ok:
            get_metrics().inc("gdelt.extract_errors", labels={"slot": slot})
        max_urls = partition.payload.get("max_urls")
        # Cap only on a positive value; None/0/negative → no per-slot cap.
        if max_urls is not None and int(max_urls) > 0:
            urls = urls[: int(max_urls)]
        m = get_metrics()
        m.inc("gdelt.urls_discovered", value=len(urls), labels={"slot": slot})
        enqueue = context.extras.setdefault("enqueue", [])
        enqueued = 0
        for u in urls:
            cu = canonical_url(u)
            if not cu:
                continue
            enqueue.append(
                PartitionSpec(
                    source_type=SourceKind.TAIL_RECRAWL,
                    # Same key shape as feeds.py so UNIQUE(job_id, partition_key)
                    # collapses cross-source duplicates (RSS vs GDELT).
                    partition_key=f"tail:{cu}",
                    payload={
                        "url": u,
                        "discovery_channel": f"gdelt:{slot}",
                        "source_kind": "gdelt",
                    },
                )
            )
            enqueued += 1
        if enqueued:
            m.inc("gdelt.urls_enqueued", value=float(enqueued), labels={"slot": slot})
        return
        if False:  # pragma: no cover
            yield


def _extract_gkg_urls(zipped: bytes) -> list[str]:
    """Extract GKG document URLs; empty list on corrupt zip (legacy helper)."""
    urls, _ok = _extract_gkg_urls_with_status(zipped)
    return urls


def _extract_gkg_urls_with_status(zipped: bytes) -> tuple[list[str], bool]:
    """Return ``(urls, extract_ok)`` where *extract_ok* is False on bad zip/IO."""
    out: set[str] = set()
    try:
        with zipfile.ZipFile(io.BytesIO(zipped)) as z:
            for name in z.namelist():
                with z.open(name) as fh:
                    text_stream = io.TextIOWrapper(fh, encoding="utf-8", errors="replace")
                    reader = csv.reader(text_stream, delimiter="\t")
                    for row in reader:
                        if not row:
                            continue
                        # GKG v2 column layout: ``DOCUMENTIDENTIFIER`` is column index 4
                        if len(row) > 4:
                            url = row[4].strip()
                            if url.startswith(("http://", "https://")):
                                out.add(url)
    except (zipfile.BadZipFile, OSError):
        return [], False
    return list(out), True
