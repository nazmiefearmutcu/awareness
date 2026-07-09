"""LinkedIn public guest job surface.

Reads only the unauthenticated guest HTML endpoints LinkedIn serves to the
open web (same pages a logged-out browser can open). No cookies, no login
bypass, no CAPTCHA solving, no residential proxies.

If LinkedIn rate-limits or returns empty HTML, the caller gets a clean error
and other boards still run.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import UTC, datetime, timedelta
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from awareness.jobsearch.cache import DETAIL_TTL_SEC, SEARCH_TTL_SEC, JobSearchCache
from awareness.jobsearch.models import SOURCE_CATALOG, JobListing
from awareness.obs.logging import get_logger

logger = get_logger("jobsearch.linkedin")

# Polite pacing between LinkedIn HTTP calls (skip when serving from cache).
LI_REQUEST_DELAY_SEC = 0.35
_last_li_request_at = 0.0

DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
DEFAULT_ENRICH_TOP_K = 25


def _id_for(*parts: str) -> str:
    import hashlib

    raw = "|".join(p.strip().lower() for p in parts if p)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _is_remote(text: str, location: str = "") -> bool:
    blob = f"{text} {location}".lower()
    return any(
        k in blob
        for k in ("remote", "work from home", "wfh", "distributed", "anywhere", "worldwide", "fully remote")
    )


# Browser-like UA: guest HTML rejects many bare bot agents.
LI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
# Max pages × 10 results. Keep small — polite public-surface use.
MAX_PAGES = 3


def _strip_tags(html: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _first(patterns: list[str], blob: str, flags: int = re.I | re.S) -> str:
    for p in patterns:
        m = re.search(p, blob, flags)
        if m:
            return _strip_tags(m.group(1))
    return ""


def _parse_relative_age(text: str) -> datetime | None:
    """Parse strings like '2 days ago', '3 hours ago', 'Just now'."""
    t = (text or "").strip().lower()
    if not t:
        return None
    now = datetime.now(UTC)
    if "just now" in t or "moments" in t:
        return now
    m = re.search(r"(\d+)\s*(minute|hour|day|week|month)s?\s*ago", t)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    delta = {
        "minute": timedelta(minutes=n),
        "hour": timedelta(hours=n),
        "day": timedelta(days=n),
        "week": timedelta(weeks=n),
        "month": timedelta(days=30 * n),
    }.get(unit)
    return now - delta if delta else None


def build_search_queries(
    q: str = "",
    titles: list[str] | None = None,
    skills: list[str] | None = None,
    *,
    max_queries: int = 4,
) -> list[str]:
    """Build up to ``max_queries`` LinkedIn keyword strings from profile + user q.

    Order: user ``q``, each title (max 2), top skills joined (1 query).
    Deduplicated case-insensitively; falls back to a generic role if empty.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        s = (raw or "").strip()
        if not s:
            return
        key = re.sub(r"\s+", " ", s).lower()
        if key in seen:
            return
        seen.add(key)
        out.append(re.sub(r"\s+", " ", s))

    _add(q or "")
    for t in (titles or [])[:2]:
        _add(t)
        if len(out) >= max_queries:
            break
    if len(out) < max_queries and skills:
        joined = " ".join(s.strip() for s in skills[:6] if (s or "").strip())
        _add(joined)

    if not out:
        out = ["software engineer"]
    return out[:max_queries]


def build_search_locations(locations: list[str] | None) -> list[str]:
    """Up to 3 profile locations; if none, a single empty (worldwide) slot."""
    locs = [re.sub(r"\s+", " ", (x or "").strip()) for x in (locations or [])]
    locs = [x for x in locs if x]
    # Deduplicate case-insensitively, preserve order
    seen: set[str] = set()
    uniq: list[str] = []
    for loc in locs:
        k = loc.lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(loc)
    if not uniq:
        return [""]
    return uniq[:3]


