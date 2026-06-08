# Awareness Cycle 1 — Plan 2: Make Scraping Actually Work (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A backfill targets REAL Common Crawl crawls and fetches real text, with robust HTTP (retries/backoff), correct domain filtering, sane shard breadth, and loud (not silent) failures — fixing the "the app has no idea how to scrape the internet" root cause.

**Architecture:** Replace the fabricated odd-ISO-week crawl-id heuristic with a real-catalog resolver (`collinfo.json` + cache + bundled fallback). Introduce one shared, retrying `httpx` layer (`util/http.py`) the adapters use. Fix the eTLD+1 domain filter and the 1-shard default. Make FineWeb fail loudly when explicitly requested without its dependency.

**Tech Stack:** Python 3.13, httpx (sync + async), warcio, pytest.

**Standard test command:** `PYTHONPATH=src .venv/bin/python -m pytest -q -p no:cacheprovider`
**Baseline at plan start:** 202 passing (`-m "not slow and not smoke"`) after Plan 1.

**Scope source:** spec workstreams **A** + **B** (data-flow-critical subset). Audit: `docs/superpowers/audit/2026-06-08-awareness-audit.json`.
**Deferred to a later Plan 2b (noted, not silently dropped):** robots crawl-delay honoring + UA consistency; per-domain rate-limiter delay race; seed discovery from sitemaps/robots; GDELT slot/time-math verification; job-wide fan-out budget. These do not block first-data-flow but must precede large-scale breadth increases.

**Co-landing constraint:** Task 4 (raise shard breadth) must not ship without keeping the existing per-domain politeness in place; since the limiter-race fix is deferred to Plan 2b, Task 4 keeps the default modest (4) — NOT unbounded — to avoid hammering Common Crawl before the limiter fix lands.

---

### Task 1: Shared HTTP layer with retries, backoff, and Retry-After

**Why:** Every adapter spins its own `httpx.AsyncClient` with no retries; one transient error yields zero docs silently. Centralize fetching with bounded retries + exponential backoff + `Retry-After` handling.

**Files:**
- Create: `src/awareness/util/http.py`
- Test: `tests/unit/test_util_http.py`

- [ ] **Step 1: Write the failing test** — create `tests/unit/test_util_http.py`:

```python
from __future__ import annotations

import httpx
import pytest

from awareness.util.http import RetryableHTTPError, get_with_retries


def _client_with_handler(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_retries_then_succeeds_on_500() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500)
        return httpx.Response(200, content=b"ok")

    async with _client_with_handler(handler) as client:
        resp = await get_with_retries(
            client, "https://example.test/x", max_attempts=5, base_delay=0.0
        )
    assert resp.status_code == 200
    assert resp.content == b"ok"
    assert calls["n"] == 3


async def test_raises_after_exhausting_retries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with _client_with_handler(handler) as client:
        with pytest.raises(RetryableHTTPError):
            await get_with_retries(
                client, "https://example.test/x", max_attempts=3, base_delay=0.0
            )


async def test_404_is_not_retried_and_returns_response() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    async with _client_with_handler(handler) as client:
        resp = await get_with_retries(
            client, "https://example.test/x", max_attempts=5, base_delay=0.0
        )
    assert resp.status_code == 404
    assert calls["n"] == 1  # genuine 404 → no retry
```

- [ ] **Step 2: Run, confirm FAIL** (`ModuleNotFoundError: awareness.util.http`):
`PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_util_http.py -q`

- [ ] **Step 3: Implement** — create `src/awareness/util/http.py`:

