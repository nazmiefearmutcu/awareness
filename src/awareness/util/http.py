"""Shared async HTTP helpers: retries, exponential backoff, Retry-After.

Adapters should fetch through these helpers instead of bare ``client.get`` so
that transient failures (timeouts, connection resets, 429/5xx) are retried with
backoff and a genuine 404 is surfaced — not silently swallowed.

Also exposes:

* a process-wide :func:`acquire_fetch_slot` semaphore keyed off
  ``settings.global_fetch_concurrency`` so concurrent adapters cannot open an
  unbounded number of sockets;
* a process-wide pooled :func:`get_shared_async_client` so adapters reuse TCP /
  TLS connections instead of constructing a fresh ``httpx.AsyncClient`` per
  feed/sitemap/fetch.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from awareness.obs.logging import get_logger

logger = get_logger("util.http")

# Status codes worth retrying (transient/overload). A 404/410 is permanent.
# 408 Request Timeout is transient (client may retry with the same request).
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BASE_DELAY = 0.5
DEFAULT_MAX_DELAY = 30.0

# Process-wide cap on concurrent HTTP fetches (open sockets).
_fetch_sem: asyncio.Semaphore | None = None
_fetch_sem_limit: int | None = None
_fetch_sem_init = threading.Lock()

# Process-wide pooled httpx clients keyed by (timeout, follow_redirects).
# Reuse avoids per-request TCP/TLS setup across feed/sitemap/tail fetches.
_shared_clients: dict[tuple[float, bool], httpx.AsyncClient] = {}
_shared_clients_lock = threading.Lock()


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


def _pool_limits(max_connections: int | None = None) -> httpx.Limits:
    """Connection-pool limits sized from ``global_fetch_concurrency``."""
    if max_connections is None:
        from awareness.config import get_settings  # noqa: PLC0415

        max_connections = int(get_settings().global_fetch_concurrency)
    max_connections = max(4, int(max_connections))
    keepalive = max(2, max_connections // 2)
    return httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=keepalive,
    )


async def get_shared_async_client(
    *,
    timeout: float | None = None,
    follow_redirects: bool = True,
    max_connections: int | None = None,
) -> httpx.AsyncClient:
    """Return a long-lived process-wide ``AsyncClient`` (connection pool).

    Clients are keyed by ``(timeout, follow_redirects)`` so callers that need
    different redirect policy (e.g. tail recrawl with manual redirect checks)
    do not clobber feed/sitemap clients. Do **not** close the returned client
    after a single request — use :func:`aclose_shared_async_clients` at process
    shutdown or in tests via :func:`reset_shared_async_clients`.

    ``timeout`` defaults to ``settings.request_timeout_sec``.
    """
    if timeout is None:
        from awareness.config import get_settings  # noqa: PLC0415

        timeout = float(get_settings().request_timeout_sec)
    timeout_f = float(timeout)
    key = (timeout_f, bool(follow_redirects))
    with _shared_clients_lock:
        client = _shared_clients.get(key)
        if client is not None and not client.is_closed:
            return client
        client = httpx.AsyncClient(
            timeout=timeout_f,
            follow_redirects=follow_redirects,
            limits=_pool_limits(max_connections),
        )
        _shared_clients[key] = client
        return client


async def aclose_shared_async_clients() -> None:
    """Close and drop every pooled client (graceful shutdown)."""
    with _shared_clients_lock:
        clients = list(_shared_clients.values())
        _shared_clients.clear()
    for client in clients:
        try:
            await client.aclose()
        except Exception as exc:  # noqa: BLE001 — best-effort close
            logger.warning("shared_http_client_close_failed", err=str(exc))


def reset_shared_async_clients() -> None:
    """Drop pooled clients without awaiting close (sync tests / reload).

    Prefer :func:`aclose_shared_async_clients` when an event loop is available.
    Remaining open sockets are GC'd with the client objects.
    """
    with _shared_clients_lock:
        _shared_clients.clear()


def shared_async_client_pool_size() -> int:
    """Number of live pooled clients (tests / diagnostics)."""
    with _shared_clients_lock:
        return sum(1 for c in _shared_clients.values() if not c.is_closed)


def _backoff_delay(attempt: int, base_delay: float, retry_after: float | None) -> float:
    if retry_after is not None:
        return min(retry_after, DEFAULT_MAX_DELAY)
    return min(base_delay * (2**attempt), DEFAULT_MAX_DELAY)


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Parse ``Retry-After`` as delta-seconds or HTTP-date.

    Returns seconds to wait (clamped ≥ 0), or ``None`` when the header is
    missing / unparseable so callers fall back to exponential backoff.
    """
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    # Delta-seconds (preferred / common for 429/503).
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    # HTTP-date form (RFC 7231): absolute timestamp → delay from now.
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delay = (dt - datetime.now(UTC)).total_seconds()
    return max(0.0, delay)


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
