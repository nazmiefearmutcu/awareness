"""Persist job-search profile as JSON under data_dir."""

from __future__ import annotations

import json
from pathlib import Path

from awareness.jobsearch.models import JobProfile
from awareness.obs.logging import get_logger

logger = get_logger("jobsearch.profile")


def profile_path(data_dir: Path) -> Path:
    return Path(data_dir) / "jobsearch_profile.json"


def load_profile(data_dir: Path) -> JobProfile:
    from awareness.jobsearch.models import DEFAULT_SOURCES, SOURCE_CATALOG

    path = profile_path(data_dir)
    if not path.exists():
        return JobProfile()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        profile = JobProfile.model_validate(raw)
        # Migrate pre-LinkedIn profiles (neither linkedin nor ats present).
        src = list(profile.sources or [])
        if not src:
            profile.sources = list(DEFAULT_SOURCES)
        elif "linkedin" not in src and "ats" not in src:
            profile.sources = ["linkedin", "ats"] + [s for s in src if s in SOURCE_CATALOG]
            logger.info("job_profile_upgraded_sources", sources=profile.sources)
        return profile
    except Exception as exc:  # noqa: BLE001
        logger.warning("job_profile_load_failed", error=str(exc))
        return JobProfile()


def save_profile(data_dir: Path, profile: JobProfile) -> JobProfile:
    path = profile_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    return profile
