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

import gzip as _gzip
from collections.abc import AsyncIterator, Iterable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import feedparser
import httpx
import yaml
from lxml import etree

from awareness.obs.logging import get_logger
from awareness.obs.metrics import get_metrics
from awareness.schemas.doc import DocCapture, SourceKind
from awareness.schemas.jobs import BackfillRequest
from awareness.sources.base import Adapter, AdapterContext, PartitionSpec
from awareness.util.http import (
    RetryableHTTPError,
    get_shared_async_client,
    get_with_retries,
)
from awareness.util.robots import extract_sitemap_urls
from awareness.util.urls import canonical_url, is_homepage_url, is_public_http_url

logger = get_logger("sources.feeds")

# Checkpoint window for feed-level URL cursors. Ordered most-recently-seen;
# oldest entries are dropped when the cap is exceeded.
SEEN_URLS_CAP = 5000

# Content negotiation: many CDNs gate XML feeds without a sensible Accept.
FEED_ACCEPT = (
    "application/rss+xml, application/atom+xml, application/xml;q=0.9, "
    "text/xml;q=0.8, */*;q=0.1"
)
SITEMAP_ACCEPT = "application/xml, text/xml, application/gzip, */*;q=0.1"


def _maybe_decompress_body(body: bytes) -> bytes:
    """Decompress gzip-wrapped feed/sitemap bodies (magic ``1f 8b``).

    Many publishers serve ``.xml.gz`` sitemaps and some CDNs gzip RSS even
    without ``Content-Encoding`` after httpx has already decoded the transfer
    encoding. Corrupt gzip falls through to the raw bytes so parsers can fail
    clearly. UTF-8 BOM is stripped so lxml/feedparser do not choke on it.
    """
    if not body:
        return body
    if body.startswith(b"\x1f\x8b"):
        try:
            body = _gzip.decompress(body)
        except OSError as exc:
            logger.warning("feed_body_gunzip_failed", err=str(exc))
            # Fall through: still try BOM strip on the raw bytes.
    if body.startswith(b"\xef\xbb\xbf"):
        return body[3:]
    return body