```python
"""Shared async HTTP helpers: retries, exponential backoff, Retry-After.

Adapters should fetch through these helpers instead of bare ``client.get`` so
that transient failures (timeouts, connection resets, 429/5xx) are retried with
backoff and a genuine 404 is surfaced — not silently swallowed.
"""

from __future__ import annotations

import asyncio

import httpx

from awareness.obs.logging import get_logger

logger = get_logger("util.http")

# Status codes worth retrying (transient/overload). A 404/410 is permanent.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BASE_DELAY = 0.5
DEFAULT_MAX_DELAY = 30.0


class RetryableHTTPError(Exception):
    """Raised when a request still failed transiently after all attempts.

    Callers should let this propagate so the task layer retries with its own
    backoff lease (see storage.state.fail_task), rather than swallowing it.
    """


def _backoff_delay(attempt: int, base_delay: float, retry_after: float | None) -> float:
    if retry_after is not None:
        return min(retry_after, DEFAULT_MAX_DELAY)
    return min(base_delay * (2 ** attempt), DEFAULT_MAX_DELAY)


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
    """
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
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
```

- [ ] **Step 4: Run, confirm PASS:** `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_util_http.py -q`
- [ ] **Step 5: Full-suite gate:** `PYTHONPATH=src .venv/bin/python -m pytest -q -m "not slow and not smoke"`
- [ ] **Step 6: Commit:**
```bash
git add src/awareness/util/http.py tests/unit/test_util_http.py
git commit -m "feat(http): shared async GET with retries, backoff, and Retry-After"
```

---

### Task 2: Resolve REAL Common Crawl crawl IDs (collinfo.json + cache + bundled fallback)

**Why:** `crawl_ids_for_range` fabricates non-existent odd-ISO-week IDs, so most WET/CC-Index/FineWeb fetches 404 and emit nothing. Resolve real crawls from the authoritative catalog, cached, with a bundled fallback for offline use. All three adapters call `crawl_ids_for_range`, so fixing it in place fixes all three. (Audit: `bug:cc-crawl-id-odd-week-heuristic-wrong`, `imp:fix-commoncrawl-crawl-id-mapping`.)

**Files:**
- Create: `src/awareness/sources/cc_crawls.py`
- Modify: `src/awareness/sources/commoncrawl_wet.py` (`crawl_ids_for_range` delegates; keep name/signature)
- Modify: `tests/unit/test_planner.py` (strengthen the crawl-id assertion)
- Test: `tests/unit/test_cc_crawls.py` (create)

- [ ] **Step 1: Write the failing test** — create `tests/unit/test_cc_crawls.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime

import awareness.sources.cc_crawls as cc


# A small real-shaped catalog: (id, from, to).
_CATALOG = [
    ("CC-MAIN-2026-21", datetime(2026, 5, 11, tzinfo=UTC), datetime(2026, 5, 25, tzinfo=UTC)),
    ("CC-MAIN-2026-17", datetime(2026, 4, 13, tzinfo=UTC), datetime(2026, 4, 27, tzinfo=UTC)),
    ("CC-MAIN-2026-12", datetime(2026, 3, 16, tzinfo=UTC), datetime(2026, 3, 30, tzinfo=UTC)),
]


def test_resolves_only_overlapping_crawls(monkeypatch) -> None:
    monkeypatch.setattr(cc, "_load_catalog", lambda: _CATALOG)
    ids = cc.resolve_crawl_ids(
        datetime(2026, 4, 1, tzinfo=UTC), datetime(2026, 5, 1, tzinfo=UTC)
    )
    # Only the April crawl (2026-17) overlaps [Apr 1, May 1].
    assert ids == ["CC-MAIN-2026-17"]


def test_bundled_fallback_returns_only_real_ids(monkeypatch) -> None:
    # Force the network/cache path to fail so the bundled fallback is used.
    monkeypatch.setattr(cc, "_fetch_catalog", lambda: None)
    monkeypatch.setattr(cc, "_read_cache", lambda: None)
    ids = cc.resolve_crawl_ids(
        datetime(2024, 1, 1, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC)
    )
    assert ids, "fallback must still yield crawl ids for a wide range"
    assert set(ids).issubset(set(cc.BUNDLED_CRAWL_IDS))
    # Newest first.
    assert ids == sorted(ids, reverse=True)
```

