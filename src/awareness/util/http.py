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
import re
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from awareness.obs.logging import get_logger
from awareness.obs.metrics import get_metrics

logger = get_logger("util.http")

# Charset sniffing: Content-Type header, HTML meta, then optional detector.
_CHARSET_PARAM_RE = re.compile(
    r"charset\s*=\s*['\"]?([a-zA-Z0-9_.:\-]+)",
    re.IGNORECASE,
)
# HTML5 <meta charset="…"> and HTML4 http-equiv Content-Type.
_META_CHARSET_RE = re.compile(
    rb"""<meta\b[^>]*?\bcharset\s*=\s*['"]?\s*([a-zA-Z0-9_.:\-]+)""",
    re.IGNORECASE,
)
_META_HTTP_EQUIV_RE = re.compile(
    rb"""<meta\b[^>]*?\bhttp-equiv\s*=\s*['"]?\s*content-type['"]?[^>]*?\bcontent\s*=\s*['"][^'"]*?\bcharset\s*=\s*([a-zA-Z0-9_.:\-]+)""",
    re.IGNORECASE,
)
# Alternate order: content= before http-equiv.
_META_HTTP_EQUIV_RE_ALT = re.compile(
    rb"""<meta\b[^>]*?\bcontent\s*=\s*['"][^'"]*?\bcharset\s*=\s*([a-zA-Z0-9_.:\-]+)[^'"]*['"][^>]*?\bhttp-equiv\s*=\s*['"]?\s*content-type""",
    re.IGNORECASE,
)
# Aliases publishers still emit; map to codecs Python knows.
_CHARSET_ALIASES: dict[str, str] = {
    "utf8": "utf-8",
    "utf-8": "utf-8",
    "utf_8": "utf-8",
    "ascii": "ascii",
    "us-ascii": "ascii",
    "latin1": "latin-1",
    "latin-1": "latin-1",
    "iso-8859-1": "latin-1",
    "iso8859-1": "latin-1",
    "iso_8859_1": "latin-1",
    "iso-8859-9": "iso-8859-9",
    "iso8859-9": "iso-8859-9",
    "windows-1252": "cp1252",
    "cp1252": "cp1252",
    "win-1252": "cp1252",
    "windows-1254": "cp1254",
    "cp1254": "cp1254",
    "gb2312": "gb18030",
    "gbk": "gb18030",
    "gb18030": "gb18030",
    "big5": "big5",
    "shift_jis": "shift_jis",
    "shift-jis": "shift_jis",
    "sjis": "shift_jis",
    "euc-jp": "euc_jp",
    "euc-kr": "euc_kr",
    "koi8-r": "koi8-r",
}

# Status codes worth retrying (transient/overload). A 404/410 is permanent.
# 408 Request Timeout is transient (client may retry with the same request).
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BASE_DELAY = 0.5
DEFAULT_MAX_DELAY = 30.0
# Cap connect phase so hung SYN/TLS handshakes fail fast while still allowing
# long reads for large feed/sitemap bodies (full budget goes to read/write).
DEFAULT_CONNECT_TIMEOUT_CAP = 10.0
DEFAULT_CONNECT_TIMEOUT_FLOOR = 1.0
DEFAULT_CONNECT_TIMEOUT_FRACTION = 0.25

# Process-wide cap on concurrent HTTP fetches (open sockets).
_fetch_sem: asyncio.Semaphore | None = None
_fetch_sem_limit: int | None = None
_fetch_sem_init = threading.Lock()

# Process-wide pooled httpx clients keyed by (timeout, follow_redirects).
# Reuse avoids per-request TCP/TLS setup across feed/sitemap/tail fetches.
_shared_clients: dict[tuple[float, bool], httpx.AsyncClient] = {}
_shared_clients_lock = threading.Lock()


