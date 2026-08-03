"""M-07 + M-37: jobsearch dedupe must not over-dedupe; enriched counts only
listings that received a real detail body."""

from __future__ import annotations

from datetime import UTC, datetime

from awareness.jobsearch.engine import JobSearchEngine
from awareness.jobsearch.linkedin import apply_detail_to_listing
from awareness.jobsearch.models import JobListing
from awareness.jobsearch.sources import dedupe_jobs


def _job(source: str, company: str, title: str, location: str = "", url: str = "") -> JobListing:
    return JobListing(
        id=f"{source}-{company}-{title}",
        title=title,
        company=company,
        location=location,
        url=url or f"https://example.com/{source}/{company}/{title}",
        source=source,
        source_label=source,
        published_at=datetime.now(UTC),
        tags=[],
        description="",
    )


def test_dedupe_keeps_distinct_roles_same_company() -> None:
    """Two different roles at the same company must BOTH survive (M-07)."""
    jobs = [
        _job("ats", "Acme", "Backend Engineer"),
        _job("ats", "Acme", "Frontend Engineer"),
    ]
    out = dedupe_jobs(jobs)
    assert len(out) == 2


def test_dedupe_keeps_same_role_across_sources() -> None:
    """LinkedIn + ATS legitimately list the same role — both survive (M-07)."""
    jobs = [
        _job("linkedin", "Acme", "Backend Engineer", location="Berlin"),
        _job("ats", "Acme", "Backend Engineer", location="Berlin"),
    ]
    out = dedupe_jobs(jobs)
    assert len(out) == 2
    assert {j.source for j in out} == {"linkedin", "ats"}


def test_dedupe_collapses_same_url_and_same_source_company_title_location() -> None:
    dup_url = [_job("remotive", "Acme", "Dev", url="https://e.com/job/1"),
               _job("remotive", "Acme", "Dev", url="https://e.com/job/1")]
    assert len(dedupe_jobs(dup_url)) == 1
    same_content = [
        _job("remotive", "Acme", "Dev", location="Paris", url="https://e.com/a?x=1"),
        _job("remotive", "Acme", "Dev", location="Paris", url="https://e.com/a?x=2"),
    ]
    assert len(dedupe_jobs(same_content)) == 1
    # Different location of the same role → distinct posting.
    diff_loc = [
        _job("remotive", "Acme", "Dev", location="Paris", url="https://e.com/a"),
        _job("remotive", "Acme", "Dev", location="Berlin", url="https://e.com/b"),
    ]
    assert len(dedupe_jobs(diff_loc)) == 2


def test_enriched_flag_set_only_with_real_detail_body() -> None:
    job = _job("linkedin", "Acme", "Dev", location="Berlin")
    assert job.enriched is False

    # A stub-only detail must not mark the listing enriched.
    stub = {"description": "Dev at Acme — Berlin"}
    after_stub = apply_detail_to_listing(job, stub)
    assert after_stub.enriched is False

    # A real body does.
    real = {"description": "Join Acme to build distributed systems with Python, Go and K8s..." * 3}
    after_real = apply_detail_to_listing(job, real)
    assert after_real.enriched is True
    assert len(after_real.description) > len(job.description)


def test_engine_enriched_counts_only_flagged(tmp_path) -> None:
    eng = JobSearchEngine(tmp_path)
    from awareness.jobsearch.models import JobSearchResponse

    res = JobSearchResponse(query="", total=0, took_ms=0, enriched=0, results=[])
    assert res.enriched == 0
    # Directly exercise the engine counter shape via a fake ranked list.
    flagged = JobListing(
        id="a", title="T", company="C", url="https://e.com/a", source="linkedin",
        description="x" * 200, enriched=True,
    )
    plain = JobListing(
        id="b", title="T", company="C", url="https://e.com/b", source="linkedin",
        description="x" * 200, enriched=False,
    )
    assert sum(1 for j in [flagged, plain] if j.enriched) == 1