- [ ] **Step 2: Run, confirm FAIL** (`ModuleNotFoundError: awareness.sources.cc_crawls`):
`PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_cc_crawls.py -q`

- [ ] **Step 3: Implement** — create `src/awareness/sources/cc_crawls.py`:

```python
"""Resolve real Common Crawl crawl IDs for a date range.

The authoritative list lives at ``https://index.commoncrawl.org/collinfo.json``
as ``[{"id": "CC-MAIN-2026-21", "from": "2026-05-11T...", "to": "..."}]``. We
fetch it once, cache it on disk with a TTL, and fall back to a bundled snapshot
of known crawl IDs (windows computed from the ISO week) when offline — so a
fresh user always targets crawls that actually exist instead of fabricated ones.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx

from awareness.config import get_settings
from awareness.obs.logging import get_logger

logger = get_logger("sources.cc_crawls")

COLLINFO_URL = "https://index.commoncrawl.org/collinfo.json"
_CACHE_TTL = timedelta(days=7)
_CATALOG_TIMEOUT = 15.0

# Bundled fallback: known real crawl IDs (newest first). Windows are computed
# from the ISO week when no dated catalog is available. Keep reasonably current.
BUNDLED_CRAWL_IDS: list[str] = [
    "CC-MAIN-2026-21", "CC-MAIN-2026-17", "CC-MAIN-2026-12", "CC-MAIN-2026-08", "CC-MAIN-2026-04",
    "CC-MAIN-2025-51", "CC-MAIN-2025-47", "CC-MAIN-2025-43", "CC-MAIN-2025-38", "CC-MAIN-2025-33",
    "CC-MAIN-2025-30", "CC-MAIN-2025-26", "CC-MAIN-2025-21", "CC-MAIN-2025-18", "CC-MAIN-2025-13",
    "CC-MAIN-2025-08", "CC-MAIN-2025-05",
    "CC-MAIN-2024-51", "CC-MAIN-2024-46", "CC-MAIN-2024-42", "CC-MAIN-2024-38", "CC-MAIN-2024-33",
    "CC-MAIN-2024-30", "CC-MAIN-2024-26", "CC-MAIN-2024-22", "CC-MAIN-2024-18", "CC-MAIN-2024-10",
]

# (id, from, to) triples.
Crawl = tuple[str, datetime, datetime]


def _iso_week_window(crawl_id: str) -> tuple[datetime, datetime] | None:
    """Approximate [from, to] for ``CC-MAIN-YYYY-WW`` from its ISO week."""
    try:
        _, _, ymd = crawl_id.partition("CC-MAIN-")
        year_s, week_s = ymd.split("-")
        year, week = int(year_s), int(week_s)
        monday = datetime.fromisocalendar(year, week, 1).replace(tzinfo=UTC)
    except (ValueError, IndexError):
        return None
    return monday, monday + timedelta(days=21)


def _bundled_catalog() -> list[Crawl]:
    out: list[Crawl] = []
    for cid in BUNDLED_CRAWL_IDS:
        window = _iso_week_window(cid)
        if window:
            out.append((cid, window[0], window[1]))
    return out


def _cache_path():
    settings = get_settings()
    assert settings.data_dir is not None
    return settings.data_dir / "cache" / "cc_collinfo.json"


def _parse_dt(raw: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _catalog_from_payload(payload: list[dict]) -> list[Crawl]:
    out: list[Crawl] = []
    for entry in payload:
        cid = entry.get("id")
        cf = _parse_dt(entry.get("from", "") or "")
        ct = _parse_dt(entry.get("to", "") or "")
        if cid and cf and ct:
            out.append((cid, cf, ct))
    return out


def _fetch_catalog() -> list[Crawl] | None:
    try:
        resp = httpx.get(COLLINFO_URL, timeout=_CATALOG_TIMEOUT, follow_redirects=True)
        if resp.status_code != 200:
            return None
        return _catalog_from_payload(resp.json())
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("cc_collinfo_fetch_failed", err=str(exc))
        return None


def _read_cache() -> list[Crawl] | None:
    path = _cache_path()
    if not path.exists():
        return None
    try:
        blob = json.loads(path.read_text())
        fetched = _parse_dt(blob.get("fetched_at", ""))
        if fetched is None or datetime.now(UTC) - fetched > _CACHE_TTL:
            return None
        return _catalog_from_payload(blob.get("crawls", []))
    except (OSError, ValueError):
        return None


def _write_cache(catalog: list[Crawl]) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "fetched_at": datetime.now(UTC).isoformat(),
                    "crawls": [
                        {"id": c, "from": f.isoformat(), "to": t.isoformat()} for c, f, t in catalog
                    ],
                }
            )
        )
    except OSError:
        pass


def _load_catalog() -> list[Crawl]:
    cached = _read_cache()
    if cached:
        return cached
    fetched = _fetch_catalog()
    if fetched:
        _write_cache(fetched)
        return fetched
    logger.info("cc_collinfo_using_bundled_fallback")
    return _bundled_catalog()


def resolve_crawl_ids(start: datetime, end: datetime) -> list[str]:
    """Real crawl IDs whose capture window overlaps ``[start, end]``, newest first."""
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    if start > end:
        start, end = end, start
    catalog = _load_catalog()
    overlapping = [cid for cid, cf, ct in catalog if cf <= end and ct >= start]
    return sorted(set(overlapping), reverse=True)
```

