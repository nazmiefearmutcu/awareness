"""Job search models — profile, listing, request/response."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# Public / guest-accessible boards. LinkedIn uses guest HTML (logged-out view).
SOURCE_CATALOG: dict[str, dict[str, str]] = {
    "linkedin": {
        "label": "LinkedIn",
        "kind": "guest",
        "url": "https://www.linkedin.com/jobs",
        "note": "Public guest job search (no login)",
    },
    "ats": {
        "label": "Company ATS",
        "kind": "api",
        "url": "https://boards-api.greenhouse.io",
        "note": "Greenhouse + Lever public boards (often same jobs as LinkedIn)",
    },
    "remoteok": {
        "label": "RemoteOK",
        "kind": "api",
        "url": "https://remoteok.com",
        "note": "Remote tech roles (public API)",
    },
    "remotive": {
        "label": "Remotive",
        "kind": "api",
        "url": "https://remotive.com",
        "note": "Remote jobs worldwide (public API)",
    },
    "arbeitnow": {
        "label": "Arbeitnow",
        "kind": "api",
        "url": "https://www.arbeitnow.com",
        "note": "EU-focused board (public API)",
    },
    "hn_hiring": {
        "label": "HN Who's Hiring",
        "kind": "api",
        "url": "https://news.ycombinator.com",
        "note": "Monthly HN hiring threads (Algolia)",
    },
    "wwr": {
        "label": "We Work Remotely",
        "kind": "rss",
        "url": "https://weworkremotely.com",
        "note": "Remote RSS feed",
    },
}

# LinkedIn + ATS first — primary real-world coverage.
DEFAULT_SOURCES = list(SOURCE_CATALOG.keys())


class JobProfile(BaseModel):
    """Simple personalization knobs — all optional, all free-text."""

    titles: list[str] = Field(default_factory=list, description="Desired titles / roles")
    skills: list[str] = Field(default_factory=list, description="Skills / stack keywords")
    locations: list[str] = Field(default_factory=list, description="Cities/countries; empty = anywhere")
    remote_only: bool = False
    exclude: list[str] = Field(default_factory=list, description="Words to push down / drop")
    min_salary: int | None = None
    sources: list[str] = Field(default_factory=lambda: list(DEFAULT_SOURCES))
    # Free-form notes used as soft keywords (e.g. "visa sponsorship", "junior ok")
    notes: str = ""


class JobListing(BaseModel):
    id: str
    title: str
    company: str = ""
    location: str = ""
    remote: bool = False
    url: str
    source: str
    source_label: str = ""
    published_at: datetime | None = None
    salary: str = ""
    tags: list[str] = Field(default_factory=list)
    description: str = ""
    score: float = 0.0
    score_reasons: list[str] = Field(default_factory=list)
    # True only when a real detail body was merged in (apply_detail_to_listing);
    # feeds the response "enriched" metric (M-37).
    enriched: bool = False


class JobSearchRequest(BaseModel):
    q: str = ""
    profile: JobProfile | None = None
    limit: int = Field(40, ge=1, le=100)
    # When true, profile is also persisted after search
    save_profile: bool = False
    # LinkedIn guest pagination (pages × ~10). Kept small by default.
    linkedin_pages: int = Field(3, ge=1, le=5)


class JobSearchResponse(BaseModel):
    query: str
    total: int
    took_ms: int
    sources_ok: list[str] = Field(default_factory=list)
    sources_err: dict[str, str] = Field(default_factory=dict)
    results: list[JobListing] = Field(default_factory=list)
    profile: JobProfile | None = None
    # Quality metadata for UI / debugging
    source_counts: dict[str, int] = Field(default_factory=dict)
    enriched: int = 0
    raw_total: int = 0


def split_csv(raw: str | None) -> list[str]:
    if not raw:
        return []
    out: list[str] = []
    for part in str(raw).replace(";", ",").split(","):
        t = part.strip()
        if t and t not in out:
            out.append(t)
    return out


def profile_from_flat(data: dict[str, Any]) -> JobProfile:
    """Accept either structured lists or comma strings from the UI."""

    def _list(key: str) -> list[str]:
        v = data.get(key)
        if v is None:
            return []
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        return split_csv(str(v))

    sources = _list("sources") or list(DEFAULT_SOURCES)
    sources = [s for s in sources if s in SOURCE_CATALOG]
    if not sources:
        sources = list(DEFAULT_SOURCES)
    min_sal = data.get("min_salary")
    if min_sal in ("", None):
        min_sal_i = None
    else:
        try:
            min_sal_i = int(min_sal)
        except (TypeError, ValueError):
            min_sal_i = None
    return JobProfile(
        titles=_list("titles"),
        skills=_list("skills"),
        locations=_list("locations"),
        remote_only=bool(data.get("remote_only", False)),
        exclude=_list("exclude"),
        min_salary=min_sal_i,
        sources=sources,
        notes=str(data.get("notes") or "").strip(),
    )
