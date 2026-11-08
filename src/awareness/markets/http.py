"""HTTP layer for collectors: UA profiles, per-host pacing, retries, conditional GET.

Collectors call :meth:`Fetcher.get`. Transient failures (timeouts, connection
errors, 429, 5xx) are retried with exponential backoff and jitter, honouring
``Retry-After``. A final failure raises :class:`FetchError` — collectors let it
propagate so the scheduler records provider health. ``conditional=True`` stores
ETag / Last-Modified validators in the store's kv table and returns a
``status == 304`` result with empty content when nothing changed.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx

from awareness.markets.config import MarketsSettings

Profile = Literal["bot", "browser", "sec"]

# Minimum seconds between requests to the same host (politeness / provider limits).
DEFAULT_HOST_INTERVALS: dict[str, float] = {
    "api.gdeltproject.org": 6.0,
    "www.sec.gov": 0.25,
    "data.sec.gov": 0.25,
    "efts.sec.gov": 0.25,
    "api.nasdaq.com": 0.8,
    "www.reddit.com": 8.0,
    "news.google.com": 2.0,
    "apewisdom.io": 1.0,
    "production.dataviz.cnn.io": 2.0,
    "date.nager.at": 0.3,
}
DEFAULT_INTERVAL = 0.4
RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524}


class KVStore(Protocol):
    def kv_get(self, key: str) -> Any | None: ...
    def kv_set(self, key: str, value: Any) -> None: ...


class FetchError(Exception):
    def __init__(self, url: str, status: int | None, message: str) -> None:
        super().__init__(f"{status or 'ERR'} {url}: {message}")
        self.url = url
        self.status = status
        self.message = message


@dataclass
class FetchResult:
    url: str
    status: int
    content: bytes
    headers: Mapping[str, str]
    fetched_at: datetime
    elapsed_ms: int = 0
    not_modified: bool = False
    final_url: str | None = None
    _text: str | None = field(default=None, repr=False)

    @property
    def text(self) -> str:
        if self._text is None:
            charset = "utf-8"
            ctype = self.headers.get("content-type", "") if self.headers else ""
            if "charset=" in ctype.lower():
                charset = ctype.lower().split("charset=", 1)[1].split(";")[0].strip().strip("\"'") or "utf-8"
            try:
                self._text = self.content.decode(charset, errors="replace")
            except LookupError:
                self._text = self.content.decode("utf-8", errors="replace")
            if self._text.startswith("﻿"):
                self._text = self._text[1:]
        return self._text

    def json(self) -> Any:
        return json.loads(self.text)


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (when - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError):
        return None


class Fetcher:
    """Shared async HTTP client with politeness and conditional requests."""

    def __init__(
        self,
        settings: MarketsSettings,
        kv: KVStore | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        host_intervals: Mapping[str, float] | None = None,
        sleep: Any = asyncio.sleep,
        backoff_base: float = 1.0,
    ) -> None:
        self.settings = settings
        self.kv = kv
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.http_timeout_s, connect=min(10.0, settings.http_timeout_s)),
            follow_redirects=True,
            transport=transport,
            headers={"Accept-Encoding": "gzip, deflate"},
        )
        self._host_intervals = dict(DEFAULT_HOST_INTERVALS)
        if host_intervals:
            self._host_intervals.update(host_intervals)
        self._host_next: dict[str, float] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._sleep = sleep
        self._backoff_base = backoff_base

    def _headers(self, profile: Profile, extra: Mapping[str, str] | None) -> dict[str, str]:
        if profile == "browser":
            headers = {
                "User-Agent": self.settings.browser_user_agent,
                "Accept": "application/json, text/plain, application/xml, text/html, */*",
                "Accept-Language": "en-US,en;q=0.9",
            }
        elif profile == "sec":
            headers = {"User-Agent": self.settings.sec_user_agent, "Accept": "*/*"}
        else:
            headers = {"User-Agent": self.settings.bot_user_agent, "Accept": "*/*"}
        if extra:
            headers.update(extra)
        return headers

    async def _pace(self, host: str) -> None:
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            interval = self._host_intervals.get(host, DEFAULT_INTERVAL)
            now = time.monotonic()
            wait = self._host_next.get(host, 0.0) - now
            if wait > 0:
                await self._sleep(wait)
            self._host_next[host] = time.monotonic() + interval

    async def get(
        self,
        url: str,
        *,
        profile: Profile = "bot",
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        conditional: bool = False,
        max_retries: int = 3,
        timeout: float | None = None,
    ) -> FetchResult:
        host = urlsplit(url).hostname or ""
        req_headers = self._headers(profile, headers)
        cache_key = f"http.validators:{url}" + (f"?{sorted(params.items())}" if params else "")
        if conditional and self.kv is not None:
            validators = self.kv.kv_get(cache_key) or {}
            if validators.get("etag"):
                req_headers["If-None-Match"] = validators["etag"]
            if validators.get("last_modified"):
                req_headers["If-Modified-Since"] = validators["last_modified"]
        last_error: str = "unknown error"
        last_status: int | None = None
        for attempt in range(max_retries + 1):
            await self._pace(host)
            started = time.perf_counter()
            try:
                response = await self._client.get(
                    url,
                    params=params,
                    headers=req_headers,
                    timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"[:300]
                last_status = None
                if attempt < max_retries:
                    await self._sleep(self._backoff(attempt, None))
                    continue
                raise FetchError(url, None, last_error) from exc
            elapsed = int((time.perf_counter() - started) * 1000)
            fetched_at = datetime.now(UTC)
            if response.status_code == 304:
                return FetchResult(
                    url=url,
                    status=304,
                    content=b"",
                    headers=dict(response.headers),
                    fetched_at=fetched_at,
                    elapsed_ms=elapsed,
                    not_modified=True,
                    final_url=str(response.url),
                )
            if 200 <= response.status_code < 300:
                if conditional and self.kv is not None:
                    validators = {
                        "etag": response.headers.get("etag"),
                        "last_modified": response.headers.get("last-modified"),
                    }
                    if validators["etag"] or validators["last_modified"]:
                        self.kv.kv_set(cache_key, validators)
                return FetchResult(
                    url=url,
                    status=response.status_code,
                    content=response.content,
                    headers=dict(response.headers),
                    fetched_at=fetched_at,
                    elapsed_ms=elapsed,
                    final_url=str(response.url),
                )
            last_status = response.status_code
            last_error = response.text[:200].replace("\n", " ") if response.content else response.reason_phrase
            if response.status_code in RETRY_STATUSES and attempt < max_retries:
                await self._sleep(self._backoff(attempt, response.headers.get("retry-after")))
                continue
            raise FetchError(url, last_status, last_error)
        raise FetchError(url, last_status, last_error)  # pragma: no cover - loop always returns/raises

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        hinted = _retry_after_seconds(retry_after)
        if hinted is not None:
            return min(hinted, 60.0)
        return min(self._backoff_base * (2**attempt) + random.uniform(0, self._backoff_base), 30.0)  # noqa: S311

    async def aclose(self) -> None:
        await self._client.aclose()
