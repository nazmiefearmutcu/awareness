"""Shared async HTTP helpers: retries, exponential backoff, Retry-After.

Adapters should fetch through these helpers instead of bare ``client.get`` so
that transient failures (timeouts, connection resets, 429/5xx) are retried with
backoff and a genuine 404 is surfaced — not silently swallowed.

Also exposes a process-wide :func:`acquire_fetch_slot` semaphore keyed off
``settings.global_fetch_concurrency`` so concurrent adapters cannot open an
unbounded number of sockets.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from awareness.obs.logging import get_logger

logger = get_logger("util.http")

# Status codes worth retrying (transient/overload). A 404/410 is permanent.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BASE_DELAY = 0.5
DEFAULT_MAX_DELAY = 30.0

# Process-wide cap on concurrent HTTP fetches (open sockets).
_fetch_sem: asyncio.Semaphore | None = None
_fetch_sem_limit: int | None = None
_fetch_sem_init = threading.Lock()


class RetryableHTTPError(Exception):
    """Raised when a request still failed transiently after all attempts.

    Callers should let this propagate so the task layer retries with its own
    backoff lease (see storage.state.fail_task), rather than swallowing it.
    """


def global_fetch_semaphore(limit: int | None = None) -> asyncio.Semaphore:
    """Return the process-wide fetch semaphore, creating it on first use.

    ``limit`` defaults to ``settings.global_fetch_concurrency`` (min 1).
    Recreating when ``limit`` changes is intentional so tests / config reloads
    can resize the cap.
    """
    global _fetch_sem, _fetch_sem_limit  # noqa: PLW0603
    if limit is None:
        from awareness.config import get_settings  # noqa: PLC0415

        limit = int(get_settings().global_fetch_concurrency)
    limit = max(1, int(limit))
    with _fetch_sem_init:
        if _fetch_sem is None or _fetch_sem_limit != limit:
            _fetch_sem = asyncio.Semaphore(limit)
            _fetch_sem_limit = limit
        return _fetch_sem


def reset_global_fetch_semaphore() -> None:
    """Drop the process-wide semaphore (tests / settings reload)."""
    global _fetch_sem, _fetch_sem_limit  # noqa: PLW0603
    with _fetch_sem_init:
        _fetch_sem = None
        _fetch_sem_limit = None


@asynccontextmanager
async def acquire_fetch_slot(limit: int | None = None) -> AsyncIterator[None]:
    """Hold one slot of the process-wide fetch concurrency cap.

    Acquire only around the actual HTTP send — not around backoff sleeps —
    so the cap bounds open sockets rather than idle retry waiters.
    """
    sem = global_fetch_semaphore(limit)
    await sem.acquire()
    try:
        yield
    finally:
        sem.release()


def _backoff_delay(attempt: int, base_delay: float, retry_after: float | None) -> float:
    if retry_after is not None:
        return min(retry_after, DEFAULT_MAX_DELAY)
    return min(base_delay * (2**attempt), DEFAULT_MAX_DELAY)


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None  # HTTP-date form: ignore, fall back to exponential backoff


async def get_with_retries(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
) -> httpx.Response:
    """GET ``url`` with retries on transient errors.

    Returns the final response on success OR on a non-retryable status (e.g.
    404) so the caller can branch on it. Raises :class:`RetryableHTTPError`
    only when a transient failure persists across all attempts.

    Each attempt acquires the process-wide fetch slot for the duration of the
    HTTP call only (backoff sleeps release the slot).
    """
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            async with acquire_fetch_slot():
                resp = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt + 1 >= max_attempts:
                break
            await asyncio.sleep(_backoff_delay(attempt, base_delay, None))
            continue
        if resp.status_code in RETRYABLE_STATUS:
            if attempt + 1 >= max_attempts:
                raise RetryableHTTPError(f"{url} -> {resp.status_code} after {max_attempts} attempts")
            await asyncio.sleep(_backoff_delay(attempt, base_delay, _retry_after_seconds(resp)))
            continue
        return resp  # success OR non-retryable (e.g. 404) — caller decides
    raise RetryableHTTPError(f"{url} failed transiently after {max_attempts} attempts: {last_exc}")
