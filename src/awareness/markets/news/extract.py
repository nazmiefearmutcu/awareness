"""Article body extraction (trafilatura) and robots.txt gating for full-text fetches.

Only the publishers flagged ``fetch_fulltext`` are fetched; the caller checks the
flag and the paywall. Extraction failures return ``None`` and never raise.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import trafilatura
from dateutil import parser as date_parser
from lxml import etree
from lxml import html as lxml_html

from awareness.markets.http import Fetcher, FetchError

log = logging.getLogger(__name__)

MIN_WORDS = 80
ROBOT_PRODUCT_NAME = "AwarenessMarkets"
_DATE_META_KEYS = frozenset(
    {
        "article:published_time", "pubdate", "publishdate", "publish-date", "date", "dc.date",
        "dc.date.issued", "datepublished", "parsely-pub-date", "sailthru.date", "og:published_time",
    }
)
# A publish date must carry a full calendar date; a bare year parses to a fabricated day.
# ISO dates and month-name dates only: "05/06/2026" is ambiguous (day-first or month-first).
_FULL_DATE_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+[A-Za-z]{3,9}\.?,?\s+\d{4}"
)
_JSONLD_DATE_RE = re.compile(r'"datePublished"\s*:\s*"([^"]+)"')


@dataclass
class ExtractedArticle:
    text: str
    author: str | None
    published_at: datetime | None
    title: str | None
    word_count: int


def _field(doc: Any, name: str) -> Any:
    """trafilatura returns a dict in 1.x and a Document object in 2.x; read either."""
    if doc is None:
        return None
    if isinstance(doc, dict):
        return doc.get(name)
    return getattr(doc, name, None)


def _parse_date(value: object) -> datetime | None:
    if not value or not isinstance(value, str) or not _FULL_DATE_RE.search(value):
        return None
    try:
        parsed = date_parser.parse(value)
    except (ValueError, OverflowError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _published_from_markup(html: str) -> datetime | None:
    """Publish time from explicit page metadata only (meta tags, <time>, JSON-LD).

    trafilatura's own date guess is not used: on pages without a date it can return a
    fabricated value (for example the year of a copyright line).
    """
    try:
        root = lxml_html.fromstring(html)
    except (ValueError, etree.LxmlError):
        return None
    candidates: list[object] = []
    for meta in root.iter("meta"):
        key = (meta.get("property") or meta.get("name") or meta.get("itemprop") or "").strip().lower()
        if key in _DATE_META_KEYS:
            candidates.append(meta.get("content"))
    for node in root.iter("time"):
        candidates.append(node.get("datetime"))
    candidates.extend(_JSONLD_DATE_RE.findall(html))
    for value in candidates:
        parsed = _parse_date(value)
        if parsed is not None:
            return parsed
    return None


def extract_from_html(html: str, *, url: str) -> ExtractedArticle | None:
    """Main-text extraction. ``None`` when the page has fewer than :data:`MIN_WORDS` words."""
    if not html or not html.strip():
        return None
    try:
        doc = trafilatura.bare_extraction(
            html,
            url=url,
            with_metadata=True,
            include_comments=False,
            favor_precision=True,
        )
    except Exception as exc:  # trafilatura raises on malformed input in some versions
        log.warning("extraction failed for %s: %s", url, exc)
        return None
    text = _field(doc, "text")
    if not isinstance(text, str):
        return None
    text = text.strip()
    word_count = len(text.split())
    if word_count < MIN_WORDS:
        return None
    author = _field(doc, "author")
    title = _field(doc, "title")
    return ExtractedArticle(
        text=text,
        author=author.strip() if isinstance(author, str) and author.strip() else None,
        published_at=_published_from_markup(html),
        title=title.strip() if isinstance(title, str) and title.strip() else None,
        word_count=word_count,
    )


@dataclass(frozen=True)
class _Rules:
    mode: Literal["rules", "allow_all", "disallow_all"]
    parser: RobotFileParser | None = None

    def allows(self, url: str, user_agent: str) -> bool:
        if self.mode == "disallow_all":
            return False
        if self.parser is None:
            return True
        return self.parser.can_fetch(user_agent, url)


_ALLOW_ALL = _Rules("allow_all")
_DISALLOW_ALL = _Rules("disallow_all")


class RobotsChecker:
    """Per-origin robots.txt cache.

    Definitive answers are cached: 200 → parsed rules; 404/410 → allow all;
    401/403 → disallow all. Transient failures (5xx, 429, network) allow the current
    page only and are not cached, so the next page re-reads robots.txt and a
    temporary outage cannot disable the check for the life of the process.
    """

    def __init__(self) -> None:
        self._cache: dict[str, _Rules] = {}
        self._warned: set[str] = set()

    async def allowed(self, fetcher: Fetcher, url: str, user_agent: str) -> bool:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return False
        origin = f"{parts.scheme}://{parts.netloc}"
        agent = user_agent or ROBOT_PRODUCT_NAME
        cached = self._cache.get(origin)
        if cached is not None:
            return cached.allows(url, agent)
        rules = await self._load(fetcher, origin)
        if rules is None:
            return True  # transient failure: allow this page, do not cache
        self._cache[origin] = rules
        return rules.allows(url, agent)

    async def _load(self, fetcher: Fetcher, origin: str) -> _Rules | None:
        try:
            result = await fetcher.get(f"{origin}/robots.txt", profile="bot", max_retries=1)
        except FetchError as exc:
            if exc.status in (404, 410):
                return _ALLOW_ALL
            if exc.status in (401, 403):
                return _DISALLOW_ALL
            if origin not in self._warned:
                self._warned.add(origin)
                log.warning("robots.txt unavailable for %s (%s); allowing for now", origin, exc.message)
            return None
        parser = RobotFileParser()
        parser.parse(result.text.splitlines())
        return _Rules("rules", parser)


async def fetch_and_extract(
    fetcher: Fetcher,
    url: str,
    *,
    robots: RobotsChecker | None = None,
    user_agent: str = "",
) -> ExtractedArticle | None:
    """robots gate → fetch the page with the bot profile → extract. Any fetch failure gives ``None``."""
    if robots is not None and not await robots.allowed(fetcher, url, user_agent):
        return None
    try:
        result = await fetcher.get(url, profile="bot")
    except FetchError:
        return None
    return extract_from_html(result.text, url=url)


async def fetch_published_date(
    fetcher: Fetcher,
    url: str,
    *,
    robots: RobotsChecker | None = None,
    user_agent: str = "",
) -> datetime | None:
    """Publication time from the publisher's own page markup (meta/JSON-LD), or ``None``.

    Used for feed items that arrive without a date: the timestamp then comes from the article
    page itself, never from the time we happened to see the item. Works on paywalled pages,
    whose <head> still carries the publication time.
    """
    if robots is not None and not await robots.allowed(fetcher, url, user_agent):
        return None
    try:
        result = await fetcher.get(url, profile="bot", max_retries=1)
    except FetchError:
        return None
    return _published_from_markup(result.text[:400_000])


__all__ = [
    "MIN_WORDS",
    "ExtractedArticle",
    "RobotsChecker",
    "extract_from_html",
    "fetch_and_extract",
    "fetch_published_date",
]
