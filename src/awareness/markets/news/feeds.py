"""News feed ingestion: URL canonicalisation, RSS/Atom and Google News parsing, feed collectors.

The parsers are pure functions over response bytes. The collectors fetch due
feeds through :class:`~awareness.markets.http.Fetcher`, drop stale and
already-stored items and hand the rest to the news pipeline in one call.
Provider failures are recorded per feed in :class:`HealthTracker`; the
collector re-raises only when every due feed failed.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from html import unescape
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser

from awareness.markets.collectors.base import CollectorContext, CollectorResult, NewsPipelineProtocol
from awareness.markets.health import HealthTracker
from awareness.markets.http import FetchError
from awareness.markets.models import FeedSpec, NewsItem, Publisher, RawNewsItem, ensure_utc
from awareness.markets.ref.sec_tickers import load_issuers, ticker_to_issuer
from awareness.markets.sources.registry import PublisherRegistry, registered_domain

log = logging.getLogger(__name__)

TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
        "fbclid", "gclid", "dclid", "gbraid", "wbraid", "msclkid", "yclid", "twclid",
        "mc_cid", "mc_eid", "cmpid", "ref", "ref_src", "src", "guccounter", "guce_referrer",
        "guce_referrer_sig", "ncid", "ocid", "soc_src", "soc_trk", "taid", "igshid", "icid",
        "_ga", "sr_share",
    }
)
_TRACKING_PREFIXES = ("utm_",)
_SUMMARY_MAX = 1200
_BODY_MIN = 600
_CATEGORY_TAG_MAX = 6
_FEED_CONCURRENCY = 8
_MAX_BACKOFF_S = 1800
_NEXT_DUE_KEY = "feeds.next_due:{}"
_BY_PREFIX_RE = re.compile(r"^by\s+", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")

CRYPTO_QUERIES: dict[str, str] = {
    "BTC": "Bitcoin",
    "ETH": "Ethereum",
    "SOL": "Solana",
    "XRP": "XRP",
    "BNB": "BNB",
    "DOGE": "Dogecoin",
    "ADA": "Cardano",
}
ETF_QUERIES: dict[str, str] = {"SPY": '"S&P 500" when:1d', "QQQ": '"Nasdaq 100" when:1d'}
_LEGAL_SUFFIX_TOKENS = frozenset(
    {
        "inc", "corp", "corporation", "ltd", "limited", "plc", "holdings", "holding", "co",
        "company", "group", "llc", "lp", "ag", "se", "sa", "s.a", "nv", "n.v", "class", "a", "b", "c",
    }
)


def _is_tracking(key: str) -> bool:
    lowered = key.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(_TRACKING_PREFIXES)


def canonicalize_url(url: str) -> str:
    """Lowercase scheme and host, drop fragment and tracking params, sort the rest.

    Idempotent: ``canonicalize_url(canonicalize_url(u)) == canonicalize_url(u)``.
    Values that do not look like absolute URLs are returned stripped and unchanged.
    """
    raw = url.strip()
    parts = urlsplit(raw)
    if not parts.scheme or not parts.netloc:
        return raw
    query = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking(key)
    )
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(query), ""))


def clean_text(text: str | None, max_len: int | None = None) -> str | None:
    """Unescape entities, strip markup, collapse whitespace. ``None`` when nothing is left."""
    if not text:
        return None
    value = _WS_RE.sub(" ", _TAG_RE.sub(" ", unescape(str(text)))).strip()
    if not value:
        return None
    if max_len is not None and len(value) > max_len:
        value = value[: max_len - 1].rstrip() + "…"
    return value


def _entry_url(entry: Any) -> str | None:
    for candidate in (entry.get("link"), entry.get("id")):
        if not candidate:
            continue
        text = str(candidate).strip()
        if text.lower().startswith(("http://", "https://")):
            return canonicalize_url(text)
    return None


def _entry_time(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=UTC)
            except (TypeError, ValueError):
                return None
    return None


def _entry_tags(entry: Any) -> list[str]:
    out: list[str] = []
    for tag in entry.get("tags") or []:
        term = str(tag.get("term") or "").strip().lower() if isinstance(tag, dict) else ""
        if term and term not in out:
            out.append(term)
        if len(out) >= _CATEGORY_TAG_MAX:
            break
    return out


def _clean_author(value: object) -> str | None:
    text = clean_text(str(value)) if value else None
    if not text:
        return None
    text = _BY_PREFIX_RE.sub("", text).strip()
    return text or None


def _entry_body(entry: Any) -> str | None:
    for block in (entry.get("content") or [])[:1]:
        value = clean_text(block.get("value")) if isinstance(block, dict) else None
        if value and len(value) >= _BODY_MIN:
            return value
    return None


def parse_feed(
    content: bytes, *, feed: FeedSpec, publisher: Publisher, fetched_at: datetime
) -> list[RawNewsItem]:
    """Parse an RSS/Atom payload into raw items. Entries without a usable URL or title are skipped."""
    parsed = feedparser.parse(content)
    fetched = ensure_utc(fetched_at)
    language = feed.language or (publisher.languages[0] if publisher.languages else None)
    out: list[RawNewsItem] = []
    for entry in parsed.entries:
        url = _entry_url(entry)
        title = clean_text(entry.get("title"))
        if url is None or not title:
            continue
        summary = clean_text(entry.get("summary") or entry.get("description"), max_len=_SUMMARY_MAX)
        if summary == title:
            summary = None
        tags = _entry_tags(entry)
        out.append(
            RawNewsItem(
                url=url,
                title=title,
                summary=summary,
                published_at=_entry_time(entry),
                author=_clean_author(entry.get("author")),
                feed_id=feed.id,
                publisher_id=publisher.id,
                categories=list(dict.fromkeys([*feed.categories, *tags])),
                language=language,
                body_text=_entry_body(entry),
                fetched_at=fetched,
            )
        )
    return out


def google_news_url(query: str, *, hl: str = "en-US", gl: str = "US", ceid: str = "US:en") -> str:
    """Google News RSS search URL for ``query``."""
    return "https://news.google.com/rss/search?" + urlencode({"q": query, "hl": hl, "gl": gl, "ceid": ceid})


def _strip_source_suffix(title: str, source_name: str | None) -> str:
    if not source_name:
        return title
    suffix = f" - {source_name}"
    if title.lower().endswith(suffix.lower()):
        return title[: -len(suffix)].rstrip()
    return title


def parse_google_news(
    content: bytes,
    *,
    registry: PublisherRegistry,
    feed_id: str,
    fetched_at: datetime,
    max_tier: int = 3,
) -> list[RawNewsItem]:
    """Parse a Google News RSS search result, keeping only allowlisted origins of tier <= ``max_tier``.

    Each item carries ``<source url="...">Name</source>``; the origin must resolve through the
    registry by domain (preferred) or by name. Unknown or low-tier origins are dropped.
    """
    parsed = feedparser.parse(content)
    fetched = ensure_utc(fetched_at)
    out: list[RawNewsItem] = []
    seen: set[str] = set()
    for entry in parsed.entries:
        url = _entry_url(entry)
        if url is None or url in seen:
            continue
        source = entry.get("source")
        source_dict = source if isinstance(source, dict) else {}
        source_href = source_dict.get("href") or source_dict.get("url")
        source_name = clean_text(source_dict.get("title"))
        publisher = registry.by_domain(source_href) or registry.by_name(source_name)
        if publisher is None or publisher.tier > max_tier:
            continue
        origin_name = source_name or publisher.name
        title = clean_text(entry.get("title"))
        if not title:
            continue
        title = _strip_source_suffix(title, origin_name)
        if not title:
            continue
        seen.add(url)
        out.append(
            RawNewsItem(
                url=url,
                title=title,
                published_at=_entry_time(entry),
                feed_id=feed_id,
                publisher_id=publisher.id,
                language=publisher.languages[0] if publisher.languages else None,
                origin_publisher_name=origin_name,
                origin_publisher_domain=registered_domain(source_href) or publisher.domain,
                fetched_at=fetched,
            )
        )
    return out


def _short_issuer_name(title: str) -> str:
    """``APPLE INC.`` → ``Apple``; ``Berkshire Hathaway Inc Class B`` → ``Berkshire Hathaway``."""
    words = title.replace(",", " ").split()
    if title.isupper():
        words = [w.capitalize() for w in words]
    while len(words) > 1 and words[-1].lower().strip(".") in _LEGAL_SUFFIX_TOKENS:
        words.pop()
    return " ".join(words) or title


def watchlist_query(symbol: str, issuer_title: str | None) -> str | None:
    """Google News query for a watchlist symbol; ``None`` when no honest query can be built."""
    ticker = symbol.strip().upper()
    if ticker in CRYPTO_QUERIES:
        return f"{CRYPTO_QUERIES[ticker]} when:2d"
    if ticker in ETF_QUERIES:
        return ETF_QUERIES[ticker]
    if not issuer_title:
        return None
    return f'"{_short_issuer_name(issuer_title)}" OR {ticker} stock when:2d'


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return ensure_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def _dedupe_by_id(raws: Iterable[RawNewsItem]) -> dict[str, RawNewsItem]:
    unique: dict[str, RawNewsItem] = {}
    for raw in raws:
        unique.setdefault(NewsItem.id_for_url(raw.url), raw)
    return unique


async def _ingest(
    ctx: CollectorContext, pipeline: NewsPipelineProtocol, raws: Iterable[RawNewsItem]
) -> int:
    """Drop stale and already-stored items, send the rest to the pipeline in one call."""
    cutoff = ctx.now() - timedelta(days=ctx.settings.max_item_age_days)
    fresh = _dedupe_by_id(r for r in raws if r.published_at is None or r.published_at >= cutoff)
    if not fresh:
        return 0
    known = ctx.store.news_exists(fresh.keys())
    new = [raw for item_id, raw in fresh.items() if item_id not in known]
    if not new:
        return 0
    stored = await pipeline.process(new)
    return len(stored)


def _is_due(ctx: CollectorContext, feed: FeedSpec, now: datetime) -> bool:
    due_at = _parse_ts(ctx.store.kv_get(_NEXT_DUE_KEY.format(feed.id)))
    return due_at is None or due_at <= now


def _schedule_next(ctx: CollectorContext, feed: FeedSpec, now: datetime, failure_streak: int) -> None:
    """Next poll = poll_seconds ±10 % jitter; a failing streak doubles the delay (cap 30 min)."""
    base = max(feed.poll_seconds, 1)
    delay = base if failure_streak == 0 else min(base * 2 ** min(failure_streak, 5), _MAX_BACKOFF_S)
    jitter = random.uniform(0.9, 1.1)  # noqa: S311 - scheduling jitter, not security
    ctx.store.kv_set(_NEXT_DUE_KEY.format(feed.id), (now + timedelta(seconds=delay * jitter)).isoformat())


class FeedCollector:
    """Polls every due RSS/Atom publisher feed; one failing feed never blocks the others."""

    name = "news.feeds"
    interval_s = 60
    lock_group: str | None = None

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        if ctx.pipeline is None:
            raise RuntimeError("news.feeds requires a news pipeline")
        now = ctx.now()
        due = [
            (publisher, feed)
            for publisher, feed in ctx.registry.feeds()
            if feed.kind in ("rss", "atom") and _is_due(ctx, feed, now)
        ]
        if not due:
            return CollectorResult(items=0, notes=["feeds_due=0", "feeds_failed=0"])

        semaphore = asyncio.Semaphore(_FEED_CONCURRENCY)

        async def guarded(publisher: Publisher, feed: FeedSpec) -> tuple[bool, list[RawNewsItem]]:
            async with semaphore:
                return await self._poll(ctx, publisher, feed)

        outcomes = await asyncio.gather(*(guarded(p, f) for p, f in due))
        failed = sum(1 for ok, _ in outcomes if not ok)
        raws = [raw for _, items in outcomes for raw in items]
        stored = await _ingest(ctx, ctx.pipeline, raws)
        if failed == len(due):
            raise RuntimeError("all feeds failed")
        return CollectorResult(
            items=stored, notes=[f"feeds_due={len(due)}", f"feeds_failed={failed}"]
        )

    async def _poll(
        self, ctx: CollectorContext, publisher: Publisher, feed: FeedSpec
    ) -> tuple[bool, list[RawNewsItem]]:
        """Fetch and parse one feed, record its health and schedule the next poll."""
        now = ctx.now()
        provider = f"feed.{feed.id}"
        health = HealthTracker(ctx.store, now=ctx.now)
        try:
            result = await ctx.http.get(feed.url, profile=feed.profile, conditional=True)
            if result.not_modified:
                health.ok(provider, 0, feed.poll_seconds)
                _schedule_next(ctx, feed, now, 0)
                return True, []
            raws = parse_feed(result.content, feed=feed, publisher=publisher, fetched_at=result.fetched_at)
        except (FetchError, ValueError, OSError) as exc:
            message = str(exc) if isinstance(exc, FetchError) else f"{type(exc).__name__}: {exc}"
            health.error(provider, message, feed.poll_seconds)
            row = ctx.store.get_health(provider)
            _schedule_next(ctx, feed, now, row.consecutive_failures if row else 1)
            log.warning("feed failed: %s (%s)", feed.id, message)
            return False, []
        health.ok(provider, len(raws), feed.poll_seconds)
        _schedule_next(ctx, feed, now, 0)
        return True, raws


class GoogleNewsCollector:
    """Google News search per watchlist symbol; only allowlisted origins of tier <= 3 are kept."""

    name = "news.gnews"
    interval_s = 900
    lock_group: str | None = None

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        if ctx.pipeline is None:
            raise RuntimeError("news.gnews requires a news pipeline")
        symbols = ctx.store.get_watchlist() or list(ctx.settings.watchlist)
        issuers = ticker_to_issuer(load_issuers(ctx.settings.ref_dir))
        queries: dict[str, str] = {}
        for symbol in symbols:
            issuer = issuers.get(symbol.strip().upper())
            query = watchlist_query(symbol, issuer.title if issuer else None)
            if query:
                queries[symbol] = query
        if not queries:
            return CollectorResult(items=0, notes=["gnews_queries=0"])

        raws: list[RawNewsItem] = []
        failed = 0
        for symbol, query in queries.items():
            provider = f"gnews.{symbol}"
            health = HealthTracker(ctx.store, now=ctx.now)
            try:
                result = await ctx.http.get(google_news_url(query), profile="browser")
            except FetchError as exc:
                failed += 1
                health.error(provider, exc.message, self.interval_s)
                log.warning("google news query failed for %s: %s", symbol, exc.message)
                continue
            items = parse_google_news(
                result.content,
                registry=ctx.registry,
                feed_id=f"gnews.{symbol}",
                fetched_at=result.fetched_at,
            )
            health.ok(provider, len(items), self.interval_s)
            raws.extend(items)

        stored = await _ingest(ctx, ctx.pipeline, raws)
        if failed == len(queries):
            raise RuntimeError("all google news queries failed")
        return CollectorResult(
            items=stored, notes=[f"gnews_queries={len(queries)}", f"gnews_failed={failed}"]
        )


__all__ = [
    "TRACKING_PARAMS",
    "FeedCollector",
    "GoogleNewsCollector",
    "canonicalize_url",
    "clean_text",
    "google_news_url",
    "parse_feed",
    "parse_google_news",
    "watchlist_query",
]