def build_http_timeout(total_sec: float) -> httpx.Timeout:
    """Build a split connect/read/write timeout from a single budget.

    Connect (TCP + TLS) is capped so slow or blackholed hosts fail quickly;
    read/write keep the full ``total_sec`` budget for large bodies. Pool
    acquisition uses the same connect budget.
    """
    total = max(0.1, float(total_sec))
    connect = min(
        DEFAULT_CONNECT_TIMEOUT_CAP,
        max(DEFAULT_CONNECT_TIMEOUT_FLOOR, total * DEFAULT_CONNECT_TIMEOUT_FRACTION),
    )
    # Never let connect exceed the overall budget.
    connect = min(connect, total)
    return httpx.Timeout(
        connect=connect,
        read=total,
        write=total,
        pool=connect,
    )


def _status_class(code: int) -> str:
    """Map an HTTP status to a low-cardinality class label (``2xx`` … ``5xx``)."""
    if 100 <= code < 200:
        return "1xx"
    if 200 <= code < 300:
        return "2xx"
    if 300 <= code < 400:
        return "3xx"
    if 400 <= code < 500:
        return "4xx"
    if 500 <= code < 600:
        return "5xx"
    return "other"


def _record_fetch_attempt(
    *,
    elapsed_sec: float,
    outcome: str,
    status_code: int | None = None,
    attempt: int = 0,
) -> None:
    """Record one HTTP attempt: latency hist + attempt/retry/status counters.

    Labels stay low-cardinality (outcome + status class only) so Prometheus
    series do not explode per URL/domain.
    """
    m = get_metrics()
    labels = {"outcome": outcome}
    if status_code is not None:
        labels = {**labels, "status_class": _status_class(status_code)}
    m.observe("http.fetch_seconds", max(0.0, float(elapsed_sec)), labels=labels)
    m.inc("http.fetch_attempts", labels={"outcome": outcome})
    if attempt > 0:
        m.inc("http.fetch_retries", labels={"outcome": outcome})
    if status_code is not None:
        m.inc("http.fetch_status", labels={"status_class": _status_class(status_code)})


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

    ``timeout`` defaults to ``settings.request_timeout_sec``. The client uses a
    split :class:`httpx.Timeout` (fast connect, full budget for read/write) so
    hung handshakes fail before burning the whole request window.
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
            timeout=build_http_timeout(timeout_f),
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

    Observability: every attempt records ``http.fetch_seconds`` (histogram),
    ``http.fetch_attempts`` / ``http.fetch_retries`` counters, and
    ``http.fetch_status`` by status class (``2xx``…``5xx``).
    """
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        t0 = time.perf_counter()
        try:
            async with acquire_fetch_slot():
                resp = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            elapsed = time.perf_counter() - t0
            last_exc = exc
            _record_fetch_attempt(
                elapsed_sec=elapsed, outcome="transport_error", attempt=attempt
            )
            if attempt + 1 >= max_attempts:
                break
            await asyncio.sleep(_backoff_delay(attempt, base_delay, None))
            continue
        elapsed = time.perf_counter() - t0
        if resp.status_code in RETRYABLE_STATUS:
            _record_fetch_attempt(
                elapsed_sec=elapsed,
                outcome="retryable",
                status_code=resp.status_code,
                attempt=attempt,
            )
            if attempt + 1 >= max_attempts:
                raise RetryableHTTPError(f"{url} -> {resp.status_code} after {max_attempts} attempts")
            await asyncio.sleep(_backoff_delay(attempt, base_delay, _retry_after_seconds(resp)))
            continue
        # success OR non-retryable (e.g. 404) — caller decides
        outcome = "ok" if resp.status_code < 400 else "http_error"
        _record_fetch_attempt(
            elapsed_sec=elapsed,
            outcome=outcome,
            status_code=resp.status_code,
            attempt=attempt,
        )
        return resp
    raise RetryableHTTPError(f"{url} failed transiently after {max_attempts} attempts: {last_exc}")


def normalize_charset_label(label: str | None) -> str | None:
    """Map a charset label to a Python codec name, or ``None`` if empty/unknown."""
    if not label:
        return None
    raw = label.strip().strip("\"'").lower().replace("_", "-")
    if not raw:
        return None
    # Normalize separators for alias table (utf_8 / utf-8 / utf8).
    compact = raw.replace("-", "").replace("_", "")
    for key, codec in _CHARSET_ALIASES.items():
        if key.replace("-", "").replace("_", "") == compact:
            return codec
    # Accept labels Python codecs already know.
    try:
        import codecs  # noqa: PLC0415

        codecs.lookup(raw)
        return raw
    except LookupError:
        # Try underscore form (e.g. shift_jis).
        underscored = raw.replace("-", "_")
        try:
            import codecs  # noqa: PLC0415

            codecs.lookup(underscored)
            return underscored
        except LookupError:
            return None


def charset_from_content_type(content_type: str | None) -> str | None:
    """Extract charset from a ``Content-Type`` header value."""
    if not content_type:
        return None
    m = _CHARSET_PARAM_RE.search(content_type)
    if not m:
        return None
    return normalize_charset_label(m.group(1))


def charset_from_html_meta(body: bytes, *, peek: int = 8192) -> str | None:
    """Extract charset from HTML ``<meta charset>`` / http-equiv in the document head.

    Only the first ``peek`` bytes are scanned (default 8 KiB) so large bodies
    do not pay a full scan. Returns a normalized codec name or ``None``.
    """
    if not body:
        return None
    head = body[: max(256, int(peek))]
    for pattern in (_META_CHARSET_RE, _META_HTTP_EQUIV_RE, _META_HTTP_EQUIV_RE_ALT):
        m = pattern.search(head)
        if m:
            try:
                label = m.group(1).decode("ascii", errors="ignore")
            except Exception:  # noqa: BLE001 — defensive
                label = ""
            codec = normalize_charset_label(label)
            if codec:
                return codec
    return None


def _try_decode(body: bytes, codec: str) -> str | None:
    """Decode *body* with *codec*; return ``None`` on hard failure.

    UTF-8 / ASCII use strict errors so we can fall through to other encodings
    when the label is wrong. Other codecs use ``replace`` only after a strict
    attempt fails? No — for declared charsets we prefer replace so pages still
    yield text. Callers pass only labels they trust (header/meta).
    """
    try:
        # Strict first: reject clearly-wrong labels for multi-byte families.
        return body.decode(codec, errors="strict")
    except UnicodeDecodeError:
        try:
            return body.decode(codec, errors="replace")
        except (LookupError, ValueError):
            return None
    except (LookupError, ValueError):
        return None


def decode_http_text(
    body: bytes,
    *,
    content_type: str | None = None,
    peek_html_meta: bool = True,
    use_detector: bool = True,
) -> tuple[str, str]:
    """Decode an HTTP response body to text with best-effort charset detection.

    Priority:
      1. ``Content-Type`` charset parameter
      2. HTML ``<meta charset>`` / http-equiv (when ``peek_html_meta``)
      3. Strict UTF-8 (BOM stripped) when the body is valid UTF-8
      4. ``charset_normalizer`` detection (when ``use_detector`` and installed)
      5. UTF-8 with ``errors=replace``

    Returns ``(text, encoding_label)`` where *encoding_label* is the codec
    actually used (or ``utf-8-replace`` for the final fallback). Empty bodies
    return ``("", "utf-8")``.
    """
    if not body:
        return "", "utf-8"

    # Strip UTF-8 BOM early so meta/header paths see clean bytes.
    if body.startswith(b"\xef\xbb\xbf"):
        body = body[3:]
        # BOM is a strong UTF-8 signal — try it first.
        try:
            return body.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            pass

    candidates: list[str] = []
    for label in (
        charset_from_content_type(content_type),
        charset_from_html_meta(body) if peek_html_meta else None,
    ):
        if label and label not in candidates:
            candidates.append(label)

    for codec in candidates:
        text = _try_decode(body, codec)
        if text is not None:
            return text, codec

    # Prefer clean UTF-8 when the body is well-formed (common for modern sites
    # that omit charset entirely).
    try:
        return body.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass

    if use_detector:
        try:
            from charset_normalizer import from_bytes  # noqa: PLC0415

            best = from_bytes(body).best()
            if best is not None:
                enc = normalize_charset_label(getattr(best, "encoding", None)) or "utf-8"
                text = str(best)
                return text, enc
        except Exception as exc:  # noqa: BLE001 — detector is best-effort
            logger.debug("charset_detect_failed", err=str(exc))

    return body.decode("utf-8", errors="replace"), "utf-8-replace"