- [ ] **Step 4: Delegate from `commoncrawl_wet.crawl_ids_for_range`.** In `src/awareness/sources/commoncrawl_wet.py`, replace the body of `crawl_ids_for_range` (lines 80-94) with a delegation (keep the name + signature so `fineweb.py` and `cc_index.py` keep working):

```python
def crawl_ids_for_range(start: datetime, end: datetime) -> list[str]:
    """Convert a date range to REAL crawl_ids (e.g. ``CC-MAIN-2024-26``).

    Delegates to :func:`awareness.sources.cc_crawls.resolve_crawl_ids`, which
    uses the authoritative collinfo.json catalog (cached, with a bundled
    fallback) instead of the old odd-ISO-week heuristic that produced
    non-existent crawl IDs.
    """
    from awareness.sources.cc_crawls import resolve_crawl_ids

    return resolve_crawl_ids(start, end)
```

The now-unused `_iso_year_weeks` helper may be left in place (harmless) or removed if no longer referenced — check with `grep -n _iso_year_weeks src/awareness/sources/commoncrawl_wet.py`; remove only if there are no other references.

- [ ] **Step 5: Strengthen the weak planner test.** In `tests/unit/test_planner.py`, the existing `test_crawl_ids_for_range_covers_a_year` asserts only count + prefix. Replace its body so it asserts the returned IDs are REAL (subset of the bundled known set), forcing the bundled path to avoid network:

```python
def test_crawl_ids_for_range_covers_a_year(monkeypatch) -> None:
    import awareness.sources.cc_crawls as cc
    from datetime import UTC, datetime

    # Avoid network: force the bundled fallback.
    monkeypatch.setattr(cc, "_fetch_catalog", lambda: None)
    monkeypatch.setattr(cc, "_read_cache", lambda: None)
    crawls = crawl_ids_for_range(
        datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 12, 31, tzinfo=UTC)
    )
    assert crawls, "a one-year range must resolve at least one real crawl"
    assert set(crawls).issubset(set(cc.BUNDLED_CRAWL_IDS))
    assert all(c.startswith("CC-MAIN-2024-") for c in crawls)
```

(If the existing test imports differ, keep its imports; only the assertions and the monkeypatch matter. Read the current `tests/unit/test_planner.py` first and adapt the function in place.)

