"""M-06: token matcher must be real word-boundary, not substring.

``ai`` must match " ai " and "ai," but never "email"; ``go`` must not match
"golang". Applies to the ranker's ``_token_in_field``, the hard-exclude path,
and the ATS board ``_match_query``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from awareness.jobsearch import ats
from awareness.jobsearch.models import JobListing, JobProfile
from awareness.jobsearch.rank import _token_in_field, score_job


def test_token_in_field_does_not_match_email() -> None:
    assert not _token_in_field("ai", "email engineer")
    assert not _token_in_field("ai", "my email is x@y.com")
    assert not _token_in_field("go", "golang developer")
    assert not _token_in_field("net", "internet")


def test_token_in_field_matches_real_tokens() -> None:
    assert _token_in_field("ai", " ai engineer")
    assert _token_in_field("ai", "ai, engineer")
    assert _token_in_field("ai", "engineer ai")
    assert _token_in_field("ai", "engineer ai!")
    assert _token_in_field("go", "knows go and python")
    assert _token_in_field("c++", "knows c++")
    assert _token_in_field("c#", "c# developer")
    assert _token_in_field(".net", ".net developer")


def test_ats_match_query_uses_boundaries() -> None:
    assert ats._match_query("Email Marketing Specialist", "ai") is False
    assert ats._match_query("AI Engineer", "ai") is True
    assert ats._match_query("Senior Backend Engineer Python", "backend python") is True
    assert ats._match_query("Backend Golang Developer", "go") is False


def test_exclude_is_token_boundary_aware() -> None:
    def _job(**kw) -> JobListing:
        base = dict(
            id="x",
            title="ML Engineer",
            company="Acme",
            location="Remote",
            remote=True,
            url="https://example.com/jobs/1",
            source="remotive",
            source_label="Remotive",
            published_at=datetime.now(UTC),
            tags=[],
            description="build ML systems with email automations",
        )
        base.update(kw)
        return JobListing(**base)

    # Exclude "ai" must NOT fire on the "email" inside the description.
    profile = JobProfile(exclude=["ai"])
    j = score_job(_job(), profile)
    assert not any(r.startswith("exclude:") for r in j.score_reasons)

    # Exclude "ai" must fire when " ai " appears as a real token.
    j2 = score_job(_job(description="build AI models in pytorch"), profile)
    assert any(r.startswith("exclude:") for r in j2.score_reasons)