def extract_job_id(url_or_text: str) -> str:
    """Extract numeric job posting id from a LinkedIn URL, urn, or free text."""
    s = (url_or_text or "").strip()
    if not s:
        return ""
    patterns = [
        r"urn:li:jobPosting:(\d+)",
        r"jobPosting:(\d+)",
        r"/jobs/view/[^/?#\s]*-(\d+)",
        r"/jobs/view/(\d+)",
        r"currentJobId=(\d+)",
        r"jobPosting/(\d+)",
        r"(?<!\d)(\d{8,12})(?!\d)",  # bare numeric id (LinkedIn ids are long)
    ]
    for p in patterns:
        m = re.search(p, s, re.I)
        if m:
            return m.group(1)
    return ""


def _parse_cards(html: str) -> list[JobListing]:
    if not html or len(html) < 200:
        return []
    # Split on job cards
    chunks = re.split(r'(?=<div[^>]+class="[^"]*base-card)', html)
    out: list[JobListing] = []
    for chunk in chunks:
        if "jobPosting" not in chunk and "jobs/view" not in chunk:
            continue
        job_id = _first(
            [
                r'data-entity-urn="urn:li:jobPosting:(\d+)"',
                r"/jobs/view/[^\"']+-(\d+)",
                r"jobPosting:(\d+)",
            ],
            chunk,
        )
        href = _first(
            [
                r'href="(https?://[^"]*linkedin\.com/jobs/view/[^"]+)"',
                r'href="(/jobs/view/[^"]+)"',
            ],
            chunk,
        )
        if href.startswith("/"):
            href = urljoin("https://www.linkedin.com", href)
        # Drop tracking query noise for stable ids
        href = href.split("?")[0] if href else ""
        title = _first(
            [
                r'class="[^"]*base-search-card__title[^"]*"[^>]*>(.*?)</h3>',
                r'class="[^"]*base-search-card__title[^"]*"[^>]*>(.*?)</[a-zA-Z0-9]+>',
            ],
            chunk,
        )
        company = _first(
            [
                r'class="[^"]*base-search-card__subtitle[^"]*"[^>]*>(.*?)</h4>',
                r'class="[^"]*base-search-card__subtitle[^"]*"[^>]*>(.*?)</[a-zA-Z0-9]+>',
            ],
            chunk,
        )
        location = _first(
            [
                r'class="[^"]*job-search-card__location[^"]*"[^>]*>(.*?)</span>',
                r'class="[^"]*base-search-card__metadata[^"]*"[^>]*>(.*?)</div>',
            ],
            chunk,
        )
        age_txt = _first(
            [
                r'class="[^"]*job-search-card__listdate[^"]*"[^>]*>(.*?)</time>',
                r'datetime="([^"]+)"',
                r'class="[^"]*job-search-card__listdate--new[^"]*"[^>]*>(.*?)</time>',
            ],
            chunk,
        )
        if not title or not href:
            continue
        published = None
        if re.match(r"\d{4}-\d{2}-\d{2}", age_txt or ""):
            try:
                published = datetime.fromisoformat(age_txt).replace(tzinfo=UTC)
            except ValueError:
                published = _parse_relative_age(age_txt)
        else:
            published = _parse_relative_age(age_txt)

        if not job_id:
            job_id = extract_job_id(href)

        remote = _is_remote(f"{title} {location}")
        out.append(
            JobListing(
                id=_id_for("linkedin", job_id or href, title),
                title=title[:200],
                company=company[:120],
                location=location[:160],
                remote=remote,
                url=href or f"https://www.linkedin.com/jobs/view/{job_id}",
                source="linkedin",
                source_label=SOURCE_CATALOG["linkedin"]["label"],
                published_at=published,
                tags=["linkedin"],
                description=f"{title} at {company} — {location}".strip(" —"),
            )
        )
    return out


