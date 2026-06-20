"""robots.txt cache.

We use the stdlib ``urllib.robotparser`` (RFC 9309-aligned) and add async
fetching with a short TTL. Per-domain crawl-delay is honored where reported.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from awareness.obs.logging import get_logger
from awareness.util.urls import is_public_http_url

if TYPE_CHECKING:
    from awareness.storage.state import StateDB

logger = get_logger("util.robots")


async def _get_public_robots_url(
    client: httpx.AsyncClient,
    url: str,
    user_agent: str,
    *,
    max_redirects: int = 10,
) -> httpx.Response | None:
    """Fetch robots.txt while validating each redirect stays public."""
    current_url = url
    for _ in range(max_redirects + 1):
        if not is_public_http_url(current_url):
            return None
        response = await client.get(current_url, headers={"User-Agent": user_agent})
        if not response.is_redirect:
            return response

        location = response.headers.get("Location")
        if not location:
            return response
        current_url = urljoin(str(response.url), location)

    return None


@dataclass
class RobotsEntry:
    parser: RobotFileParser | None
    expires_at: float
    crawl_delay: float | None
    robots_txt: str | None = None


class RobotsCache:
    """Persistent and in-memory robots cache.

    Use ``await is_allowed(url, user_agent)`` from async code.
    """

    def __init__(self, state_db: StateDB | None = None, ttl: int = 3600, timeout: float = 10.0) -> None:
        self._state_db = state_db
        self._ttl = ttl
        self._timeout = timeout
        self._entries: dict[str, RobotsEntry] = {}
        # We hold a shared client; not strictly necessary but cheaper.
        self._client: httpx.AsyncClient | None = None

    async def _client_lazy(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
                headers={"Accept": "text/plain, */*;q=0.1"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _site_key(url: str) -> str:
        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            return ""
        return f"{parts.scheme.lower()}://{parts.netloc.lower()}"

    async def _load(self, site: str, user_agent: str) -> RobotsEntry:
        url = f"{site}/robots.txt"
        rp = RobotFileParser()
        rp.set_url(url)
        crawl_delay: float | None = None
        robots_txt: str | None = None
        try:
            client = await self._client_lazy()
            resp = await _get_public_robots_url(client, url, user_agent)
            if resp is None:
                rp.parse([])
                robots_txt = ""
            elif resp.status_code == 200 and resp.text:
                robots_txt = resp.text
                rp.parse(robots_txt.splitlines())
                # crawl-delay isn't first-class in RobotFileParser; emulate.
                cd = rp.crawl_delay(user_agent)
                if cd is not None:
                    try:
                        crawl_delay = float(cd)
                    except (TypeError, ValueError):
                        crawl_delay = None
            elif resp.status_code in (401, 403):
                # Treat as DISALLOWED for everything.
                robots_txt = "User-agent: *\nDisallow: /"
                rp.parse(robots_txt.splitlines())
            elif resp.status_code == 404:
                robots_txt = ""
                rp.parse([])  # implicit allow-all
            else:
                robots_txt = ""
                rp.parse([])  # be permissive on transient errors
            return RobotsEntry(
                parser=rp,
                expires_at=time.time() + self._ttl,
                crawl_delay=crawl_delay,
                robots_txt=robots_txt,
            )
        except (httpx.HTTPError, ValueError, OSError) as e:
            logger.warning("robots_fetch_failed", site=site, err=str(e))
            # Be cautious on failure: cache empty/permissive entry briefly.
            rp.parse([])
            return RobotsEntry(
                parser=rp,
                expires_at=time.time() + min(self._ttl, 300),
                crawl_delay=None,
                robots_txt="",
            )

    async def is_allowed(self, url: str, user_agent: str) -> bool:
        site = self._site_key(url)
        if not site:
            return False

        # 1. Check local memory cache
        entry = self._entries.get(site)
        if entry is not None and entry.expires_at >= time.time():
            if entry.parser is None:
                return False
            try:
                return entry.parser.can_fetch(user_agent, url)
            except (ValueError, AttributeError):
                return False

        # 2. Check StateDB
        if self._state_db is not None:
            try:
                row = await asyncio.to_thread(self._state_db.get_robots_cache, site)
                if row is not None and row.expires_at >= time.time():
                    rp = RobotFileParser()
                    rp.set_url(f"{site}/robots.txt")
                    if row.robots_txt is not None:
                        rp.parse(row.robots_txt.splitlines())
                    else:
                        rp.parse([])
                    entry = RobotsEntry(
                        parser=rp,
                        expires_at=row.expires_at,
                        crawl_delay=row.crawl_delay,
                        robots_txt=row.robots_txt,
                    )
                    self._entries[site] = entry
                    if entry.parser is None:
                        return False
                    try:
                        return entry.parser.can_fetch(user_agent, url)
                    except (ValueError, AttributeError):
                        return False
            except Exception as e:
                logger.warning("robots_db_load_failed", site=site, err=str(e))

        # 3. Not in memory/DB or expired -> load and save
        entry = await self._load(site, user_agent)
        self._entries[site] = entry

        if self._state_db is not None:
            try:
                await asyncio.to_thread(
                    self._state_db.set_robots_cache,
                    site,
                    entry.robots_txt,
                    entry.expires_at,
                    entry.crawl_delay,
                )
            except Exception as e:
                logger.warning("robots_db_save_failed", site=site, err=str(e))

        if entry.parser is None:
            return False
        try:
            return entry.parser.can_fetch(user_agent, url)
        except (ValueError, AttributeError):
            return False

    def crawl_delay(self, url: str) -> float | None:
        site = self._site_key(url)
        e = self._entries.get(site)
        if e is None and self._state_db is not None:
            try:
                row = self._state_db.get_robots_cache(site)
                if row is not None and row.expires_at >= time.time():
                    rp = RobotFileParser()
                    rp.set_url(f"{site}/robots.txt")
                    if row.robots_txt is not None:
                        rp.parse(row.robots_txt.splitlines())
                    else:
                        rp.parse([])
                    e = RobotsEntry(
                        parser=rp,
                        expires_at=row.expires_at,
                        crawl_delay=row.crawl_delay,
                        robots_txt=row.robots_txt,
                    )
                    self._entries[site] = e
            except Exception as exc:
                logger.warning("robots_db_crawl_delay_failed", site=site, err=str(exc))
        return e.crawl_delay if e else None
