"""FineWeb / FineWeb2 adapter (HuggingFace).

Streams plain-text rows from the HuggingFace datasets ``HuggingFaceFW/fineweb``
and ``HuggingFaceFW/fineweb-2``. The ``datasets`` package is optional; if it's
not installed, ``plan()`` returns an empty list with a warning and the adapter
becomes a no-op. This keeps the dependency surface lean for users who just
want the WET + tail path.

Partitioning:
- One partition per (dataset, dump, sample limit). The 'dump' aligns with a
  Common Crawl ``CC-MAIN-YYYY-WW`` value when present in FineWeb's metadata.
- Within a partition, we stream rows; each row has ``text``, ``url``,
  ``date`` (when available), and ``language``.

Resume: checkpoint stores the last consumed row index per partition.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

from awareness.config import get_settings
from awareness.normalize.text import detect_language, normalize_text, safe_title
from awareness.obs.logging import get_logger
from awareness.obs.metrics import get_metrics
from awareness.schemas.doc import DocCapture, RobotsDecision, SourceKind, SourceRef
from awareness.schemas.jobs import BackfillRequest
from awareness.sources.base import Adapter, AdapterContext, PartitionSpec
from awareness.sources.commoncrawl_wet import crawl_ids_for_range
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

logger = get_logger("sources.fineweb")


def _fineweb_dataset_label(ds_name: str) -> str:
    """Map HF dataset id to a low-cardinality metric label."""
    n = (ds_name or "").lower()
    if "fineweb-2" in n or "fineweb_2" in n:
        return "fineweb_2"
    return "fineweb"


def _normalize_filter_reason(reason: str | None) -> str:
    """Collapse normalize_text discard reasons to stable filter labels."""
    if not reason:
        return "normalize"
    if reason == "empty":
        return "empty"
    if reason.startswith("too_short"):
        return "too_short"
    return "normalize"


def _record_fineweb_load(*, outcome: str, elapsed: float, dataset: str) -> None:
    """Emit process-local FineWeb load counters + latency histogram."""
    m = get_metrics()
    labels = {"outcome": outcome, "dataset": dataset}
    m.inc("fineweb.load_attempts", labels=labels)
    m.observe("fineweb.load_seconds", max(0.0, elapsed), labels=labels)


class FineWebDependencyMissing(RuntimeError):
    """FineWeb was explicitly requested but the optional `datasets` dep is missing.

    Install it with `pip install 'awareness[hf]'` (adds datasets + huggingface-hub).
    """


class FineWebAdapter(Adapter):
    """Combined FineWeb + FineWeb2 adapter.

    The same adapter handles both datasets; the dataset name is on the partition.
    """

    source_type = SourceKind.FINEWEB

    def __init__(self, default_dataset: str = "HuggingFaceFW/fineweb", rows_per_partition: int = 500) -> None:
        super().__init__()
        self._default = default_dataset
        self._rows = rows_per_partition

    def plan(self, request: BackfillRequest) -> list[PartitionSpec]:
        explicitly_requested = bool(
            {SourceKind.FINEWEB, SourceKind.FINEWEB_2} & set(request.sources or [])
        )
        try:
            import datasets  # noqa: F401, PLC0415
        except ImportError as exc:
            if explicitly_requested:
                raise FineWebDependencyMissing(
                    "FineWeb was requested but the 'datasets' package is not installed. "
                    "Install it with: pip install 'awareness[hf]'"
                ) from exc
            logger.info("fineweb_skipped_missing_datasets_lib")
            return []
        # Build candidate (dataset, dump) tuples.
        crawls = crawl_ids_for_range(request.start, request.end)
        # If languages requested, pivot to fineweb-2 (multilingual).
        datasets_to_use = []
        if request.languages and any(lang.lower() not in ("en", "english") for lang in request.languages):
            datasets_to_use.append(("HuggingFaceFW/fineweb-2", SourceKind.FINEWEB_2))
        else:
            datasets_to_use.append((self._default, SourceKind.FINEWEB))

        out: list[PartitionSpec] = []
        seen_keys: set[str] = set()

        for ds_name, kind in datasets_to_use:
            configs: list[str] = []
            try:
                from datasets import get_dataset_config_names  # noqa: PLC0415
                configs = get_dataset_config_names(ds_name)
            except Exception as exc:
                logger.info("fineweb_failed_to_fetch_configs", ds=ds_name, err=str(exc))

            for crawl_id in crawls:
                resolved_dump = crawl_id
                if configs and crawl_id not in configs:
                    fallback = "sample-10BT" if "sample-10BT" in configs else (configs[0] if configs else crawl_id)
                    logger.warning("fineweb_crawl_id_mismatch", ds=ds_name, requested=crawl_id, fallback=fallback)
                    resolved_dump = fallback

                part_key = f"{ds_name}:{resolved_dump}"
                if part_key in seen_keys:
                    continue
                seen_keys.add(part_key)

                out.append(
                    PartitionSpec(
                        source_type=kind,
                        partition_key=part_key,
                        payload={
                            "dataset": ds_name,
                            "dump": resolved_dump,
                            "rows_per_partition": self._rows,
                            "languages": request.languages,
                            "domains": request.domains,
                        },
                    )
                )
        return out

    async def run_partition(
        self,
        partition: PartitionSpec,
        context: AdapterContext,
    ) -> AsyncIterator[DocCapture]:
        ds_name = partition.payload.get("dataset") or self._default
        ds_label = _fineweb_dataset_label(str(ds_name))
        try:
            from datasets import load_dataset  # noqa: PLC0415
        except ImportError:
            logger.info("fineweb_run_skipped_missing_datasets_lib")
            _record_fineweb_load(outcome="missing_dep", elapsed=0.0, dataset=ds_label)
            return

        dump = partition.payload.get("dump")
        rows_per = int(partition.payload.get("rows_per_partition", self._rows))
        languages = set(partition.payload.get("languages") or [])
        domains_filter = set(partition.payload.get("domains") or [])
        start_offset = int(context.checkpoint.get("row_index", 0))

        settings = get_settings()
        m = get_metrics()

        # Stream mode is required to avoid downloading TB-scale dumps.
        t_load = time.perf_counter()
        try:
            ds = load_dataset(ds_name, name=dump, split="train", streaming=True)
        except Exception as exc:
            _record_fineweb_load(
                outcome="error",
                elapsed=time.perf_counter() - t_load,
                dataset=ds_label,
            )
            logger.warning("fineweb_load_failed", ds=ds_name, dump=dump, err=str(exc))
            return
        _record_fineweb_load(
            outcome="ok",
            elapsed=time.perf_counter() - t_load,
            dataset=ds_label,
        )

        emitted = 0
        t_part = time.perf_counter()
        for i, row in enumerate(ds):
            if context.is_stopping():
                break
            if i < start_offset:
                continue
            if emitted >= rows_per:
                break
            m.inc("fineweb.rows_seen", labels={"dataset": ds_label})
            text_raw = row.get("text") or row.get("content")
            if not text_raw:
                m.inc(
                    "fineweb.rows_filtered",
                    labels={"reason": "empty", "dataset": ds_label},
                )
                continue
            url = row.get("url") or row.get("source")
            row_date = row.get("date") or row.get("date_download") or row.get("published_date")
            lang = (row.get("language") or "").lower() or None
            if languages and lang and lang not in languages:
                m.inc(
                    "fineweb.rows_filtered",
                    labels={"reason": "language", "dataset": ds_label},
                )
                continue
            cu = canonical_url(url) if url else None
            dom = domain_of(cu) if cu else None
            if domains_filter and dom not in domains_filter:
                m.inc(
                    "fineweb.rows_filtered",
                    labels={"reason": "domain", "dataset": ds_label},
                )
                continue
            norm = normalize_text(
                text_raw,
                min_chars=settings.text_min_chars,
                max_chars=settings.text_max_chars,
            )
            if norm.discarded_reason:
                m.inc(
                    "fineweb.rows_filtered",
                    labels={
                        "reason": _normalize_filter_reason(norm.discarded_reason),
                        "dataset": ds_label,
                    },
                )
                continue
            ch = compute_content_hash(norm.text)
            sim = simhash64(norm.text)
            fetch_ts = to_utc(row_date) or utcnow()
            observed_ts = utcnow()
            did = doc_id_for(cu, ch)
            yield DocCapture(
                doc_id=did,
                capture_id=capture_id_for(did, observed_ts.isoformat(), str(i)),
                source=SourceRef(
                    source_type=partition.source_type,
                    source_name=ds_name,
                    source_locator=f"hf://datasets/{ds_name}",
                    source_shard=str(dump or ""),
                    source_offset_or_record_id=str(i),
                ),
                discovery_channel=f"hf:{ds_name}",
                job_id=context.job_id,
                batch_id=context.batch_id,
                ingest_version=context.ingest_version,
                url=url,
                canonical_url=cu,
                domain=dom,
                fetch_ts=fetch_ts,
                observed_ts=observed_ts,
                title=safe_title(None, norm.text),
                text=norm.text,
                language=lang or detect_language(norm.text),
                content_hash=ch,
                near_dup_hash=sim,
                content_type="text/plain",
                http_status=200,
                robots_decision=RobotsDecision.NOT_APPLICABLE,
            )
            emitted += 1
            m.inc("fineweb.rows_admitted", labels={"dataset": ds_label})
            # Update checkpoint cooperatively.
            context.checkpoint["row_index"] = i + 1
        m.observe(
            "fineweb.partition_seconds",
            max(0.0, time.perf_counter() - t_part),
            labels={"dataset": ds_label},
        )
        logger.info("fineweb_partition_done", ds=ds_name, dump=dump, emitted=emitted)