def _criteria_value(html: str, header: str) -> str:
    """Pull a job-criteria list value by subheader label (seniority, employment, …)."""
    # Typical: <h3 ...>Seniority level</h3> ... <span ...>Mid-Senior level</span>
    esc = re.escape(header)
    pat = (
        r'class="[^"]*description__job-criteria-subheader[^"]*"[^>]*>\s*'
        + esc
        + r'\s*</[^>]+>.{0,400}?'
        r'class="[^"]*description__job-criteria-text[^"]*"[^>]*>(.*?)</(?:span|div|p|li|h\d)>'
    )
    m = re.search(pat, html, re.I | re.S)
    if m:
        return _strip_tags(m.group(1))
    # Looser fallback: header text then next non-empty text node-ish span
    pat2 = esc + r"\s*</[^>]+>\s*<[^>]+>(.*?)</(?:span|div|p|li|h\d)>"
    m2 = re.search(pat2, html, re.I | re.S)
    if m2:
        val = _strip_tags(m2.group(1))
        if val and val.lower() != header.lower():
            return val
    return ""


def parse_job_detail(html: str) -> dict[str, str]:
    """Parse guest job-detail HTML into structured fields (no network)."""
    if not html or len(html) < 100:
        return {}
    title = _first(
        [
            r'class="[^"]*top-card-layout__title[^"]*"[^>]*>(.*?)</h1>',
            r'class="[^"]*topcard__title[^"]*"[^>]*>(.*?)</h1>',
            r"<h1[^>]*>(.*?)</h1>",
        ],
        html,
    )
    company = _first(
        [
            r'class="[^"]*topcard__org-name-link[^"]*"[^>]*>(.*?)</a>',
            r'class="[^"]*top-card-layout__card[^"]*"[^>]*>.*?<a[^>]*>(.*?)</a>',
            r'class="[^"]*topcard__flavor[^"]*"[^>]*>(.*?)</a>',
            r'data-tracking-control-name="public_jobs_topcard-org-name"[^>]*>(.*?)</a>',
        ],
        html,
    )
    location = _first(
        [
            r'class="[^"]*topcard__flavor--bullet[^"]*"[^>]*>(.*?)</span>',
            r'class="[^"]*topcard__flavor[^"]*topcard__flavor--bullet[^"]*"[^>]*>(.*?)</span>',
        ],
        html,
    )
    description = _first(
        [
            r'class="[^"]*show-more-less-html__markup[^"]*"[^>]*>(.*?)</div>',
            r'class="[^"]*description__text[^"]*"[^>]*>(.*?)</div>',
            r'id="job-details"[^>]*>(.*?)</div>',
        ],
        html,
    )
    if not description:
        # Fallback: core description section
        m = re.search(
            r'class="[^"]*description__text[^"]*"(?:[^>]*)>(.{100,20000})</div>',
            html,
            re.I | re.S,
        )
        if m:
            description = _strip_tags(m.group(1))

    seniority = _criteria_value(html, "Seniority level")
    employment = _criteria_value(html, "Employment type")
    if not employment:
        employment = _criteria_value(html, "Employment Type")
    job_function = _criteria_value(html, "Job function")
    industries = _criteria_value(html, "Industries")

    return {
        "title": title[:200] if title else "",
        "company": company[:120] if company else "",
        "location": location[:160] if location else "",
        "description": description[:5000] if description else "",
        "seniority": seniority[:80] if seniority else "",
        "employment": employment[:80] if employment else "",
        "job_function": job_function[:120] if job_function else "",
        "industries": industries[:160] if industries else "",
    }


