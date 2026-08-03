"""Orchestrate fetch → dedupe → personalize rank."""

from __future__ import annotations

import time
from pathlib import Path

from awareness.jobsearch.models import (
    DEFAULT_SOURCES,
    SOURCE_CATALOG,
    JobProfile,
    JobSearchRequest,
    JobSearchResponse,
)
from awareness.jobsearch.profile_store import load_profile, save_profile
from awareness.jobsearch.rank import rank_jobs
from awareness.jobsearch.sources import dedupe_jobs, fetch_sources


class JobSearchEngine:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)

    def get_profile(self) -> JobProfile:
        return load_profile(self.data_dir)

    def put_profile(self, profile: JobProfile) -> JobProfile:
        return save_profile(self.data_dir, profile)

    def catalog(self) -> list[dict[str, str]]:
        return [{"id": k, **v} for k, v in SOURCE_CATALOG.items()]

    async def search(self, req: JobSearchRequest) -> JobSearchResponse:
        t0 = time.perf_counter()
        profile = req.profile or self.get_profile()
        if req.save_profile and req.profile is not None:
            self.put_profile(profile)

        sources = [s for s in (profile.sources or DEFAULT_SOURCES) if s in SOURCE_CATALOG]
        if not sources:
            sources = list(DEFAULT_SOURCES)

        # Prefer first title / skills as soft query for APIs that accept search
        user_q = (req.q or "").strip()
        soft_q = user_q
        if not soft_q and profile.titles:
            soft_q = " ".join(profile.titles[:2])
        if not soft_q and profile.skills:
            soft_q = " ".join(profile.skills[:3])
        # LinkedIn guest search works best with a real keyword string
        if not soft_q:
            soft_q = "software engineer"

        raw, ok, err = await fetch_sources(
            sources,
            query=soft_q,
            locations=profile.locations or None,
            linkedin_pages=req.linkedin_pages,
            titles=list(profile.titles or []),
            skills=list(profile.skills or []),
            data_dir=self.data_dir,
            user_q=user_q,
        )
        uniq = dedupe_jobs(raw)
        # Score with full user query + titles for personalization
        rank_q = " ".join(
            x for x in [(req.q or "").strip(), " ".join(profile.titles), " ".join(profile.skills)] if x
        )
        ranked = rank_jobs(uniq, profile, query=rank_q, limit=req.limit)
        took = int((time.perf_counter() - t0) * 1000)

        source_counts: dict[str, int] = {}
        for j in ranked:
            source_counts[j.source] = source_counts.get(j.source, 0) + 1
        # Enriched = listings that actually received a merged detail body via
        # apply_detail_to_listing (flag tracked on the listing) — M-37.
        enriched = sum(1 for j in ranked if j.enriched)

        return JobSearchResponse(
            query=req.q,
            total=len(ranked),
            took_ms=took,
            sources_ok=ok,
            sources_err=err,
            results=ranked,
            profile=profile,
            source_counts=source_counts,
            enriched=enriched,
            raw_total=len(uniq),
        )
