"""Lightweight personalization scorer v2 — field-weighted, transparent reasons."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from awareness.jobsearch.models import JobListing, JobProfile

# Relative importance when a token hits a field (title >> description).
FIELD_WEIGHTS: dict[str, float] = {
    "title": 3.0,
    "tags": 1.5,
    "location": 2.0,
    "company": 0.5,
    "description": 1.0,
}

# Scale so multi-skill stacks stay in a usable score band.
_SKILL_UNIT = 4.0
_TITLE_UNIT = 5.0
_QUERY_UNIT = 5.0
_NOTES_UNIT = 2.0

_PHRASE_BONUS = 25.0
_FRESH_MAX = 12.0
_FRESH_ZERO_HOURS = 14.0 * 24.0  # ~14 days → boost decays to 0
_LINKEDIN_RESERVE_FRAC = 0.30


def _tokens(s: str) -> list[str]:
    return [t for t in re.split(r"[^\w+#./-]+", (s or "").lower()) if len(t) >= 2]


def _fields(job: JobListing) -> dict[str, str]:
    return {
        "title": (job.title or "").lower(),
        "company": (job.company or "").lower(),
        "location": (job.location or "").lower(),
        "tags": " ".join(job.tags or []).lower(),
        "description": (job.description or "")[:2000].lower(),
        "salary": (job.salary or "").lower(),
    }


def _haystack_from_fields(fields: dict[str, str]) -> str:
    return " ".join(fields.values())


def _token_in_field(token: str, text: str) -> bool:
    """Prefer word-ish boundaries; fall back to substring for short tech tokens."""
    if not token or not text:
        return False
    if re.search(rf"(?<![\w+#./-]){re.escape(token)}(?![\w+#./-])", text):
        return True
    # Short stack tokens (e.g. c++, go) often sit in free text without clean edges
    if len(token) <= 3 and token in text:
        return True
    return token in text and len(token) >= 4


def _field_hits(token: str, fields: dict[str, str]) -> list[str]:
    hits: list[str] = []
    for name in FIELD_WEIGHTS:
        if _token_in_field(token, fields.get(name, "")):
            hits.append(name)
    return hits


def _weighted_hits(token: str, fields: dict[str, str], unit: float) -> tuple[float, list[str]]:
    hits = _field_hits(token, fields)
    score = unit * sum(FIELD_WEIGHTS[h] for h in hits)
    return score, hits


def _freshness_boost(published_at: datetime | None) -> tuple[float, bool]:
    """Continuous freshness: ~12 at age 0, linear decay to 0 at ~14 days."""
    if not published_at:
        return 0.0, False
    pub = published_at
    if pub.tzinfo is None:
        pub = pub.replace(tzinfo=UTC)
    age_h = max(0.0, (datetime.now(UTC) - pub.astimezone(UTC)).total_seconds() / 3600.0)
    if age_h >= _FRESH_ZERO_HOURS:
        # Mild stale penalty beyond the decay window
        over = age_h - _FRESH_ZERO_HOURS
        return (-min(6.0, over / (7.0 * 24.0) * 6.0), False)
    boost = _FRESH_MAX * (1.0 - age_h / _FRESH_ZERO_HOURS)
    return (boost, boost >= 0.5)


def score_job(job: JobListing, profile: JobProfile, query: str = "") -> JobListing:
    fields = _fields(job)
    hay = _haystack_from_fields(fields)
    score = 0.0
    reasons: list[str] = []

    # Hard exclude: large penalty when term appears anywhere
    for ex in profile.exclude:
        ex_l = ex.lower().strip()
        if ex_l and ex_l in hay:
            score -= 25.0
            reasons.append(f"exclude:{ex}")

    # Query tokens — field-weighted
    q_terms = _tokens(query)
    for t in q_terms:
        pts, hits = _weighted_hits(t, fields, _QUERY_UNIT)
        if pts:
            score += pts
            primary = hits[0]
            reasons.append(f"query:{t}@{primary}" if primary != "title" else f"query:{t}")

    # Title / role preferences
    for title_pref in profile.titles:
        t_raw = title_pref.strip()
        t_l = t_raw.lower()
        if not t_l:
            continue
        # Phrase match: multi-word preference as substring of job title
        words = _tokens(t_l)
        if len(words) >= 2 and t_l in fields["title"]:
            score += _PHRASE_BONUS
            reasons.append(f"phrase:{t_raw}")
        # Per-token field-weighted contribution
        for tok in words or [t_l]:
            pts, hits = _weighted_hits(tok, fields, _TITLE_UNIT)
            if not pts:
                continue
            score += pts
            if "title" in hits:
                reasons.append(f"title:{tok}")
            else:
                reasons.append(f"title:{tok}@{hits[0]}")

    # Skills — field-weighted
    for skill in profile.skills:
        s_l = skill.lower().strip()
        if not s_l:
            continue
        pts, hits = _weighted_hits(s_l, fields, _SKILL_UNIT)
        if pts:
            score += pts
            if "title" in hits:
                reasons.append(f"skill:{s_l}@title")
            elif "tags" in hits:
                reasons.append(f"skill:{s_l}@tags")
            else:
                reasons.append(f"skill:{s_l}@{hits[0]}")

    # Locations
    if profile.locations:
        loc_hay = fields["location"]
        loc_hit = any(
            loc.lower() in loc_hay or loc.lower() in hay for loc in profile.locations if loc.strip()
        )
        if loc_hit:
            score += 14.0
            reasons.append("location")
        elif job.remote:
            score += 6.0
            reasons.append("remote-fallback")
        else:
            score -= 8.0
            reasons.append("loc-miss")

    # Remote preference (hard when remote_only)
    if profile.remote_only:
        if job.remote or "remote" in hay or "worldwide" in hay:
            score += 12.0
            reasons.append("remote")
        else:
            score -= 40.0
            reasons.append("not-remote")

    # Notes as soft keywords (field-weighted, light)
    for n in _tokens(profile.notes):
        pts, hits = _weighted_hits(n, fields, _NOTES_UNIT)
        if pts:
            score += pts

    # Continuous freshness
    fresh_pts, is_fresh = _freshness_boost(job.published_at)
    score += fresh_pts
    if is_fresh:
        reasons.append("fresh")
    elif fresh_pts < 0:
        reasons.append("stale")

    # Source trust (public boards we trust more)
    trust = {
        "linkedin": 4.0,  # primary public professional surface
        "ats": 3.5,  # canonical company postings (often dual-listed on LI)
        "remotive": 2.0,
        "remoteok": 2.0,
        "arbeitnow": 1.5,
        "wwr": 1.5,
        "hn_hiring": 3.0,
    }
    src_bonus = trust.get(job.source, 0.0)
    score += src_bonus
    if job.source == "linkedin" and src_bonus:
        reasons.append("linkedin")

    # Salary floor soft filter
    if profile.min_salary and job.salary:
        nums = [int(x) for x in re.findall(r"\d{2,6}", job.salary.replace(",", ""))]
        if nums and max(nums) < profile.min_salary:
            score -= 12.0
            reasons.append("salary-low")

    # Floor for empty matches still usable when browsing
    if not reasons and not q_terms and not profile.titles and not profile.skills:
        score += 1.0

    # Deduplicate reasons while preserving order; cap for UI
    seen: set[str] = set()
    uniq_reasons: list[str] = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            uniq_reasons.append(r)

    job.score = round(score, 2)
    job.score_reasons = uniq_reasons[:12]
    return job


def rank_jobs(
    jobs: list[JobListing],
    profile: JobProfile,
    query: str = "",
    limit: int = 40,
) -> list[JobListing]:
    scored = [score_job(j.model_copy(deep=True), profile, query) for j in jobs]
    # Drop hard remote misses when remote_only
    if profile.remote_only:
        scored = [j for j in scored if "not-remote" not in j.score_reasons or j.remote]
    # Drop very negative
    scored = [j for j in scored if j.score > -15]
    scored.sort(
        key=lambda j: (j.score, j.published_at or datetime.min.replace(tzinfo=UTC)),
        reverse=True,
    )
    # Round-robin by source with LinkedIn slot reserve
    return _diversify_by_source(scored, limit)


def _diversify_by_source(jobs: list[JobListing], limit: int) -> list[JobListing]:
    """Diversify result sources; reserve ~30% of slots for LinkedIn when available."""
    if limit <= 0:
        return []
    if len(jobs) <= limit:
        return jobs

    li_pool = [j for j in jobs if j.source == "linkedin"]
    other_pool = [j for j in jobs if j.source != "linkedin"]

    out: list[JobListing] = []
    used_ids: set[int] = set()

    # Reserve ~30% for LinkedIn (best LI first — pools already score-sorted)
    if li_pool:
        li_slots = max(1, int(round(limit * _LINKEDIN_RESERVE_FRAC)))
        li_slots = min(li_slots, len(li_pool), limit)
        for j in li_pool[:li_slots]:
            out.append(j)
            used_ids.add(id(j))
        li_remain = li_pool[li_slots:]
    else:
        li_remain = []

    # Remaining slots: round-robin across other sources, then leftover LI
    buckets: dict[str, list[JobListing]] = {}
    for j in other_pool:
        buckets.setdefault(j.source, []).append(j)
    if li_remain:
        buckets.setdefault("linkedin", []).extend(li_remain)

    order = sorted(
        buckets.keys(),
        key=lambda s: (0 if s == "linkedin" else 1 if s == "ats" else 2, s),
    )
    while len(out) < limit and any(buckets.get(s) for s in order):
        progressed = False
        for s in order:
            if buckets.get(s) and len(out) < limit:
                out.append(buckets[s].pop(0))
                progressed = True
        if not progressed:
            break

    return out