def _is_http_url(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def _coerce_http_url(value: Any, base_url: str | None = None) -> str | None:
    """Return an absolute http(s) URL, resolving relatives against *base_url*.

    Skips non-http schemes (mailto:, tag:, urn:, javascript:). Relative paths
    (``/story/1``, ``../a``, protocol-relative ``//cdn…``) resolve via
    ``urljoin`` when *base_url* is a usable http(s) base (the feed/sitemap
    URL). Without a base, only absolute http(s) values are accepted.
    """
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    if _is_http_url(s):
        return s
    # Explicit non-http schemes must not be rewritten by urljoin.
    scheme, _, rest = s.partition(":")
    if rest and scheme and scheme.lower() not in ("http", "https") and not s.startswith("//"):
        # scheme present and not http(s) or protocol-relative → reject.
        if "/" not in scheme and scheme.isalpha():
            return None
    if base_url and _is_http_url(base_url):
        resolved = urljoin(base_url, s)
        if _is_http_url(resolved):
            return resolved
    return None


def _entry_attr_http_url(
    entry: Any, *attrs: str, base_url: str | None = None
) -> str | None:
    """First http(s) URL found among named entry attributes (or nested dicts)."""
    for attr in attrs:
        value = getattr(entry, attr, None)
        if value is None and isinstance(entry, dict):
            value = entry.get(attr)
        if isinstance(value, dict):
            value = value.get("value") or value.get("href") or value.get("url")
        coerced = _coerce_http_url(value, base_url)
        if coerced is not None:
            return coerced
    return None


def _media_or_enclosure_urls(entry: Any, base_url: str | None = None) -> list[str]:
    """Collect http(s) URLs from media:content / media:thumbnail / enclosures.

    feedparser surfaces Media RSS as ``media_content`` / ``media_thumbnail``
    (list of dicts with ``url``) and RSS ``<enclosure>`` as ``enclosures``
    (list of dicts with ``href`` or ``url``). Order is preserved so callers
    can prefer HTML pages over binary blobs when both appear.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _push(raw: Any) -> None:
        coerced = _coerce_http_url(raw, base_url)
        if coerced is None or coerced in seen:
            return
        seen.add(coerced)
        out.append(coerced)

    for attr in ("media_content", "media_thumbnail", "enclosures"):
        items = getattr(entry, attr, None)
        if items is None and isinstance(entry, dict):
            items = entry.get(attr)
        if not items:
            continue
        if not isinstance(items, (list, tuple)):
            items = [items]
        for item in items:
            if isinstance(item, dict):
                _push(item.get("url") or item.get("href"))
            else:
                _push(getattr(item, "url", None) or getattr(item, "href", None))
    # Singular enclosure (some parsers).
    enc = getattr(entry, "enclosure", None)
    if enc is None and isinstance(entry, dict):
        enc = entry.get("enclosure")
    if isinstance(enc, dict):
        _push(enc.get("url") or enc.get("href"))
    elif enc is not None:
        _push(getattr(enc, "url", None) or getattr(enc, "href", None))
    return out


def _prefer_html_media_url(urls: list[str]) -> str | None:
    """Prefer a likely HTML page URL among media/enclosure candidates.

    Many podcast/news feeds list the binary enclosure first and an HTML
    landing page second (or vice versa). Prefer ``text/html``-looking paths
    (no media extension, or ``.html``/``.htm``) so tail_recrawl fetches an
    article rather than a naked MP3 when both are present.
    """
    if not urls:
        return None
    _media_ext = (
        ".mp3",
        ".mp4",
        ".m4a",
        ".wav",
        ".ogg",
        ".flac",
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".pdf",
        ".zip",
        ".gz",
        ".webm",
        ".mov",
    )

    def _looks_html(u: str) -> bool:
        path = urlsplit(u).path.lower() if u else ""
        # Binary / asset extensions are not article pages.
        return not any(path.endswith(ext) for ext in _media_ext)

    for u in urls:
        if _looks_html(u):
            return u
    return urls[0]


def entry_primary_url(entry: Any, base_url: str | None = None) -> str | None:
    """Best article URL from a feedparser entry (RSS link or Atom links[]).

    Prefers publisher-original permalinks when present:

    1. ``feedburner:origLink`` / ``phoenix:origLink`` / bare ``origlink``
       (feedparser exposes these as ``feedburner_origlink`` etc.) — these
       beat FeedBurner / syndication proxy ``link`` values so the fetch gate
       keys the real article URL instead of a redirector.
    2. ``entry.link`` when it is an http(s) URL (or relative resolved against
       *base_url*, typically the feed URL).
    3. ``entry.links`` for ``rel=alternate`` (Atom default) then any http(s)
       href (also resolved when relative).
    4. ``entry.id`` / ``entry.guid`` when that value is itself an http(s) URL
       (common when publishers put the permalink only in ``guid`` / Atom
       ``id``).
    5. ``media:content`` / ``media:thumbnail`` / RSS ``enclosure`` http(s)
       URLs (podcasts and media-heavy feeds that omit ``link``). Prefer an
       HTML-looking candidate when several are listed.

    Returns ``None`` when no usable URL is present.
    """
    # Syndication proxies (FeedBurner, etc.) put the real article URL in
    # origLink while ``link`` points at feedproxy.google.com / similar.
    orig = _entry_attr_http_url(
        entry,
        "feedburner_origlink",
        "phoenix_origlink",
        "origlink",
        "feedburner:origLink",  # raw namespace form if present
        base_url=base_url,
    )
    if orig is not None:
        return orig

    link = getattr(entry, "link", None)
    coerced_link = _coerce_http_url(link, base_url)
    if coerced_link is not None:
        return coerced_link

    links = getattr(entry, "links", None) or []
    fallback: str | None = None
    for ln in links:
        if isinstance(ln, dict):
            href = ln.get("href")
            rel = ln.get("rel")
        else:
            href = getattr(ln, "href", None)
            rel = getattr(ln, "rel", None)
        href_s = _coerce_http_url(href, base_url)
        if href_s is None:
            continue
        # Atom: alternate is the HTML article; self is often the entry id.
        if rel in (None, "", "alternate"):
            return href_s
        if fallback is None:
            fallback = href_s
    if fallback is not None:
        return fallback

    # RSS guid / Atom id often hold the permanent article URL when <link> is
    # absent or non-http (tag: URNs, bare guids). Only accept http(s).
    for attr in ("id", "guid"):
        value = getattr(entry, attr, None)
        if isinstance(value, dict):
            value = value.get("value") or value.get("href")
        coerced = _coerce_http_url(value, base_url)
        if coerced is not None:
            return coerced

    # Media RSS / enclosure: last resort so podcast and media-only entries
    # still enqueue a fetchable URL for the tail (avoids silent drop).
    media_urls = _media_or_enclosure_urls(entry, base_url=base_url)
    preferred = _prefer_html_media_url(media_urls)
    if preferred is not None:
        return preferred
    return None


def dedupe_feed_urls(urls: Iterable[str]) -> list[str]:
    """Preserve first-seen order while collapsing canonical URL identity.

    Feeds and sitemaps often list the same article twice (http/https, trailing
    slash, utm params). Collapsing here prevents double-enqueue of identical
    tail recrawls within one discovery pass. Original strings are kept for
    fetch; identity keys are applied again at enqueue.
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in urls:
        if not raw:
            continue
        raw_s = str(raw).strip()
        if not raw_s:
            continue
        key = canonical_url(raw_s) or raw_s
        if key in seen:
            continue
        seen.add(key)
        out.append(raw_s)
    return out


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
        # Reuse process-wide pooled client (connection keep-alive across seeds).
        client = await get_shared_async_client(timeout=30.0, follow_redirects=True)
        # Transient 429/5xx retried inside get_with_retries (global slot held
        # only during each GET). Exhausted retries raise RetryableHTTPError.
        r = await get_with_retries(
            client,
            url,
            headers={"User-Agent": user_agent, "Accept": FEED_ACCEPT},
        )
        if r.status_code != 200:
            logger.warning("feed_fetch_non_200", url=url, status=r.status_code)
            get_metrics().inc(
                "feeds.fetch_non_200",
                labels={"kind": "rss", "status": str(r.status_code)},
            )
            return []
        if not r.content:
            return []
        body = _maybe_decompress_body(r.content)
    except RetryableHTTPError:
        get_metrics().inc(
            "feeds.retryable_http_error",
            labels={"kind": "rss"},
        )
        raise
    except httpx.HTTPError as exc:
        logger.warning("feed_fetch_failed", url=url, err=str(exc))
        return []
    parsed = feedparser.parse(body)
    out: list[str] = []
    for entry in parsed.entries:
        # Resolve relative entry links against the feed URL (common on
        # self-hosted / legacy RSS where <link>/story</link> is path-only).
        link = entry_primary_url(entry, base_url=url)
        if link:
            out.append(link)
    # Collapse scheme/slash/utm variants so one article → one tail enqueue.
    return dedupe_feed_urls(out)


async def _read_sitemap(url: str, user_agent: str, depth: int = 1) -> list[str]:
    """Parse a sitemap or sitemap-index. Follows one level of nesting by default."""
    try:
        # Longer timeout for large sitemap indexes; still pooled by timeout key.
        client = await get_shared_async_client(timeout=60.0, follow_redirects=True)
        # Same retry policy as feeds / CC discovery: transient → retry/raise.
        r = await get_with_retries(
            client,
            url,
            headers={"User-Agent": user_agent, "Accept": SITEMAP_ACCEPT},
        )
        if r.status_code != 200:
            logger.warning("sitemap_fetch_non_200", url=url, status=r.status_code)
            get_metrics().inc(
                "feeds.fetch_non_200",
                labels={"kind": "sitemap", "status": str(r.status_code)},
            )
            return []
        if not r.content:
            return []
        body = _maybe_decompress_body(r.content)
    except RetryableHTTPError:
        get_metrics().inc(
            "feeds.retryable_http_error",
            labels={"kind": "sitemap"},
        )
        raise
    except httpx.HTTPError as exc:
        logger.warning("sitemap_fetch_failed", url=url, err=str(exc))
        return []

    try:
        root = etree.fromstring(body)
    except (etree.XMLSyntaxError, OSError, ValueError) as exc:
        logger.warning("sitemap_parse_failed", url=url, err=str(exc))
        return []

    out: list[str] = []
    tag = etree.QName(root.tag).localname
    if tag == "sitemapindex":
        if depth <= 0:
            return out
        for loc in _sitemap_loc_texts(root, parent_local="sitemap", base_url=url):
            out.extend(await _read_sitemap(loc, user_agent, depth=depth - 1))
    else:
        out.extend(_sitemap_loc_texts(root, parent_local="url", base_url=url))
    # Same article listed twice (http vs https, utm wrappers) → one identity.
    return dedupe_feed_urls(out)


def _sitemap_loc_texts(
    root: Any, *, parent_local: str, base_url: str | None = None
) -> list[str]:
    """Collect ``<loc>`` text under ``parent_local`` elements, any namespace.

    Standard sitemaps use ``{http://www.sitemaps.org/schemas/sitemap/0.9}``,
    but many publishers emit un-namespaced or default-namespaced XML. Matching
    on local-name keeps discovery working for both. Relative locs resolve
    against *base_url* (the sitemap document URL).
    """
    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"

    def _abs(loc: str) -> str | None:
        return _coerce_http_url(loc, base_url)

    # Fast path: standard sitemap namespace.
    found = [
        abs_loc
        for el in root.findall(f"{ns}{parent_local}/{ns}loc")
        if (abs_loc := _abs((el.text or "").strip()))
    ]
    if found:
        return found
    # Namespace-agnostic fallback (no-ns, alternate default xmlns, etc.).
    out: list[str] = []
    for parent in root.iter():
        try:
            if etree.QName(parent.tag).localname != parent_local:
                continue
        except (ValueError, TypeError):
            continue
        for child in parent:
            try:
                if etree.QName(child.tag).localname != "loc":
                    continue
            except (ValueError, TypeError):
                continue
            loc = (child.text or "").strip()
            abs_loc = _abs(loc) if loc else None
            if abs_loc:
                out.append(abs_loc)
    return out