- [ ] **Step 6: Run the new + changed tests:** `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_cc_crawls.py tests/unit/test_planner.py -q`
- [ ] **Step 7: Full-suite gate:** `PYTHONPATH=src .venv/bin/python -m pytest -q -m "not slow and not smoke"`
- [ ] **Step 8: Commit:**
```bash
git add src/awareness/sources/cc_crawls.py src/awareness/sources/commoncrawl_wet.py tests/unit/test_cc_crawls.py tests/unit/test_planner.py
git commit -m "fix(sources): resolve real Common Crawl IDs from collinfo.json (+bundled fallback)"
```

---

### Task 3: Fix the WET domain filter (eTLD+1 vs subdomain mismatch)

**Why:** `_parse_wet_to_captures` keeps a record only if `domain_of(canonical_url(url)) in domains_filter`, but `domain_of` returns the registered eTLD+1. If the user passes a subdomain (`news.bbc.co.uk`, `www.cnn.com`), nothing matches and a domain-narrowed backfill emits zero docs. Normalize both sides through `domain_of`. (Audit: `bug:wet-domain-filter-etld1-drops-subdomain-requests`.)

**Files:**
- Modify: `src/awareness/sources/commoncrawl_wet.py` (`_run_shard` builds `domains_filter` ~line 204; `_parse_wet_to_captures` compares ~line 303)
- Test: `tests/unit/test_cc_wet_domain_filter.py` (create)

- [ ] **Step 1: Write the failing test** — create `tests/unit/test_cc_wet_domain_filter.py`:

```python
from __future__ import annotations

from awareness.sources.commoncrawl_wet import _normalize_domain_filter, _record_passes_domain_filter


def test_subdomain_request_matches_etld1_records() -> None:
    # User asked for a subdomain; records are matched on registered domain.
    flt = _normalize_domain_filter(["news.bbc.co.uk", "www.cnn.com"])
    assert flt == {"bbc.co.uk", "cnn.com"}
    assert _record_passes_domain_filter("http://news.bbc.co.uk/a", flt) is True
    assert _record_passes_domain_filter("http://www.cnn.com/x", flt) is True
    assert _record_passes_domain_filter("http://example.org/y", flt) is False


def test_none_filter_passes_everything() -> None:
    assert _record_passes_domain_filter("http://anything.test/z", None) is True
```

