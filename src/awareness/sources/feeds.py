"""Feeds adapter: RSS, Atom, and Sitemap discovery.

Used by BOTH body (when feeds expose historical archives) and tail (the
default discovery channel for newly published content). The adapter:

1. Reads a YAML seed file describing feeds and sitemaps.
2. ``plan()`` emits one partition per seed; payload carries cursor state.
3. ``run_partition()`` fetches the feed/sitemap, diffs vs last cursor,
   for each new URL emits a TailRecrawl sub-partition.

Politeness: robots.txt is consulted; per-domain limiter is acquired by the
sub-partition that actually fetches the page (tail_recrawl).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence
from pathlib import Path
from typing import Any

import feedparser
import httpx
import yaml
from lxml import etree

from awareness.obs.logging import get_logger
from awareness.obs.metrics import get_metrics
from awareness.schemas.doc import DocCapture, SourceKind
from awareness.schemas.jobs import BackfillRequest
from awareness.sources.base import Adapter, AdapterContext, PartitionSpec
from awareness.util.http import RetryableHTTPError, get_with_retries
from awareness.util.robots import extract_sitemap_urls
from awareness.util.urls import canonical_url, is_homepage_url, is_public_http_url

logger = get_logger("sources.feeds")

# Checkpoint window for feed-level URL cursors. Ordered most-recently-seen;
# oldest entries are dropped when the cap is exceeded.
SEEN_URLS_CAP = 5000


def merge_seen_urls(
    previous: Sequence[str] | None,
    discovered: Iterable[str],
    *,
    cap: int = SEEN_URLS_CAP,
) -> list[str]:
    """Merge discovered URLs into an ordered most-recently-seen window.

    Insertion order is oldest → newest. Re-seeing a URL moves it to the end
    (most recent). When over ``cap``, the oldest entries are dropped so the
    checkpoint retains the most recent ``cap`` URLs.

    Unlike ``set``-based trimming, this is stable across runs and does not
    randomly forget recently seen URLs when the window is full.
    """
    ordered: dict[str, None] = {}
    for raw in previous or ():
        if not raw:
            continue
        # Checkpoint already stores canonical forms; keep non-empty strings.
        ordered[str(raw)] = None
    for raw in discovered:
        cu = canonical_url(raw) if raw else None
        if not cu:
            continue
        # Move to end = most recently seen (LRU-ish ordered window).
        ordered.pop(cu, None)
        ordered[cu] = None
    if cap <= 0:
        return []
    if len(ordered) > cap:
        keys = list(ordered.keys())
        ordered = {k: None for k in keys[-cap:]}
    return list(ordered.keys())


def _load_seeds(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data if isinstance(data, dict) else {}


class FeedsAdapter(Adapter):
    """RSS / Atom / Sitemap discovery → recrawl sub-partitions."""

    source_type = SourceKind.RSS  # canonical; ATOM/SITEMAP share this adapter

    def plan(self, request: BackfillRequest) -> list[PartitionSpec]:
        # Feeds aren't a natural historical body source; only meaningful when
        # the tail engine kicks them off. For BODY backfills we emit nothing.
        return []

    async def run_partition(
        self,
        partition: PartitionSpec,
        context: AdapterContext,
    ) -> AsyncIterator[DocCapture]:
        kind = partition.payload.get("kind", "rss")
        url = partition.payload["url"]
        if kind == "sitemap":
            urls = await _read_sitemap(url, context.user_agent)
            channel = f"sitemap:{url}"
        else:
            urls = await _read_feed(url, context.user_agent)
            channel = f"{kind}:{url}"

        get_metrics().inc("feeds.urls_discovered", value=len(urls), labels={"channel": kind})

        # Filter against ordered cursor (membership is order-independent).
        prev_seen = list(context.checkpoint.get("seen_urls") or [])
        last_seen: set[str] = set(prev_seen)
        new_urls = [u for u in urls if canonical_url(u) and canonical_url(u) not in last_seen]
        # Ordered most-recently-seen window; cap keeps newest SEEN_URLS_CAP.
        context.checkpoint["seen_urls"] = merge_seen_urls(prev_seen, urls, cap=SEEN_URLS_CAP)

        enqueue = context.extras.setdefault("enqueue", [])
        for u in new_urls:
            enqueue.append(
                PartitionSpec(
                    source_type=SourceKind.TAIL_RECRAWL,
                    partition_key=f"tail:{canonical_url(u)}",
                    payload={
                        "url": u,
                        "discovery_channel": channel,
                        "source_kind": kind,
                    },
                )
            )

        # C3-T6 partial: bare-domain homepage seeds → robots Sitemap: discovery once.
        if is_homepage_url(url) and not context.checkpoint.get("robots_sitemaps_discovered"):
            discovered = await _enqueue_robots_sitemaps(url, context)
            context.checkpoint["robots_sitemaps_discovered"] = True
            if discovered:
                get_metrics().inc(
                    "feeds.robots_sitemaps_discovered",
                    value=discovered,
                    labels={"channel": kind},
                )

        return
        if False:  # pragma: no cover
            yield


async def _enqueue_robots_sitemaps(seed_url: str, context: AdapterContext) -> int:
    """Discover Sitemap: URLs from robots.txt for a homepage seed; enqueue once.

    Returns the number of public sitemap partitions enqueued. Missing robots
    cache, empty body, or non-public sitemap URLs are no-ops.
    """
    robots = context.extras.get("robots") if context.extras else None
    if robots is None or not hasattr(robots, "get_robots_txt"):
        return 0
    try:
        body = await robots.get_robots_txt(seed_url, context.user_agent)
    except Exception as exc:  # noqa: BLE001 — discovery must not fail the seed
        logger.warning("robots_sitemap_discover_failed", seed=seed_url, err=str(exc))
        return 0

    enqueue = context.extras.setdefault("enqueue", [])
    added = 0
    for sm_url in extract_sitemap_urls(body):
        if not is_public_http_url(sm_url):
            continue
        key = canonical_url(sm_url) or sm_url
        enqueue.append(
            PartitionSpec(
                source_type=SourceKind.RSS,
                partition_key=f"sitemap:{key}",
                payload={"kind": "sitemap", "url": sm_url},
            )
        )
        added += 1
    if added:
        logger.info("robots_sitemaps_discovered", seed=seed_url, count=added)
    return added


async def _read_feed(url: str, user_agent: str) -> list[str]:
    """RSS / Atom — fetch and parse."""
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            # Transient 429/5xx retried inside get_with_retries (global slot held
            # only during each GET). Exhausted retries raise RetryableHTTPError.
            r = await get_with_retries(
                client, url, headers={"User-Agent": user_agent}
            )
            if r.status_code != 200:
                logger.warning("feed_fetch_non_200", url=url, status=r.status_code)
                return []
            if not r.content:
                return []
            body = r.content
    except RetryableHTTPError:
        raise
    except httpx.HTTPError as exc:
        logger.warning("feed_fetch_failed", url=url, err=str(exc))
        return []
    parsed = feedparser.parse(body)
    out: list[str] = []
    for entry in parsed.entries:
        link = getattr(entry, "link", None)
        if link and link.startswith(("http://", "https://")):
            out.append(link)
    return out


async def _read_sitemap(url: str, user_agent: str, depth: int = 1) -> list[str]:
    """Parse a sitemap or sitemap-index. Follows one level of nesting by default."""
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            # Same retry policy as feeds / CC discovery: transient → retry/raise.
            r = await get_with_retries(
                client, url, headers={"User-Agent": user_agent}
            )
            if r.status_code != 200:
                logger.warning("sitemap_fetch_non_200", url=url, status=r.status_code)
                return []
            if not r.content:
                return []
            body = r.content
    except RetryableHTTPError:
        raise
    except httpx.HTTPError as exc:
        logger.warning("sitemap_fetch_failed", url=url, err=str(exc))
        return []

    try:
        if body.startswith(b"\x1f\x8b"):
            import gzip as _gz

            body = _gz.decompress(body)
        root = etree.fromstring(body)
    except (etree.XMLSyntaxError, OSError, ValueError) as exc:
        logger.warning("sitemap_parse_failed", url=url, err=str(exc))
        return []

    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    out: list[str] = []
    tag = etree.QName(root.tag).localname
    if tag == "sitemapindex":
        if depth <= 0:
            return out
        for child in root.findall(f"{ns}sitemap/{ns}loc"):
            loc = (child.text or "").strip()
            if loc:
                out.extend(await _read_sitemap(loc, user_agent, depth=depth - 1))
    else:
        for child in root.findall(f"{ns}url/{ns}loc"):
            loc = (child.text or "").strip()
            if loc:
                out.append(loc)
    return out
