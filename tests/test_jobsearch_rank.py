"""Unit tests for job search ranking v2 (no network)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from awareness.jobsearch.models import JobListing, JobProfile
from awareness.jobsearch.rank import _diversify_by_source, rank_jobs, score_job
from awareness.jobsearch.sources import dedupe_jobs


def _job(**kw) -> JobListing:
    base = dict(
        id="x",
        title="Backend Engineer",
        company="Acme",
        location="Remote",
        remote=True,
        url="https://example.com/jobs/1",
        source="remotive",
        source_label="Remotive",
        published_at=datetime.now(UTC) - timedelta(hours=5),
        tags=["python"],
        description="Build APIs with python and postgres",
    )
    base.update(kw)
    return JobListing(**base)


def test_rank_prefers_title_and_skill_match():
    profile = JobProfile(
        titles=["backend"],
        skills=["python", "postgres"],
        remote_only=True,
    )
    jobs = [
        _job(id="a", title="Marketing Lead", description="brand campaigns", tags=[], url="https://e.com/a"),
        _job(
            id="b",
            title="Backend Engineer",
            description="python postgres apis",
            tags=["python"],
            url="https://e.com/b",
        ),
    ]
    ranked = rank_jobs(jobs, profile, query="backend", limit=10)
    assert ranked
    assert ranked[0].title == "Backend Engineer"
    assert ranked[0].score > ranked[-1].score


def test_title_match_ranks_higher_than_description_only():
    """Field weights: skill/title in job title outrank description-only hits."""
    profile = JobProfile(skills=["python"], titles=["backend"])
    title_hit = _job(
        id="title",
        title="Python Backend Engineer",
        description="Build services for customers",
        tags=[],
        url="https://e.com/title",
        source="remotive",
    )
    desc_only = _job(
        id="desc",
        title="Platform Engineer",
        description="We use python and backend services extensively in our stack",
        tags=[],
        url="https://e.com/desc",
        source="remotive",
    )
    ranked = rank_jobs([desc_only, title_hit], profile, query="", limit=10)
    assert ranked[0].id == "title"
    assert ranked[0].score > ranked[1].score
    # Clearer reason labels for field hits
    reasons = " ".join(ranked[0].score_reasons)
    assert "skill:python@title" in reasons or "title:backend" in reasons


def test_phrase_match_bonus():
    profile = JobProfile(titles=["software engineer"])
    phrase = _job(
        id="phrase",
        title="Senior Software Engineer",
        description="generalist role",
        tags=[],
        url="https://e.com/phrase",
    )
    partial = _job(
        id="partial",
        title="Engineer",
        description="software tools and engineer culture",
        tags=[],
        url="https://e.com/partial",
    )
    sp = score_job(phrase, profile)
    so = score_job(partial, profile)
    assert sp.score > so.score
    assert any(r.startswith("phrase:") for r in sp.score_reasons)
    assert "phrase:software engineer" in sp.score_reasons


def test_freshness_continuous_decay():
    profile = JobProfile()
    fresh = score_job(_job(id="f", published_at=datetime.now(UTC) - timedelta(hours=1)), profile)
    mid = score_job(_job(id="m", published_at=datetime.now(UTC) - timedelta(days=7)), profile)
    old = score_job(_job(id="o", published_at=datetime.now(UTC) - timedelta(days=13)), profile)
    assert fresh.score > mid.score > old.score
    assert "fresh" in fresh.score_reasons
    # Near 14 days the boost is tiny but non-negative until the window ends
    assert mid.score > old.score


def test_linkedin_slot_reserve():
    """With limit=10 and many ATS + few LI, reserve ≥3 LI when ≥3 available."""
    profile = JobProfile(skills=["python"])
    jobs: list[JobListing] = []
    # 3 LinkedIn jobs (slightly lower raw skill density so ATS would win pure score order)
    for i in range(3):
        jobs.append(
            _job(
                id=f"li{i}",
                title=f"Engineer {i}",
                description="python",
                tags=["python"],
                source="linkedin",
                source_label="LinkedIn",
                url=f"https://linkedin.com/jobs/view/{i}",
                published_at=datetime.now(UTC) - timedelta(hours=10 + i),
            )
        )
    # Many ATS jobs with strong scores
    for i in range(20):
        jobs.append(
            _job(
                id=f"ats{i}",
                title=f"Python Backend Engineer {i}",
                description="python postgres kubernetes",
                tags=["python", "postgres"],
                source="ats",
                source_label="ATS",
                url=f"https://boards.greenhouse.io/x/jobs/{i}",
                published_at=datetime.now(UTC) - timedelta(hours=i),
            )
        )
    ranked = rank_jobs(jobs, profile, query="python", limit=10)
    assert len(ranked) == 10
    li_count = sum(1 for j in ranked if j.source == "linkedin")
    assert li_count >= 3
    # Direct diversify unit: reserve math on pre-sorted list
    scored = sorted(jobs, key=lambda j: j.id)  # arbitrary; diversify uses order given
    # Prefer score order like rank_jobs
    scored = [score_job(j.model_copy(deep=True), profile, "python") for j in jobs]
    scored.sort(key=lambda j: j.score, reverse=True)
    diversified = _diversify_by_source(scored, 10)
    assert sum(1 for j in diversified if j.source == "linkedin") >= 3


def test_linkedin_reserve_fills_when_not_enough_li():
    profile = JobProfile()
    jobs = [
        _job(id="li0", source="linkedin", source_label="LinkedIn", url="https://li/0"),
        *[_job(id=f"a{i}", source="ats", url=f"https://ats/{i}") for i in range(15)],
    ]
    ranked = rank_jobs(jobs, profile, limit=10)
    assert len(ranked) == 10
    assert sum(1 for j in ranked if j.source == "linkedin") == 1


def test_score_reasons_labels():
    profile = JobProfile(
        titles=["backend"],
        skills=["python"],
        exclude=["crypto"],
        remote_only=True,
    )
    job = _job(
        id="r",
        title="Backend Engineer",
        tags=["python"],
        description="remote python backend",
        source="linkedin",
        source_label="LinkedIn",
        url="https://linkedin.com/jobs/view/1",
    )
    scored = score_job(job, profile, query="backend")
    joined = scored.score_reasons
    assert any(r == "title:backend" or r.startswith("title:backend") for r in joined)
    assert any(r.startswith("skill:python@") for r in joined)
    assert "fresh" in joined
    assert "linkedin" in joined
    assert "remote" in joined


def test_remote_only_filters_onsite():
    profile = JobProfile(remote_only=True)
    jobs = [
        _job(id="r", remote=True, location="Remote", url="https://e.com/r"),
        _job(
            id="o",
            remote=False,
            location="New York",
            title="Office Engineer",
            description="onsite only",
            url="https://e.com/o",
        ),
    ]
    ranked = rank_jobs(jobs, profile, limit=10)
    ids = {j.id for j in ranked}
    assert "r" in ids
    # onsite should be filtered or scored out
    assert "o" not in ids or ranked[0].id == "r"


def test_exclude_and_min_salary_penalties():
    profile = JobProfile(exclude=["crypto"], min_salary=150000)
    bad = score_job(
        _job(id="bad", title="Crypto Engineer", salary="$80k", description="crypto trading"),
        profile,
    )
    good = score_job(
        _job(id="good", title="Backend Engineer", salary="$180000", description="apis"),
        profile,
    )
    assert any(r.startswith("exclude:") for r in bad.score_reasons)
    assert "salary-low" in bad.score_reasons
    assert bad.score < good.score


def test_dedupe_by_url():
    a = _job(id="1", url="https://e.com/job")
    b = _job(id="2", url="https://e.com/job/")
    out = dedupe_jobs([a, b])
    assert len(out) == 1
