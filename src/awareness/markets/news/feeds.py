"""News feed ingestion: URL canonicalisation, RSS/Atom and Google News parsing, feed collectors.

The parsers are pure functions over response bytes. The collectors fetch due
feeds through :class:`~awareness.markets.http.Fetcher` and hand each feed's new
items to the news pipeline as soon as that feed completes, so progress persists
even if a run is cut short. Provider failures are recorded per feed in
:class:`HealthTracker`; the collector re-raises only when every feed in the run
failed.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import unescape
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import feedparser

from awareness.markets.collectors.base import CollectorContext, CollectorResult, NewsPipelineProtocol
from awareness.markets.health import HealthTracker
from awareness.markets.http import FetchError
from awareness.markets.models import FeedSpec, NewsItem, Publisher, RawNewsItem, ensure_utc
from awareness.markets.ref.sec_tickers import load_issuers, ticker_to_issuer
from awareness.markets.sources.registry import PublisherRegistry, registered_domain

log = logging.getLogger(__name__)

# Query keys dropped from every URL. ``ref`` is included on purpose: it is a generic
# referrer parameter on most publisher sites, so the rare case where it carries an
# article id is accepted as a loss.
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
_MAX_FEEDS_PER_RUN = 40
_PIPELINE_BATCH = 25
_MAX_BACKOFF_S = 1800
_STAGGER_WINDOW_S = 600
_NEXT_DUE_KEY = "feeds.next_due:{}"
_BY_PREFIX_RE = re.compile(r"^by\s+", re.IGNORECASE)
# Real markup only: a tag must start with a letter after "<" (so "< 5%" is text).
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")
# Block-level tags become a word break; inline tags are removed without a space.
_BLOCK_TAG_RE = re.compile(
    r"</?(?:p|br|div|li|ul|ol|h[1-6]|tr|td|th|table|section|article|blockquote)\b[^>]*>", re.IGNORECASE
)
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

    Query segments are kept exactly as written (``?flag`` stays ``?flag``; no
    re-encoding), then sorted. Idempotent. Values that do not look like absolute
    URLs are returned stripped and unchanged.
    """
    raw = url.strip()
    parts = urlsplit(raw)
    if not parts.scheme or not parts.netloc:
        return raw
    segments = sorted(
        segment
        for segment in parts.query.split("&")
        if segment and not _is_tracking(segment.split("=", 1)[0])
    )
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "&".join(segments), ""))


def clean_text(text: str | None, max_len: int | None = None) -> str | None:
    """Strip markup, unescape entities, collapse whitespace. ``None`` when nothing is left.

    Markup is removed before unescaping, so entity-encoded or raw comparison
    operators ("&lt;5%", "< 5%") survive as text. Block tags become a space;
    inline tags are removed without one, so ``Tom<b>son</b>'s`` reads ``Tomson's``.
    """
    if not text:
        return None
    without_comments = _COMMENT_RE.sub("", str(text))
    without_blocks = _BLOCK_TAG_RE.sub(" ", without_comments)
    value = unescape(_TAG_RE.sub("", without_blocks))
    value = _WS_RE.sub(" ", value).strip()
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
    """Parse an RSS/Atom payload into raw items.

    Entries without a usable URL or title are skipped; a malformed entry (for example
    an unparseable link) is skipped on its own without failing the feed.
    """
    parsed = feedparser.parse(content)
    fetched = ensure_utc(fetched_at)
    language = feed.language or (publisher.languages[0] if publisher.languages else None)
    out: list[RawNewsItem] = []
    for entry in parsed.entries:
        try:
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
        except ValueError as exc:
            log.debug("skipping malformed entry in %s: %s", feed.id, exc)
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


def _gnews_origin(
    registry: PublisherRegistry, source_href: str | None, source_name: str | None
) -> Publisher | None:
    """Origin publisher for a Google News item.

    When the ``<source>`` carries a URL it must resolve by domain; a name fallback
    applies only when no URL was given (otherwise an unlisted domain could borrow
    the provenance of a registered publisher with the same name).
    """
    if source_href:
        return registry.by_domain(source_href)
    return registry.by_name(source_name)


