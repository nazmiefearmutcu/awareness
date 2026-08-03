"""Tail recrawl adapter.

Receives one partition per discovered URL (emitted by the feeds adapter or
by direct planner inputs), fetches the page, runs HTML→text, emits one
``DocCapture``.

Politeness:
- Robots.txt is consulted via the shared cache.
- Per-domain concurrency and delay are honored.
- If robots disallows, we emit a DocCapture only if explicitly requested.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from urllib.parse import urljoin, urlsplit

import httpx

from awareness.config import get_settings
from awareness.normalize.html import html_to_text
from awareness.normalize.text import detect_language
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
from awareness.util.http import (
    RetryableHTTPError,
    decode_http_text,
    get_shared_async_client,
    get_with_retries,
)
from awareness.util.ratelimit import PerDomainLimiter
from awareness.util.robots import RobotsCache
from awareness.util.timeutil import parse_http_date, utcnow
from awareness.util.urls import canonical_url, domain_of, is_public_http_url

logger = get_logger("sources.tail_recrawl")

# Discovery channels / source_kind values that produce short legitimate news.
_NEWS_SOURCE_MARKERS = ("rss", "atom", "gdelt", "sitemap")
_NEWS_MIN_FLOOR = 40


def is_news_discovery(
    discovery_channel: str | None = None,
    source_kind: str | None = None,
) -> bool:
    """True when the URL came from RSS/Atom/GDELT/sitemap-style discovery."""
    kind = (source_kind or "").strip().lower()
    if kind.startswith(_NEWS_SOURCE_MARKERS):
        return True
    channel = (discovery_channel or "").strip().lower()
    return channel.startswith(_NEWS_SOURCE_MARKERS)


def resolve_text_min_chars(
    settings,
    *,
    discovery_channel: str | None = None,
    source_kind: str | None = None,
) -> int:
    """Effective min_chars for HTML extract: news sources use a lower floor.

    News floor is ``max(40, min(text_min_chars, text_min_chars_news))`` so bulk
    defaults (200) do not drop short feed articles, while a user who lowers
    ``text_min_chars`` still gets that lower bound for news.
    """
    base = int(settings.text_min_chars)
    if not is_news_discovery(discovery_channel, source_kind):
        return base
    news = int(getattr(settings, "text_min_chars_news", 80))
    return max(_NEWS_MIN_FLOOR, min(base, news))


class TailRecrawlAdapter(Adapter):
    source_type = SourceKind.TAIL_RECRAWL

    def plan(self, request: BackfillRequest) -> list[PartitionSpec]:
        # Reactive only; the planner never emits these directly. The feeds
        # adapter and the tail engine enqueue them as sub-partitions.
        return []

    async def run_partition(  # noqa: PLR0911
        self,
        partition: PartitionSpec,
        context: AdapterContext,
    ) -> AsyncIterator[DocCapture]:
        url = partition.payload["url"]
        discovery_channel = partition.payload.get("discovery_channel", "tail")
        force_refresh = bool(partition.payload.get("force_refresh"))
        if not is_public_http_url(url):
            get_metrics().inc("tail.blocked_internal_url")
            return
        dom = domain_of(url)
        if not dom:
            return

        settings = get_settings()
        limiter: PerDomainLimiter = context.extras.get("limiter") or _global_limiter(settings)
        robots: RobotsCache = context.extras.get("robots") or _global_robots(settings)
        state = context.extras.get("state")

        # Exact-URL gate: skip HTTP when this canonical URL was already fetched.
        pre_cu = canonical_url(url)
        if state is not None and pre_cu and not force_refresh and state.was_url_fetched(pre_cu):
            get_metrics().inc("tail.fetch_skipped_seen", labels={"domain": dom})
            logger.debug("tail_skip_already_fetched", url=url, canonical_url=pre_cu)
            return

        # Robots check.
        try:
            allowed = await robots.is_allowed(url, context.user_agent)
        except Exception:
            allowed = True
        robots_decision = RobotsDecision.ALLOWED if allowed else RobotsDecision.DISALLOWED
        if not allowed:
            get_metrics().inc("tail.robots_disallowed", labels={"domain": dom})
            return

        crawl_delay = robots.crawl_delay(url)
        # Order: robots already checked → domain limiter (delay + slot) →
        # get_with_retries (process-wide fetch slot around each GET only).
        metrics = get_metrics()
        t_fetch = time.perf_counter()
        fetch_outcome = "ok"
        r: httpx.Response | None = None
        async with limiter.domain(dom, override_delay=crawl_delay):
            try:
                # Pooled client; follow_redirects=False so each hop stays public.
                # Per-request UA (not client defaults) keeps the pool shareable.
                client = await get_shared_async_client(
                    timeout=settings.request_timeout_sec,
                    follow_redirects=False,
                )
                r = await _get_public_url(
                    client,
                    url,
                    headers={"User-Agent": context.user_agent},
                    user_agent=context.user_agent,
                    robots=robots,
                    limiter=limiter,
                )
                if r is None:
                    fetch_outcome = "blocked_redirect"
                    metrics.inc("tail.blocked_internal_url", labels={"domain": dom})
                elif not r.content:
                    # L-06: a 200 with an empty body is not a non-200 — classify
                    # it as "empty" so dashboards don't mislabel it an error.
                    fetch_outcome = "empty"
                elif r.status_code >= 400:
                    fetch_outcome = "non_200"
            except RetryableHTTPError:
                # Transient failure exhausted retries — task layer requeues.
                fetch_outcome = "retryable_error"
                metrics.inc("tail.fetch_errors", labels={"domain": dom})
                metrics.inc("tail.retryable_http_error", labels={"domain": dom})
                raise
            except httpx.HTTPError as exc:
                fetch_outcome = "network_error"
                logger.warning("tail_fetch_failed", url=url, err=str(exc))
                metrics.inc("tail.fetch_errors", labels={"domain": dom})
            finally:
                fetch_elapsed = max(0.0, time.perf_counter() - t_fetch)
                metrics.inc(
                    "tail.fetch_attempts",
                    labels={"outcome": fetch_outcome, "domain": dom},
                )
                metrics.observe(
                    "tail.fetch_seconds",
                    fetch_elapsed,
                    labels={"outcome": fetch_outcome},
                )

        if r is None or fetch_outcome in ("blocked_redirect", "network_error"):
            return

        metrics.inc("tail.fetches", labels={"domain": dom})
        if fetch_outcome == "empty":
            # L-06: fetched but the body was empty — nothing to extract.
            return
        if fetch_outcome == "non_200":
            metrics.inc(
                "tail.fetch_non_200",
                labels={"domain": dom, "status": str(r.status_code)},
            )
            return

        ctype = r.headers.get("Content-Type", "")
        if "html" not in ctype.lower() and "xml" not in ctype.lower() and "text" not in ctype.lower():
            return

        # Prefer Content-Type / HTML meta charset over httpx's r.text heuristic
        # so Latin-1 / Windows-125x / CJK pages extract without mojibake.
        html, encoding = decode_http_text(r.content, content_type=ctype or None)
        get_metrics().inc(
            "tail.decode_charset",
            labels={"encoding": (encoding or "unknown")[:32]},
        )
        source_kind = partition.payload.get("source_kind")
        min_chars = resolve_text_min_chars(
            settings,
            discovery_channel=discovery_channel,
            source_kind=source_kind if isinstance(source_kind, str) else None,
        )
        ext = html_to_text(
            html,
            url=url,
            min_chars=min_chars,
            max_chars=settings.text_max_chars,
        )
        if ext is None:
            get_metrics().inc("tail.text_too_short", labels={"domain": dom})
            return
        text = ext.text.text
        # Observability: news floor accepted a body the bulk floor would drop.
        bulk_min = int(settings.text_min_chars)
        if min_chars < bulk_min and len(text) < bulk_min:
            get_metrics().inc(
                "tail.news_floor_kept",
                labels={"domain": dom, "discovery": str(discovery_channel or "")[:48]},
            )
        ch = compute_content_hash(text)
        sim = simhash64(text)

        observed_ts = utcnow()
        cu = canonical_url(ext.canonical_url_hint or url) or canonical_url(url)
        did = doc_id_for(cu, ch)

        # Durable fetch log so future partitions for this URL skip HTTP.
        # Record before yield so a consumer that stops early still gates next time.
        if state is not None:
            try:
                # Record extract-resolved canonical (and pre-fetch key if different).
                state.record_url_fetch(
                    cu or pre_cu,
                    doc_id=did,
                    content_hash=ch,
                    http_status=int(r.status_code),
                )
                if pre_cu and cu and pre_cu != cu:
                    state.record_url_fetch(
                        pre_cu,
                        doc_id=did,
                        content_hash=ch,
                        http_status=int(r.status_code),
                    )
            except Exception as exc:
                logger.warning("url_fetch_log_failed", url=url, err=str(exc))

        yield DocCapture(
            doc_id=did,
            capture_id=capture_id_for(did, observed_ts.isoformat(), url),
            source=SourceRef(
                source_type=SourceKind.TAIL_RECRAWL,
                source_name="tail",
                source_locator=url,
                source_shard=discovery_channel,
                source_offset_or_record_id=None,
            ),
            discovery_channel=discovery_channel,
            job_id=context.job_id,
            batch_id=context.batch_id,
            ingest_version=context.ingest_version,
            url=url,
            canonical_url=cu,
            domain=dom,
            fetch_ts=observed_ts,
            observed_ts=observed_ts,
            published_ts=ext.published_ts,
            last_modified=parse_http_date(r.headers.get("Last-Modified")),
            content_type=ctype,
            http_status=int(r.status_code),
            etag=r.headers.get("ETag"),
            title=ext.title,
            text=text,
            language=ext.language_hint or detect_language(text),
            content_hash=ch,
            near_dup_hash=sim,
            robots_decision=robots_decision,
        )


def _hop_host(url: str) -> str | None:
    """Normalized host (netloc) used to detect cross-host redirect hops."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = parts.hostname
    return host.lower() if host else None