def apply_detail_to_listing(job: JobListing, detail: dict[str, str]) -> JobListing:
    """Merge detail fields into a listing; prefer longer/better strings."""
    if not detail:
        return job
    data = job.model_dump()
    new_title = (detail.get("title") or "").strip()
    if new_title and (len(new_title) > len(job.title or "") or not job.title):
        data["title"] = new_title[:200]
    new_company = (detail.get("company") or "").strip()
    if new_company and (len(new_company) > len(job.company or "") or not job.company):
        data["company"] = new_company[:120]
    new_loc = (detail.get("location") or "").strip()
    if new_loc and (len(new_loc) > len(job.location or "") or not job.location):
        data["location"] = new_loc[:160]
    desc = (detail.get("description") or "").strip()
    # Prefer real description over card stub ("Title at Company — Loc")
    stub = f"{job.title} at {job.company}".lower()
    current = (job.description or "").strip()
    if desc and (len(desc) > len(current) + 40 or current.lower().startswith(stub[:40].lower())):
        data["description"] = desc[:5000]
    tags = list(job.tags or [])
    for key, prefix in (
        ("seniority", "seniority"),
        ("employment", "employment"),
        ("job_function", "function"),
        ("industries", "industry"),
    ):
        val = (detail.get(key) or "").strip()
        if not val:
            continue
        tag = f"{prefix}:{val}"
        if tag not in tags and val not in tags:
            tags.append(tag)
    data["tags"] = tags
    data["remote"] = job.remote or _is_remote(data.get("description", ""), data.get("location", ""))
    return JobListing(**data)


async def _polite_wait() -> None:
    global _last_li_request_at
    now = time.monotonic()
    wait = LI_REQUEST_DELAY_SEC - (now - _last_li_request_at)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_li_request_at = time.monotonic()


async def _li_get_text(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    cache: JobSearchCache | None = None,
    cache_kind: str | None = None,
    cache_params: dict[str, Any] | None = None,
    cache_ttl: int = SEARCH_TTL_SEC,
) -> str:
    """GET LinkedIn guest HTML with cache, 0.35s pacing, and one 429 retry."""
    if cache is not None and cache_kind and cache_params is not None:
        hit = cache.get(cache_kind, cache_params)
        if isinstance(hit, str) and hit:
            return hit

    await _polite_wait()
    r = await client.get(url, params=params, headers=LI_HEADERS)
    if r.status_code == 429:
        await asyncio.sleep(2.0)
        await _polite_wait()
        r = await client.get(url, params=params, headers=LI_HEADERS)
    if r.status_code in (429, 999, 403):
        raise RuntimeError(f"linkedin rate-limited or blocked (HTTP {r.status_code})")
    r.raise_for_status()
    text = r.text
    if cache is not None and cache_kind and cache_params is not None:
        cache.set(cache_kind, cache_params, text, cache_ttl)
    return text