- [ ] **Step 2: Run, confirm FAIL** (helpers don't exist):
`PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_cc_wet_domain_filter.py -q`

- [ ] **Step 3: Implement.** In `src/awareness/sources/commoncrawl_wet.py`, add two module-level helpers (e.g. right after `crawl_ids_for_range`):

```python
def _normalize_domain_filter(domains: list[str] | None) -> set[str] | None:
    """Reduce requested domains to their registered eTLD+1 so a subdomain
    request (news.bbc.co.uk) matches records whose domain_of is bbc.co.uk."""
    if not domains:
        return None
    normalized = {domain_of(d) or domain_of(f"http://{d}") or d.lower() for d in domains}
    return {d for d in normalized if d} or None


def _record_passes_domain_filter(url: str, domains_filter: set[str] | None) -> bool:
    if not domains_filter:
        return True
    cu = canonical_url(url)
    dom = domain_of(cu) if cu else None
    return dom in domains_filter
```

Then in `_run_shard`, change the filter construction (line ~204) from:
```python
        domains_filter = set(partition.payload.get("domains") or []) or None
```
to:
```python
        domains_filter = _normalize_domain_filter(partition.payload.get("domains"))
```

And in `_parse_wet_to_captures`, replace the inline domain check (the `cu = canonical_url(url)`, `dom = ...`, `if domains_filter and dom not in domains_filter: continue` block, lines ~301-304) with the shared helper while still computing `cu`/`dom` for later use:
```python
            cu = canonical_url(url)
            dom = domain_of(cu) if cu else None
            if domains_filter and dom not in domains_filter:
                continue
```
(Keep this as-is for `cu`/`dom` reuse — the fix is that `domains_filter` now holds eTLD+1 values, so the existing `dom in domains_filter` comparison is correct. The helper functions exist for the test and future reuse.)

- [ ] **Step 4: Run, confirm PASS:** `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_cc_wet_domain_filter.py -q`
- [ ] **Step 5: Full-suite gate.**
- [ ] **Step 6: Commit:**
```bash
git add src/awareness/sources/commoncrawl_wet.py tests/unit/test_cc_wet_domain_filter.py
git commit -m "fix(cc-wet): normalize domain filter to eTLD+1 so subdomain requests match"
```

---

### Task 4: Configurable WET shards-per-crawl (default > 1)

**Why:** The adapter defaults to 1 WET shard per crawl (registry uses bare `cls()`), so even a correct crawl ingests a trivial slice. Make it configurable via settings with a modest default (4). (Audit: `imp:expose-cc-wet-shard-count`. Kept modest pending the Plan-2b limiter fix.)

**Files:**
- Modify: `src/awareness/config/schema.py` (add a setting) and/or `src/awareness/config/settings.py`
- Modify: `src/awareness/sources/base.py` (`_register_defaults`, line ~124) to pass the setting
- Modify: `src/awareness/sources/commoncrawl_wet.py` (default arg)
- Test: `tests/unit/test_cc_wet_shards.py` (create)

- [ ] **Step 1: Read the config first.** Read `src/awareness/config/schema.py` and `src/awareness/config/settings.py` to find the settings class and the existing `text_min_chars`-style field pattern and the `AW_`-prefixed env convention. Add a field `cc_wet_max_shards_per_crawl: int = 4` following that exact pattern (validation: ge=1). Note the exact attribute name you used for Step 3.

- [ ] **Step 2: Write the failing test** — create `tests/unit/test_cc_wet_shards.py`:

```python
from __future__ import annotations

from awareness.sources.commoncrawl_wet import CommonCrawlWetAdapter
from awareness.schemas.jobs import BackfillRequest
from awareness.schemas.doc import SourceKind
from datetime import UTC, datetime


def test_plan_propagates_max_shards(monkeypatch) -> None:
    import awareness.sources.cc_crawls as cc
    monkeypatch.setattr(cc, "_fetch_catalog", lambda: None)
    monkeypatch.setattr(cc, "_read_cache", lambda: None)
    adapter = CommonCrawlWetAdapter(max_shards_per_crawl=4)
    req = BackfillRequest(
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 6, 1, tzinfo=UTC),
        sources=[SourceKind.COMMON_CRAWL_WET],
    )
    parts = adapter.plan(req)
    assert parts, "must produce discovery partitions for a real-crawl range"
    assert all(p.payload["max_shards"] == 4 for p in parts)
```

- [ ] **Step 2b: Run, confirm FAIL or adapt** — it may already pass for the explicit `max_shards_per_crawl=4` ctor arg; the real change is the DEFAULT and the registry wiring. Proceed.

- [ ] **Step 3: Wire the setting.** In `src/awareness/sources/base.py` `_register_defaults` (line ~124, `reg.register(cls())`), special-case the WET adapter to pass the configured shard count, e.g.:
```python
            try:
                if cls is CommonCrawlWetAdapter:
                    reg.register(cls(max_shards_per_crawl=get_settings().cc_wet_max_shards_per_crawl))
                else:
                    reg.register(cls())
```
(Read `base.py` to see how `cls` iterates and where `CommonCrawlWetAdapter` / `get_settings` are imported; add the import if needed. Use the EXACT settings attribute name from Step 1.)

Also change the `CommonCrawlWetAdapter.__init__` default from `max_shards_per_crawl: int = 1` to `= 4` for callers that construct it directly.

- [ ] **Step 4: Run the new test + full-suite gate.**
- [ ] **Step 5: Commit:**
```bash
git add src/awareness/config/ src/awareness/sources/base.py src/awareness/sources/commoncrawl_wet.py tests/unit/test_cc_wet_shards.py
git commit -m "feat(cc-wet): configurable shards-per-crawl (default 4) wired from settings"
```

---

### Task 5: FineWeb fails loudly when explicitly requested without `datasets`

**Why:** When `datasets` isn't installed, FineWeb's `plan()` returns `[]` with a low-level `logger.info`, so a user who explicitly selected FineWeb gets a silent no-op and no data. Make it loud: when FineWeb is explicitly in the request's sources but the dependency is missing, raise a clear error (or emit a prominent warning the planner surfaces). (Audit: `imp:install-fineweb-deps-or-warn-loudly`.)

**Files:**
- Modify: `src/awareness/sources/fineweb.py` (`plan`, lines ~56-61)
- Test: `tests/unit/test_fineweb_loud.py` (create)

- [ ] **Step 1: Read `src/awareness/sources/fineweb.py` lines 40-90** to see `plan`'s signature and how it learns which sources were requested (it receives a `BackfillRequest` with `.sources`). Confirm whether `plan` can see that FineWeb was explicitly requested.

- [ ] **Step 2: Write the failing test** — create `tests/unit/test_fineweb_loud.py`:

```python
from __future__ import annotations

import builtins
from datetime import UTC, datetime

import pytest

from awareness.schemas.doc import SourceKind
from awareness.schemas.jobs import BackfillRequest
from awareness.sources.fineweb import FineWebAdapter, FineWebDependencyMissing


def test_explicit_fineweb_without_datasets_raises(monkeypatch) -> None:
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "datasets":
            raise ImportError("no datasets")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    adapter = FineWebAdapter()
    req = BackfillRequest(
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 2, 1, tzinfo=UTC),
        sources=[SourceKind.FINEWEB],  # explicitly requested
    )
    with pytest.raises(FineWebDependencyMissing):
        adapter.plan(req)
```

- [ ] **Step 3: Implement.** In `src/awareness/sources/fineweb.py`, add an exception class near the top:
```python
class FineWebDependencyMissing(RuntimeError):
    """FineWeb was explicitly requested but the optional `datasets` dep is missing.

    Install it with `pip install 'awareness[hf]'` (adds datasets + huggingface-hub).
    """
```
Then change the `plan` dependency check so that when FineWeb (or FineWeb-2) is explicitly present in `request.sources`, a missing `datasets` RAISES `FineWebDependencyMissing` with an actionable message; only when FineWeb was NOT explicitly requested (e.g. a default sweep) does it fall back to the quiet skip. Concretely replace:
```python
        try:
            import datasets  # noqa: F401, PLC0415
        except ImportError:
            logger.info("fineweb_skipped_missing_datasets_lib")
            return []
```
with:
```python
        explicitly_requested = bool(
            {SourceKind.FINEWEB, SourceKind.FINEWEB_2} & set(request.sources or [])
        )
        try:
            import datasets  # noqa: F401, PLC0415
        except ImportError as exc:
            if explicitly_requested:
                raise FineWebDependencyMissing(
                    "FineWeb was requested but the 'datasets' package is not installed. "
                    "Install it with: pip install 'awareness[hf]'"
                ) from exc
            logger.info("fineweb_skipped_missing_datasets_lib")
            return []
```
(Adjust the `request.sources` access to match the real field; verify in Step 1.)

- [ ] **Step 4: Run the new test + full-suite gate.**
- [ ] **Step 5: Commit:**
```bash
git add src/awareness/sources/fineweb.py tests/unit/test_fineweb_loud.py
git commit -m "fix(fineweb): fail loudly when explicitly requested without the datasets dep"
```

---

### Task 6: Distinguish transient HTTP failures from genuine 404 in WET discovery

**Why:** `_run_discovery` and `_run_shard` `return` (emit nothing) on ANY error, so a transient timeout looks identical to a genuine "crawl not published" 404 — the job reports success with zero docs. Route discovery fetches through `get_with_retries` (Task 1): genuine 404 → quiet skip; persistent transient → raise so the task layer retries with its backoff lease (Plan 1). (Audit: `bug:cc-and-discovery-network-failures-silently-swallowed`.)

**Files:**
- Modify: `src/awareness/sources/commoncrawl_wet.py` (`_run_discovery` lines ~158-171)
- Test: `tests/unit/test_cc_wet_discovery_errors.py` (create)

- [ ] **Step 1: Read `_run_discovery` (lines 142-195)** and note it builds an `httpx.AsyncClient` inline. You will route the GET through `get_with_retries` so transient failures raise `RetryableHTTPError` (which propagates out of `run_partition` → the worker's `_run_task` except-block marks the task for retry with backoff), while a 404 still returns cleanly with no enqueue.

- [ ] **Step 2: Write the failing test** — create `tests/unit/test_cc_wet_discovery_errors.py`:

```python
from __future__ import annotations

import httpx
import pytest

from awareness.util.http import RetryableHTTPError, get_with_retries


# This task's behavioral contract is exercised at the get_with_retries level
# (a 404 returns; a persistent 503 raises). The discovery wiring reuses it.
async def test_discovery_helper_raises_on_persistent_503() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RetryableHTTPError):
            await get_with_retries(client, "https://data.commoncrawl.test/x", max_attempts=2, base_delay=0.0)


async def test_discovery_helper_returns_on_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resp = await get_with_retries(client, "https://data.commoncrawl.test/x", max_attempts=2, base_delay=0.0)
    assert resp.status_code == 404
```

- [ ] **Step 3: Implement.** In `_run_discovery`, replace the inline GET with the retrying helper. Change:
```python
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            try:
                resp = await client.get(url, headers={"User-Agent": context.user_agent})
            except httpx.HTTPError as exc:
                logger.warning("cc_wet_paths_fetch_failed", crawl_id=crawl_id, err=str(exc))
                return
            if resp.status_code != 200:
                logger.warning("cc_wet_paths_not_found", crawl_id=crawl_id, status=resp.status_code)
                return
```
to:
```python
        from awareness.util.http import get_with_retries

        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            # Transient failures raise (task retries with backoff); a genuine
            # 404 means this crawl has no wet.paths — skip quietly.
            resp = await get_with_retries(
                client, url, headers={"User-Agent": context.user_agent}
            )
            if resp.status_code != 200:
                logger.info("cc_wet_paths_not_found", crawl_id=crawl_id, status=resp.status_code)
                return
```
(`RetryableHTTPError` is intentionally NOT caught here, so it propagates to the worker's per-task except-block, which calls `fail_task` → backoff retry.)

- [ ] **Step 4: Run the new test + full-suite gate** (the existing cc_wet tests must still pass).
- [ ] **Step 5: Commit:**
```bash
git add src/awareness/sources/commoncrawl_wet.py tests/unit/test_cc_wet_discovery_errors.py
git commit -m "fix(cc-wet): retry transient discovery failures; only skip on genuine 404"
```

---

## Plan-level self-review checklist

- [ ] Full suite green after all tasks: `PYTHONPATH=src .venv/bin/python -m pytest -q -m "not slow and not smoke"`.
- [ ] `crawl_ids_for_range` now returns only real IDs (subset of `BUNDLED_CRAWL_IDS` in the offline test).
- [ ] `ruff check` introduces no NEW errors in the touched files.
- [ ] Deferred items recorded for Plan 2b: limiter race, robots crawl-delay/UA, seed discovery, GDELT slot-math verification, fan-out budget.

## Spec coverage map (workstreams A + B core)

| Item | Task |
|---|---|
| Real crawl-ID resolver (collinfo.json + cache + fallback) | 2 |
| Shared HTTP client + retries/backoff/Retry-After | 1 |
| Domain filter eTLD+1 normalization | 3 |
| Shards-per-crawl configurable (default > 1) | 4 |
| FineWeb fail-loud | 5 |
| Transient-vs-404 in discovery | 1 + 6 |
| robots crawl-delay/UA, limiter race, seed discovery, GDELT math, fan-out budget | Deferred → Plan 2b |