def parse_google_news(
    content: bytes,
    *,
    registry: PublisherRegistry,
    feed_id: str,
    fetched_at: datetime,
    max_tier: int = 3,
) -> list[RawNewsItem]:
    """Parse a Google News RSS search result, keeping only allowlisted origins of tier <= ``max_tier``.

    Each item carries ``<source url="...">Name</source>``. Unknown or low-tier origins are dropped.
    """
    parsed = feedparser.parse(content)
    fetched = ensure_utc(fetched_at)
    out: list[RawNewsItem] = []
    seen: set[str] = set()
    for entry in parsed.entries:
        try:
            url = _entry_url(entry)
            if url is None or url in seen:
                continue
            source = entry.get("source")
            source_dict = source if isinstance(source, dict) else {}
            source_href = source_dict.get("href") or source_dict.get("url")
            source_name = clean_text(source_dict.get("title"))
            publisher = _gnews_origin(registry, source_href, source_name)
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
        except ValueError as exc:
            log.debug("skipping malformed google news entry for %s: %s", feed_id, exc)
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
    ctx: CollectorContext,
    pipeline: NewsPipelineProtocol,
    raws: Iterable[RawNewsItem],
    seen: set[str] | None = None,
) -> int:
    """Drop stale, already-stored and already-sent-this-run items; send the rest in batches of 25.

    ``seen`` is the run-level set of item ids, so one story carried by two feeds
    reaches the pipeline once per run.
    """
    cutoff = ctx.now() - timedelta(days=ctx.settings.max_item_age_days)
    fresh = _dedupe_by_id(r for r in raws if r.published_at is None or r.published_at >= cutoff)
    if seen is not None:
        fresh = {item_id: raw for item_id, raw in fresh.items() if item_id not in seen}
    if not fresh:
        return 0
    known = ctx.store.news_exists(fresh.keys())
    new = [raw for item_id, raw in fresh.items() if item_id not in known]
    if seen is not None:
        seen.update(fresh.keys())
    stored = 0
    for start in range(0, len(new), _PIPELINE_BATCH):
        stored += len(await pipeline.process(new[start : start + _PIPELINE_BATCH]))
    return stored


def _stagger_offset(publisher: Publisher, feed: FeedSpec) -> int:
    """Initial delay for community feeds: a stable 0 to 600 s offset derived from the feed id.

    Reddit answers 429 when subreddits are hit back-to-back, so their first polls are
    spread across the window instead of all firing in the same run.
    """
    if publisher.kind != "community":
        return 0
    return zlib.crc32(feed.id.encode("utf-8")) % (_STAGGER_WINDOW_S + 1)


def _next_due(ctx: CollectorContext, publisher: Publisher, feed: FeedSpec, now: datetime) -> datetime:
    """Stored next-poll time; a feed with none yet gets its initial (possibly staggered) time."""
    key = _NEXT_DUE_KEY.format(feed.id)
    stored = _parse_ts(ctx.store.kv_get(key))
    if stored is not None:
        return stored
    initial = now + timedelta(seconds=_stagger_offset(publisher, feed))
    ctx.store.kv_set(key, initial.isoformat())
    return initial


def _schedule_next(ctx: CollectorContext, feed: FeedSpec, now: datetime, failure_streak: int) -> None:
    """Next poll = poll_seconds plus jitter; a failing streak doubles the delay (capped at 30 min,
    never shorter than poll_seconds).

    Jitter is upward-only (+0…10 %) so a feed is never polled sooner than its ``poll_seconds`` —
    community hosts such as Reddit rate-limit hard below their floor.
    """
    base = max(feed.poll_seconds, 1)
    if failure_streak == 0:
        delay = base
    else:
        delay = max(base, min(base * 2 ** min(failure_streak, 5), _MAX_BACKOFF_S))
    jitter = random.uniform(1.0, 1.1)  # noqa: S311 - scheduling jitter, not security
    ctx.store.kv_set(_NEXT_DUE_KEY.format(feed.id), (now + timedelta(seconds=delay * jitter)).isoformat())


@dataclass
class _Polled:
    feed: FeedSpec
    ok: bool
    raws: list[RawNewsItem]
    failure_streak: int