async def fetch_job_detail(
    client: httpx.AsyncClient,
    job_id: str,
    *,
    cache: JobSearchCache | None = None,
) -> dict[str, str]:
    """Fetch + parse one guest job posting by numeric id."""
    jid = extract_job_id(job_id) or (job_id or "").strip()
    if not jid or not jid.isdigit():
        return {}
    url = DETAIL_URL.format(job_id=jid)
    try:
        html = await _li_get_text(
            client,
            url,
            cache=cache,
            cache_kind="linkedin_detail",
            cache_params={"job_id": jid},
            cache_ttl=DETAIL_TTL_SEC,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("linkedin_detail_fail", job_id=jid, error=str(exc)[:160])
        return {}
    detail = parse_job_detail(html)
    if detail:
        logger.info(
            "linkedin_detail_ok",
            job_id=jid,
            desc_len=len(detail.get("description") or ""),
            seniority=detail.get("seniority") or "",
        )
    return detail


async def enrich_jobs(
    client: httpx.AsyncClient,
    jobs: list[JobListing],
    *,
    top_k: int = DEFAULT_ENRICH_TOP_K,
    cache: JobSearchCache | None = None,
) -> list[JobListing]:
    """Enrich the first ``top_k`` listings via guest job-detail pages."""
    k = max(0, int(top_k))
    if k <= 0 or not jobs:
        return jobs
    out = list(jobs)
    for i, job in enumerate(out[:k]):
        jid = extract_job_id(job.url) or extract_job_id(job.id)
        if not jid:
            # Try tags / description for urn
            jid = extract_job_id(f"{job.url} {job.description}")
        if not jid:
            continue
        detail = await fetch_job_detail(client, jid, cache=cache)
        if detail:
            out[i] = apply_detail_to_listing(job, detail)
    return out


async def fetch_linkedin(
    client: httpx.AsyncClient,
    query: str = "",
    locations: list[str] | None = None,
    pages: int = MAX_PAGES,
    *,
    titles: list[str] | None = None,
    skills: list[str] | None = None,
    data_dir: Path | str | None = None,
    cache: JobSearchCache | None = None,
    enrich_top_k: int = DEFAULT_ENRICH_TOP_K,
    user_q: str | None = None,
) -> list[JobListing]:
    """Fetch public guest job cards with query/location fanout + optional enrich.

    Parameters
    ----------
    query:
        Soft keyword used as primary fanout seed when ``user_q`` is empty
        (also used by callers that only pass a single string).
    user_q:
        Explicit user search box string (preferred over ``query`` for fanout).
    titles / skills:
        Profile fields for multi-query fanout.
    data_dir:
        When set, enables disk cache under ``{data_dir}/jobsearch_cache/``.
    enrich_top_k:
        After merge/dedupe, fetch detail HTML for the first K jobs (0 = skip).
    """
    js_cache = cache
    if js_cache is None and data_dir is not None:
        js_cache = JobSearchCache(data_dir)

    primary = (user_q if user_q is not None else query) or ""
    queries = build_search_queries(primary, titles, skills)
    # If user_q was empty but engine soft-filled ``query`` with titles already,
    # build_search_queries still dedupes. When primary is empty, also try query.
    if not primary and query and query.strip().lower() not in {q.lower() for q in queries}:
        # prepend soft query if distinct and room remains
        merged = build_search_queries(query, titles, skills)
        queries = merged

    locs = build_search_locations(locations)
    pages = max(1, min(int(pages or MAX_PAGES), 5))
    # Secondary fanout combos: fewer pages to stay polite
    n_combos = max(1, len(queries) * len(locs))

    all_jobs: list[JobListing] = []
    for qi, keywords in enumerate(queries):
        for loc in locs:
            pages_for = pages
            if n_combos > 2 and qi > 0:
                pages_for = min(pages, 1)
            elif n_combos > 4:
                pages_for = min(pages, 2 if qi == 0 else 1)

            for page in range(pages_for):
                params: dict[str, Any] = {
                    "keywords": keywords,
                    "start": page * 10,
                    "f_TPR": "r604800",  # past week — fresher, less load
                }
                if loc:
                    params["location"] = loc
                cache_params = {
                    "keywords": keywords,
                    "location": loc,
                    "start": page * 10,
                    "f_TPR": "r604800",
                }
                try:
                    html = await _li_get_text(
                        client,
                        SEARCH_URL,
                        params=params,
                        cache=js_cache,
                        cache_kind="linkedin_search",
                        cache_params=cache_params,
                        cache_ttl=SEARCH_TTL_SEC,
                    )
                except Exception:
                    # Bubble rate-limit / hard errors so fetch_sources records source fail
                    # only if we have zero results so far; otherwise keep what we have.
                    if not all_jobs:
                        raise
                    logger.warning(
                        "linkedin_page_fail",
                        keywords=keywords,
                        location=loc,
                        page=page,
                    )
                    break
                cards = _parse_cards(html)
                logger.info(
                    "linkedin_page",
                    page=page,
                    n=len(cards),
                    keywords=keywords[:80],
                    location=loc or "(anywhere)",
                )
                if not cards:
                    break
                all_jobs.extend(cards)
                if len(cards) < 5:
                    break

    # Dedupe within LinkedIn pages (URL first)
    seen: set[str] = set()
    uniq: list[JobListing] = []
    for j in all_jobs:
        key = (j.url or "").rstrip("/").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(j)

    if enrich_top_k and uniq:
        uniq = await enrich_jobs(client, uniq, top_k=enrich_top_k, cache=js_cache)

    return uniq