async def _get_public_url(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    max_redirects: int = 10,
    user_agent: str | None = None,
    robots: RobotsCache | None = None,
    limiter: PerDomainLimiter | None = None,
) -> httpx.Response | None:
    """Fetch a public URL while validating every redirect target.

    ``httpx`` follows redirects transparently when ``follow_redirects=True``,
    which means an attacker-controlled public URL can redirect the crawler to an
    internal service. Keep redirect handling explicit so each hop is checked
    before any request is sent.

    L-07: when ``robots`` / ``limiter`` are provided, every hop onto a NEW
    domain is politeness-checked before the fetch — the target domain's
    robots.txt is consulted (disallowed → ``None``) and the hop's GET runs
    under that domain's limiter slot with its crawl-delay. The first hop is
    covered by the caller (``run_partition`` already checks robots and holds
    the initial domain's limiter slot), so only cross-domain redirect hops
    re-check here.
    """
    current_url = url
    prev_host: str | None = _hop_host(url)
    for _ in range(max_redirects + 1):
        if not is_public_http_url(current_url):
            return None
        hop_host = _hop_host(current_url)
        new_host = hop_host != prev_host
        if new_host:
            prev_host = hop_host
            if robots is not None and user_agent:
                # Per-hop robots gate for the NEW host.
                try:
                    allowed = await robots.is_allowed(current_url, user_agent)
                except Exception:
                    allowed = True
                if not allowed:
                    return None
        if limiter is not None and new_host and hop_host:
            # L-07: the new host pays its own politeness slot + crawl-delay.
            # (The first hop's slot is held by the caller in run_partition.)
            crawl_delay = robots.crawl_delay(current_url) if robots is not None else None
            # get_with_retries acquires the global fetch slot per attempt.
            async with limiter.domain(hop_host, override_delay=crawl_delay):
                response = await get_with_retries(client, current_url, headers=headers)
        else:
            response = await get_with_retries(client, current_url, headers=headers)
        if not response.is_redirect:
            return response

        location = response.headers.get("Location")
        if not location:
            return response
        current_url = urljoin(str(response.url), location)

    return None


_LIMITER: PerDomainLimiter | None = None
_ROBOTS: RobotsCache | None = None


def _global_limiter(settings) -> PerDomainLimiter:
    global _LIMITER  # noqa: PLW0603
    if _LIMITER is None:
        _LIMITER = PerDomainLimiter(
            concurrency=settings.per_domain_concurrency,
            min_delay_sec=settings.per_domain_delay_sec,
        )
    return _LIMITER


def _global_robots(settings) -> RobotsCache:
    global _ROBOTS  # noqa: PLW0603
    if _ROBOTS is None:
        from awareness.storage.state import StateDB

        state_db = None
        if settings.state_db_url:
            state_db = StateDB(settings.state_db_url)
        _ROBOTS = RobotsCache(state_db=state_db, ttl=settings.robots_cache_ttl_sec)
    return _ROBOTS
