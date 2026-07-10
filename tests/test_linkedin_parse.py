"""LinkedIn guest HTML parsers + query fanout (no network)."""

from __future__ import annotations

from awareness.jobsearch.linkedin import (
    apply_detail_to_listing,
    build_search_locations,
    build_search_queries,
    extract_job_id,
    parse_job_detail,
    _parse_cards,
)
from awareness.jobsearch.models import JobListing

SAMPLE = """
<li>
  <div class="base-card relative w-full base-search-card job-search-card"
       data-entity-urn="urn:li:jobPosting:4427346679">
    <a class="base-card__full-link"
       href="https://www.linkedin.com/jobs/view/python-developer-at-deloitte-4427346679?refId=x">
    </a>
    <h3 class="base-search-card__title">Python Developer</h3>
    <h4 class="base-search-card__subtitle">Deloitte</h4>
    <span class="job-search-card__location">London, England, United Kingdom</span>
    <time class="job-search-card__listdate" datetime="2026-07-08">1 day ago</time>
  </div>
</li>
"""

# Minimal guest job-detail HTML shaped like LinkedIn's jobs-guest jobPosting page.
DETAIL_SAMPLE = """
<!DOCTYPE html>
<html>
<body>
  <section class="top-card-layout container">
    <h1 class="top-card-layout__title">Senior Python Developer</h1>
    <a class="topcard__org-name-link topcard__flavor--black-link"
       href="https://www.linkedin.com/company/deloitte">Deloitte Digital</a>
    <span class="topcard__flavor topcard__flavor--bullet">London, England, United Kingdom</span>
  </section>
  <div class="description__text description__text--rich">
    <div class="show-more-less-html__markup">
      <p>We are hiring a <strong>Senior Python Developer</strong> to build APIs with
      FastAPI, Postgres, and Kubernetes. Remote-friendly within the UK.</p>
      <ul>
        <li>5+ years Python</li>
        <li>Experience with cloud platforms</li>
      </ul>
    </div>
  </div>
  <ul class="description__job-criteria-list">
    <li class="description__job-criteria-item">
      <h3 class="description__job-criteria-subheader">Seniority level</h3>
      <span class="description__job-criteria-text description__job-criteria-text--criteria">
        Mid-Senior level
      </span>
    </li>
    <li class="description__job-criteria-item">
      <h3 class="description__job-criteria-subheader">Employment type</h3>
      <span class="description__job-criteria-text description__job-criteria-text--criteria">
        Full-time
      </span>
    </li>
    <li class="description__job-criteria-item">
      <h3 class="description__job-criteria-subheader">Job function</h3>
      <span class="description__job-criteria-text description__job-criteria-text--criteria">
        Engineering
      </span>
    </li>
    <li class="description__job-criteria-item">
      <h3 class="description__job-criteria-subheader">Industries</h3>
      <span class="description__job-criteria-text description__job-criteria-text--criteria">
        IT Services and IT Consulting
      </span>
    </li>
  </ul>
</body>
</html>
"""


def test_parse_cards_extracts_listing():
    jobs = _parse_cards(SAMPLE)
    assert len(jobs) >= 1
    j = jobs[0]
    assert j.source == "linkedin"
    assert "Python" in j.title
    assert "Deloitte" in j.company
    assert "linkedin.com/jobs/view" in j.url
    assert "4427346679" in j.url or j.id
    assert extract_job_id(j.url) == "4427346679"


def test_extract_job_id_variants():
    assert extract_job_id("https://www.linkedin.com/jobs/view/python-dev-at-x-4436043581") == "4436043581"
    assert extract_job_id("urn:li:jobPosting:4436043581") == "4436043581"
    assert extract_job_id("https://www.linkedin.com/jobs/view/4436043581") == "4436043581"
    assert extract_job_id("https://www.linkedin.com/jobs/view/foo-4436043581?refId=abc") == "4436043581"
    assert extract_job_id("") == ""


def test_parse_job_detail_extracts_description_and_criteria():
    detail = parse_job_detail(DETAIL_SAMPLE)
    assert detail
    assert "Senior Python Developer" in detail["title"]
    assert "Deloitte" in detail["company"]
    assert "FastAPI" in detail["description"] or "Python" in detail["description"]
    assert "Mid-Senior" in detail["seniority"]
    assert "Full-time" in detail["employment"]
    assert "Engineering" in detail["job_function"]


def test_apply_detail_to_listing_improves_description():
    job = JobListing(
        id="x",
        title="Python Developer",
        company="Deloitte",
        location="London",
        url="https://www.linkedin.com/jobs/view/python-developer-at-deloitte-4427346679",
        source="linkedin",
        description="Python Developer at Deloitte — London",
        tags=["linkedin"],
    )
    detail = parse_job_detail(DETAIL_SAMPLE)
    enriched = apply_detail_to_listing(job, detail)
    assert len(enriched.description) > len(job.description)
    assert "FastAPI" in enriched.description or "Kubernetes" in enriched.description
    assert enriched.title.startswith("Senior") or "Python" in enriched.title
    assert any("seniority:" in t for t in enriched.tags)
    assert any("employment:" in t for t in enriched.tags)


def test_build_search_queries_fanout_and_dedupe():
    qs = build_search_queries(
        "backend engineer",
        titles=["Backend Engineer", "Platform Engineer", "Ignored Third"],
        skills=["python", "postgres", "k8s"],
    )
    assert len(qs) <= 4
    # user q + 2 titles + skills join
    assert qs[0] == "backend engineer"
    assert "Platform Engineer" in qs
    # titles capped at 2 — third title not included as its own query
    assert not any(q.lower() == "ignored third" for q in qs)
    # skills joined into one query
    skills_q = [q for q in qs if "python" in q.lower() and "postgres" in q.lower()]
    assert skills_q

    # Dedup: same title as q
    qs2 = build_search_queries("Backend Engineer", titles=["backend engineer", "SRE"], skills=["go"])
    assert len([q for q in qs2 if q.lower() == "backend engineer"]) == 1
    assert "SRE" in qs2

    # Empty → fallback
    qs3 = build_search_queries("", titles=[], skills=[])
    assert qs3 == ["software engineer"]


def test_build_search_locations():
    assert build_search_locations(None) == [""]
    assert build_search_locations([]) == [""]
    locs = build_search_locations(["London", "Berlin", "Paris", "Tokyo", "london"])
    assert locs == ["London", "Berlin", "Paris"]
    assert "Tokyo" not in locs


def test_jobsearch_cache_roundtrip(tmp_path):
    from awareness.jobsearch.cache import DETAIL_TTL_SEC, SEARCH_TTL_SEC, JobSearchCache

    cache = JobSearchCache(tmp_path)
    assert cache.get("linkedin_search", {"keywords": "python", "start": 0}) is None
    cache.set("linkedin_search", {"keywords": "python", "start": 0}, SAMPLE, SEARCH_TTL_SEC)
    assert cache.get("linkedin_search", {"keywords": "python", "start": 0}) == SAMPLE
    cache.set("linkedin_detail", {"job_id": "4427346679"}, DETAIL_SAMPLE, DETAIL_TTL_SEC)
    assert "Seniority level" in cache.get("linkedin_detail", {"job_id": "4427346679"})
    # Different params → miss
    assert cache.get("linkedin_search", {"keywords": "other", "start": 0}) is None
    assert (tmp_path / "jobsearch_cache").is_dir()