class FeedCollector:
    """Polls due RSS/Atom publisher feeds, most overdue first, at most 40 per run.

    Each feed's new items go to the pipeline as soon as that feed completes. Failing
    feeds are isolated; the run raises only when every feed in it failed.
    """

    name = "news.feeds"
    interval_s = 60
    timeout_s = 900
    lock_group: str | None = None

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        if ctx.pipeline is None:
            raise RuntimeError("news.feeds requires a news pipeline")
        pipeline = ctx.pipeline
        now = ctx.now()
        candidates: list[tuple[datetime, Publisher, FeedSpec]] = []
        for publisher, feed in ctx.registry.feeds():
            if feed.kind not in ("rss", "atom"):
                continue
            due_at = _next_due(ctx, publisher, feed, now)
            if due_at <= now:
                candidates.append((due_at, publisher, feed))
        candidates.sort(key=lambda item: item[0])
        batch = candidates[:_MAX_FEEDS_PER_RUN]
        deferred = len(candidates) - len(batch)
        if not batch:
            return CollectorResult(items=0, notes=["feeds_due=0", "feeds_failed=0"])

        semaphore = asyncio.Semaphore(_FEED_CONCURRENCY)

        async def fetch(publisher: Publisher, feed: FeedSpec) -> _Polled:
            async with semaphore:
                return await self._poll(ctx, publisher, feed)

        tasks = [asyncio.ensure_future(fetch(p, f)) for _, p, f in batch]
        stored = 0
        failed = 0
        seen: set[str] = set()
        try:
            for finished in asyncio.as_completed(tasks):
                polled = await finished
                if polled.ok:
                    stored += await _ingest(ctx, pipeline, polled.raws, seen)
                else:
                    failed += 1
                _schedule_next(ctx, polled.feed, now, polled.failure_streak)
        finally:
            for task in tasks:
                task.cancel()
        if failed == len(batch):
            raise RuntimeError("all feeds failed")
        return CollectorResult(
            items=stored,
            notes=[f"feeds_due={len(batch)}", f"feeds_failed={failed}", f"feeds_deferred={deferred}"],
        )

    async def _poll(self, ctx: CollectorContext, publisher: Publisher, feed: FeedSpec) -> _Polled:
        """Fetch and parse one feed and record its health. Scheduling happens after ingestion."""
        provider = f"feed.{feed.id}"
        health = HealthTracker(ctx.store, now=ctx.now)
        try:
            result = await ctx.http.get(feed.url, profile=feed.profile, conditional=True)
            if result.not_modified:
                health.ok(provider, 0, feed.poll_seconds)
                return _Polled(feed, True, [], 0)
            raws = parse_feed(result.content, feed=feed, publisher=publisher, fetched_at=result.fetched_at)
        except (FetchError, ValueError, OSError) as exc:
            message = str(exc) if isinstance(exc, FetchError) else f"{type(exc).__name__}: {exc}"
            health.error(provider, message, feed.poll_seconds)
            row = ctx.store.get_health(provider)
            log.warning("feed failed: %s (%s)", feed.id, message)
            return _Polled(feed, False, [], row.consecutive_failures if row else 1)
        health.ok(provider, len(raws), feed.poll_seconds)
        return _Polled(feed, True, raws, 0)


class GoogleNewsCollector:
    """Google News search per watchlist symbol; only allowlisted origins of tier <= 3 are kept."""

    name = "news.gnews"
    interval_s = 900
    timeout_s = 900
    lock_group: str | None = None

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        if ctx.pipeline is None:
            raise RuntimeError("news.gnews requires a news pipeline")
        pipeline = ctx.pipeline
        symbols = ctx.store.get_watchlist() or list(ctx.settings.watchlist)
        issuers = ticker_to_issuer(load_issuers(ctx.settings.ref_dir))
        queries: dict[str, str] = {}
        skipped: list[str] = []
        for symbol in symbols:
            issuer = issuers.get(symbol.strip().upper())
            query = watchlist_query(symbol, issuer.title if issuer else None)
            if query:
                queries[symbol] = query
            else:
                skipped.append(symbol)
        notes = [f"gnews_skipped_no_issuer={len(skipped)}"] if skipped else []
        if skipped:
            log.warning(
                "google news skipped %d symbols with no issuer title (SEC cache missing?): %s",
                len(skipped),
                ",".join(skipped),
            )
        if not queries:
            return CollectorResult(items=0, notes=["gnews_queries=0", *notes])

        stored = 0
        failed = 0
        seen: set[str] = set()
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
            stored += await _ingest(ctx, pipeline, items, seen)
        if failed == len(queries):
            raise RuntimeError("all google news queries failed")
        return CollectorResult(
            items=stored,
            notes=[f"gnews_queries={len(queries)}", f"gnews_failed={failed}", *notes],
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
