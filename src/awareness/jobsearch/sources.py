"""Fetch + normalize listings from public job boards + LinkedIn guest surface.

LinkedIn: unauthenticated guest job HTML (same as a logged-out browser).
ATS: Greenhouse/Lever public JSON (canonical company postings).
Others: public APIs / RSS. No login, no CAPTCHA bypass, no credential use.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import httpx

from awareness.jobsearch.models import SOURCE_CATALOG, JobListing
from awareness.obs.logging import get_logger

logger = get_logger("jobsearch.sources")

USER_AGENT = "AwarenessJobSearch/0.1 (+https://github.com/local; research client)"
TIMEOUT = httpx.Timeout(22.0, connect=10.0)


def _id_for(*parts: str) -> str:
    raw = "|".join(p.strip().lower() for p in parts if p)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _parse_ts(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        # remoteok uses unix epoch
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(value).strip()
    if not s:
        return None
    # ISO-ish
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).astimezone(UTC)
    except ValueError:
        pass
    # RFC-ish via dateutil if present
    try:
        from dateutil import parser as date_parser  # type: ignore

        return date_parser.parse(s).astimezone(UTC)
    except Exception:  # noqa: BLE001
        return None


def _is_remote(text: str, location: str = "") -> bool:
    blob = f"{text} {location}".lower()
    return any(
        k in blob
        for k in (
            "remote",
            "work from home",
            "wfh",
            "distributed",
            "anywhere",
            "worldwide",
            "fully remote",
        )
    )


async def fetch_remoteok(client: httpx.AsyncClient) -> list[JobListing]:
    r = await client.get("https://remoteok.com/api")
    r.raise_for_status()
    data = r.json()
    out: list[JobListing] = []
    if not isinstance(data, list):
        return out
    for row in data:
        if not isinstance(row, dict) or not row.get("position"):
            continue
        title = str(row.get("position") or "")
        company = str(row.get("company") or "")
        loc = str(row.get("location") or "Remote")
        url = str(row.get("url") or row.get("apply_url") or "")
        if not url and row.get("id"):
            url = f"https://remoteok.com/remote-jobs/{row['id']}"
        if not url:
            continue
        tags = [str(t) for t in (row.get("tags") or []) if t]
        salary = ""
        if row.get("salary_min") or row.get("salary_max"):
            lo = row.get("salary_min") or ""
            hi = row.get("salary_max") or ""
            salary = f"{lo}-{hi}".strip("-")
        out.append(
            JobListing(
                id=_id_for("remoteok", url, title),
                title=title,
                company=company,
                location=loc or "Remote",
                remote=True,
                url=url,
                source="remoteok",
                source_label=SOURCE_CATALOG["remoteok"]["label"],
                published_at=_parse_ts(row.get("date") or row.get("epoch")),
                salary=salary,
                tags=tags,
                description=str(row.get("description") or "")[:3000],
            )
        )
    return out


async def fetch_remotive(client: httpx.AsyncClient, search: str = "") -> list[JobListing]:
    params = {}
    if search:
        params["search"] = search
    r = await client.get("https://remotive.com/api/remote-jobs", params=params or None)
    r.raise_for_status()
    data = r.json()
    jobs = data.get("jobs") if isinstance(data, dict) else None
    out: list[JobListing] = []
    if not isinstance(jobs, list):
        return out
    for row in jobs:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "")
        url = str(row.get("url") or "")
        if not title or not url:
            continue
        loc = str(row.get("candidate_required_location") or "Remote")
        tags = [str(t) for t in (row.get("tags") or []) if t]
        cat = row.get("category")
        if cat:
            tags = [str(cat), *tags]
        out.append(
            JobListing(
                id=_id_for("remotive", url, title),
                title=title,
                company=str(row.get("company_name") or ""),
                location=loc,
                remote=True,
                url=url,
                source="remotive",
                source_label=SOURCE_CATALOG["remotive"]["label"],
                published_at=_parse_ts(row.get("publication_date")),
                salary=str(row.get("salary") or ""),
                tags=tags,
                description=str(row.get("description") or "")[:3000],
            )
        )
    return out


async def fetch_arbeitnow(client: httpx.AsyncClient) -> list[JobListing]:
    r = await client.get("https://www.arbeitnow.com/api/job-board-api")
    r.raise_for_status()
    data = r.json()
    rows = data.get("data") if isinstance(data, dict) else None
    out: list[JobListing] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "")
        url = str(row.get("url") or "")
        if not title or not url:
            continue
        loc = str(row.get("location") or "")
        tags = [str(t) for t in (row.get("tags") or []) if t]
        remote = bool(row.get("remote")) or _is_remote(title, loc)
        out.append(
            JobListing(
                id=_id_for("arbeitnow", url, title),
                title=title,
                company=str(row.get("company_name") or ""),
                location=loc,
                remote=remote,
                url=url,
                source="arbeitnow",
                source_label=SOURCE_CATALOG["arbeitnow"]["label"],
                published_at=_parse_ts(row.get("created_at")),
                tags=tags,
                description=str(row.get("description") or "")[:3000],
            )
        )
    return out


async def fetch_hn_hiring(client: httpx.AsyncClient, query: str = "") -> list[JobListing]:
    """Pull recent 'Who is hiring' comment posts via Algolia HN API."""
    # Find latest "Who is hiring" story
    r = await client.get(
        "https://hn.algolia.com/api/v1/search",
        params={
            "query": "Who is hiring",
            "tags": "story,author_whoishiring",
            "hitsPerPage": 3,
        },
    )
    r.raise_for_status()
    stories = r.json().get("hits") or []
    story_ids = [str(h.get("objectID")) for h in stories if h.get("objectID")]
    if not story_ids:
        return []

    out: list[JobListing] = []
    q = (query or "").strip()
    for sid in story_ids[:2]:
        params: dict[str, Any] = {
            "tags": f"comment,story_{sid}",
            "hitsPerPage": 80,
        }
        if q:
            params["query"] = q
        cr = await client.get("https://hn.algolia.com/api/v1/search_by_date", params=params)
        cr.raise_for_status()
        for hit in cr.json().get("hits") or []:
            text = str(hit.get("comment_text") or "")
            if not text or len(text) < 40:
                continue
            # First line often has Company | Role | Location
            plain = re.sub(r"<[^>]+>", " ", text)
            plain = re.sub(r"\s+", " ", plain).strip()
            first = plain[:180]
            company = ""
            title = first
            if "|" in first:
                bits = [b.strip() for b in first.split("|") if b.strip()]
                if bits:
                    company = bits[0][:80]
                if len(bits) > 1:
                    title = bits[1][:120]
            elif " is hiring" in first.lower():
                m = re.match(r"(.+?)\s+is hiring[:\s]+(.+)", first, re.I)
                if m:
                    company = m.group(1)[:80]
                    title = m.group(2)[:120]
            url = f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
            loc = ""
            for token in ("Remote", "remote", "ONSITE", "Hybrid", "hybrid"):
                if token.lower() in plain[:400].lower():
                    loc = token.capitalize() if token.lower() != "onsite" else "Onsite"
                    break
            out.append(
                JobListing(
                    id=_id_for("hn_hiring", url),
                    title=title or "Hiring",
                    company=company or "HN",
                    location=loc or ("Remote" if _is_remote(plain) else ""),
                    remote=_is_remote(plain, loc),
                    url=url,
                    source="hn_hiring",
                    source_label=SOURCE_CATALOG["hn_hiring"]["label"],
                    published_at=_parse_ts(hit.get("created_at")),
                    tags=["hackernews", "hiring"],
                    description=plain[:3000],
                )
            )
    return out


async def fetch_wwr(client: httpx.AsyncClient) -> list[JobListing]:
    r = await client.get("https://weworkremotely.com/categories/remote-programming-jobs.rss")
    r.raise_for_status()
    root = ET.fromstring(r.text)
    out: list[JobListing] = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        pub = item.findtext("pubDate")
        if not title or not link:
            continue
        company = ""
        role = title
        if ":" in title:
            company, role = [p.strip() for p in title.split(":", 1)]
        out.append(
            JobListing(
                id=_id_for("wwr", link, title),
                title=role or title,
                company=company,
                location="Remote",
                remote=True,
                url=link,
                source="wwr",
                source_label=SOURCE_CATALOG["wwr"]["label"],
                published_at=_parse_ts(pub),
                tags=["remote", "wwr"],
                description=re.sub(r"<[^>]+>", " ", desc)[:3000],
            )
        )
    return out


async def fetch_sources(
    sources: list[str],
    query: str = "",
    *,
    locations: list[str] | None = None,
    linkedin_pages: int = 3,
    titles: list[str] | None = None,
    skills: list[str] | None = None,
    data_dir: str | Path | None = None,
    user_q: str | None = None,
    linkedin_enrich_top_k: int = 25,
) -> tuple[list[JobListing], list[str], dict[str, str]]:
    import asyncio

    from awareness.jobsearch.ats import fetch_ats
    from awareness.jobsearch.linkedin import fetch_linkedin

    ok: list[str] = []
    err: dict[str, str] = {}
    jobs: list[JobListing] = []
    dd: Path | None = Path(data_dir) if data_dir is not None else None

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, application/rss+xml, text/html, */*",
    }
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers, follow_redirects=True) as client:

        async def _one(name: str) -> None:
            try:
                if name == "linkedin":
                    rows = await fetch_linkedin(
                        client,
                        query=query,
                        locations=locations,
                        pages=linkedin_pages,
                        titles=titles,
                        skills=skills,
                        data_dir=dd,
                        user_q=user_q,
                        enrich_top_k=linkedin_enrich_top_k,
                    )
                elif name == "ats":
                    rows = await fetch_ats(client, query)
                elif name == "remoteok":
                    rows = await fetch_remoteok(client)
                elif name == "remotive":
                    rows = await fetch_remotive(client, query)
                elif name == "arbeitnow":
                    rows = await fetch_arbeitnow(client)
                elif name == "hn_hiring":
                    rows = await fetch_hn_hiring(client, query)
                elif name == "wwr":
                    rows = await fetch_wwr(client)
                else:
                    err[name] = "unknown source"
                    return
                jobs.extend(rows)
                ok.append(name)
                logger.info("job_source_ok", source=name, n=len(rows))
            except Exception as exc:  # noqa: BLE001
                err[name] = str(exc)[:200]
                logger.warning("job_source_fail", source=name, error=str(exc))

        await asyncio.gather(*[_one(s) for s in sources if s in SOURCE_CATALOG])

    return jobs, ok, err


def dedupe_jobs(jobs: list[JobListing]) -> list[JobListing]:
    seen: set[str] = set()
    out: list[JobListing] = []
    for j in jobs:
        key = j.url.rstrip("/").lower()
        alt = f"{j.company.lower()}::{j.title.lower()}"
        if key in seen or alt in seen:
            continue
        seen.add(key)
        seen.add(alt)
        out.append(j)
    return out
