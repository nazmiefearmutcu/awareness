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

BUNDLED_CRAWL_IDS: list[str] = [
    "CC-MAIN-2026-21", "CC-MAIN-2026-17", "CC-MAIN-2026-12", "CC-MAIN-2026-08", "CC-MAIN-2026-04",
    "CC-MAIN-2025-51", "CC-MAIN-2025-47", "CC-MAIN-2025-43", "CC-MAIN-2025-38", "CC-MAIN-2025-33",
    "CC-MAIN-2025-30", "CC-MAIN-2025-26", "CC-MAIN-2025-21", "CC-MAIN-2025-18", "CC-MAIN-2025-13",
    "CC-MAIN-2025-08", "CC-MAIN-2025-05",
    "CC-MAIN-2024-51", "CC-MAIN-2024-46", "CC-MAIN-2024-42", "CC-MAIN-2024-38", "CC-MAIN-2024-33",
    "CC-MAIN-2024-30", "CC-MAIN-2024-26", "CC-MAIN-2024-22", "CC-MAIN-2024-18", "CC-MAIN-2024-10",
]

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
