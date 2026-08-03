"""Company ATS boards — often the canonical source of jobs also listed on LinkedIn.

Public JSON endpoints (no auth):
  - Greenhouse: boards-api.greenhouse.io
  - Lever: api.lever.co
  - Ashby: api.ashbyhq.com/posting-api/job-board/{board}

Board lists load from ``configs/jobsearch_boards.yaml`` when present; otherwise
embedded defaults are used. Per-board fetch failures are soft (logged, skipped).
"""

from __future__ import annotations

import asyncio
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
import yaml

from awareness.jobsearch.models import SOURCE_CATALOG, JobListing
from awareness.obs.logging import get_logger

logger = get_logger("jobsearch.ats")

# Concurrent board fetches per batch (polite: avoid exploding outbound fan-out).
_BOARD_BATCH_SIZE = 5

# Curated high-signal tech boards (public board slug, not a secret).
# Used when YAML is missing or empty for a provider.
DEFAULT_GREENHOUSE_BOARDS: list[str] = [
    "stripe",
    "airbnb",
    "figma",
    "notion",
    "cloudflare",
    "datadog",
    "discord",
    "gitlab",
    "hashicorp",
    "openai",
    "anthropic",
    "elastic",
    "mongodb",
    "snowflakecomputing",
    "twilio",
    "shopify",
    "reddit",
    "dropbox",
    "airtable",
    "asana",
    "doordash",
    "robinhood",
    "coinbase",
    "plaid",
    "brex",
    "ramp",
    "duolingo",
    "instacart",
    "lyft",
    "hubspot",
    "pinterest",
    "github",
    "mozilla",
    "intercom",
    "gusto",
    "calendly",
    "grammarly",
    "coursera",
    "affirm",
    "rivian",
    "anduril",
    "postman",
    "mixpanel",
    "nerdwallet",
    "square",
]

DEFAULT_LEVER_COMPANIES: list[str] = [
    "netflix",
    "palantir",
    "twitch",
    "spotify",
    "box",
    "canva",
    "fingerfood",
    "activecampaign",
    "yelp",
    "loom",
    "wealthsimple",
    "eventbrite",
    "outreach",
    "buildkite",
    "coursehero",
    "gojek",
    "nylas",
    "lattice",
]

DEFAULT_ASHBY_BOARDS: list[str] = [
    "ashby",
    "linear",
    "retool",
    "vercel",
    "mercury",
    "runway",
    "perplexity",
    "clerk",
    "sourcegraph",
    "baseten",
    "sierra",
    "harvey",
    "cursor",
    "ramp",
    "notion",
]

# Back-compat aliases (resolved at load time via load_boards()).
GREENHOUSE_BOARDS = list(DEFAULT_GREENHOUSE_BOARDS)
LEVER_COMPANIES = list(DEFAULT_LEVER_COMPANIES)
ASHBY_BOARDS = list(DEFAULT_ASHBY_BOARDS)


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


def _parse_ts(value: Any):
    from datetime import UTC, datetime

    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        # Lever uses milliseconds
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(value).strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).astimezone(UTC)
    except ValueError:
        return None


def _strip_html(html: str) -> str:
    import re
    from html import unescape

    t = re.sub(r"(?s)<[^>]+>", " ", html or "")
    return unescape(re.sub(r"\s+", " ", t)).strip()


def re_split(q: str) -> list[str]:
    import re

    return re.split(r"[^\w+#./-]+", q.lower())


def _match_query(text: str, query: str) -> bool:
    q = (query or "").strip().lower()
    if not q:
        return True
    hay = (text or "").lower()
    # All tokens must appear as real tokens (AND) — same boundary class as the
    # ranker (M-06): "ai" must not match "email", "go" must not match "golang".
    from awareness.jobsearch.rank import _token_in_field

    tokens = [t for t in re_split(q) if len(t) > 1]
    if not tokens:
        return True
    return all(_token_in_field(t, hay) for t in tokens)


def _normalize_board_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if item is None:
            continue
        slug = str(item).strip().lower()
        if not slug or slug.startswith("#") or slug in seen:
            continue
        seen.add(slug)
        out.append(slug)
    return out


def _candidate_board_paths() -> list[Path]:
    """Resolve likely locations for jobsearch_boards.yaml."""
    paths: list[Path] = []
    env = os.environ.get("AW_JOBSEARCH_BOARDS") or os.environ.get("AW_JOBSEARCH_BOARDS_FILE")
    if env:
        paths.append(Path(env).expanduser())

    # Package-relative: src/awareness/jobsearch/ats.py -> project root = parents[3]
    try:
        pkg_root = Path(__file__).resolve().parents[3]
        paths.append(pkg_root / "configs" / "jobsearch_boards.yaml")
    except IndexError:
        pass

    # Settings project root (honours AW_PROJECT_ROOT)
    try:
        from awareness.config.settings import _project_root

        root = _project_root()
        paths.append(root / "configs" / "jobsearch_boards.yaml")
        # data_dir parent / configs (deploy layouts)
        try:
            from awareness.config.settings import get_settings

            data_dir = Path(get_settings().data_dir)
            paths.append(data_dir.parent / "configs" / "jobsearch_boards.yaml")
        except Exception:
            pass
    except Exception:
        pass

    # CWD fallbacks
    cwd = Path.cwd()
    paths.append(cwd / "configs" / "jobsearch_boards.yaml")
    paths.append(cwd / "jobsearch_boards.yaml")

    # Dedup while preserving order
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in paths:
        key = str(p.resolve()) if p.exists() else str(p)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    return uniq


@lru_cache(maxsize=1)
def load_boards() -> dict[str, list[str]]:
    """Load board lists from YAML; fall back to embedded defaults.

    Returns keys: greenhouse, lever, ashby — each a non-empty list when possible.
    """
    greenhouse = list(DEFAULT_GREENHOUSE_BOARDS)
    lever = list(DEFAULT_LEVER_COMPANIES)
    ashby = list(DEFAULT_ASHBY_BOARDS)
    loaded_from: str | None = None

    for path in _candidate_board_paths():
        try:
            if not path.is_file():
                continue
            with open(path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            if not isinstance(data, dict):
                logger.warning("jobsearch_boards_invalid", path=str(path), reason="not a mapping")
                continue
            gh = _normalize_board_list(data.get("greenhouse"))
            lv = _normalize_board_list(data.get("lever"))
            ab = _normalize_board_list(data.get("ashby"))
            if gh:
                greenhouse = gh
            if lv:
                lever = lv
            if ab:
                ashby = ab
            loaded_from = str(path)
            break
        except Exception as exc:
            logger.warning("jobsearch_boards_load_fail", path=str(path), error=str(exc)[:120])
            continue

    if loaded_from:
        logger.info(
            "jobsearch_boards_loaded",
            path=loaded_from,
            greenhouse=len(greenhouse),
            lever=len(lever),
            ashby=len(ashby),
        )
    else:
        logger.info(
            "jobsearch_boards_defaults",
            greenhouse=len(greenhouse),
            lever=len(lever),
            ashby=len(ashby),
        )

    return {
        "greenhouse": greenhouse,
        "lever": lever,
        "ashby": ashby,
    }


def get_greenhouse_boards() -> list[str]:
    return list(load_boards()["greenhouse"])


def get_lever_companies() -> list[str]:
    return list(load_boards()["lever"])


def get_ashby_boards() -> list[str]:
    return list(load_boards()["ashby"])


# Refresh module-level aliases from YAML when importable (tests may clear cache).
def _sync_module_board_aliases() -> None:
    global GREENHOUSE_BOARDS, LEVER_COMPANIES, ASHBY_BOARDS
    boards = load_boards()
    GREENHOUSE_BOARDS = list(boards["greenhouse"])
    LEVER_COMPANIES = list(boards["lever"])
    ASHBY_BOARDS = list(boards["ashby"])


_sync_module_board_aliases()


async def _fetch_boards_batched(
    boards: list[str],
    fetch_one,
    *,
    batch_size: int = _BOARD_BATCH_SIZE,
) -> list[JobListing]:
    """Run per-board coroutines in small gather batches."""
    out: list[JobListing] = []
    if not boards:
        return out
    size = max(1, batch_size)
    for i in range(0, len(boards), size):
        batch = boards[i : i + size]
        results = await asyncio.gather(*[fetch_one(b) for b in batch], return_exceptions=True)
        for board, result in zip(batch, results, strict=False):
            if isinstance(result, Exception):
                logger.warning("ats_board_batch_fail", board=board, error=str(result)[:120])
                continue
            if isinstance(result, list):
                out.extend(result)
    return out


async def _fetch_greenhouse_board(client: httpx.AsyncClient, board: str, query: str = "") -> list[JobListing]:
    out: list[JobListing] = []
    url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
    try:
        r = await client.get(url, params={"content": "true"})
        if r.status_code == 404:
            return out
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        logger.warning("greenhouse_board_fail", board=board, error=str(exc)[:120])
        return out
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        return out
    for row in jobs:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "")
        abs_url = str(row.get("absolute_url") or "")
        if not title or not abs_url:
            continue
        loc_obj = row.get("location") or {}
        loc = str(loc_obj.get("name") if isinstance(loc_obj, dict) else loc_obj or "")
        desc = str(row.get("content") or "")[:3000]
        blob = f"{title} {loc} {desc}"
        if not _match_query(blob, query):
            continue
        out.append(
            JobListing(
                id=_id_for("greenhouse", board, str(row.get("id") or abs_url)),
                title=title,
                company=board.replace("-", " ").title(),
                location=loc,
                remote=_is_remote(blob, loc),
                url=abs_url,
                source="ats",
                source_label=SOURCE_CATALOG["ats"]["label"],
                published_at=_parse_ts(row.get("updated_at") or row.get("created_at")),
                tags=["greenhouse", board],
                description=_strip_html(desc)[:3000],
            )
        )
    return out


async def fetch_greenhouse(client: httpx.AsyncClient, query: str = "") -> list[JobListing]:
    boards = get_greenhouse_boards()

    async def one(board: str) -> list[JobListing]:
        return await _fetch_greenhouse_board(client, board, query)

    return await _fetch_boards_batched(boards, one)


async def _fetch_lever_company(client: httpx.AsyncClient, company: str, query: str = "") -> list[JobListing]:
    out: list[JobListing] = []
    url = f"https://api.lever.co/v0/postings/{company}"
    try:
        r = await client.get(url, params={"mode": "json"})
        if r.status_code == 404:
            return out
        r.raise_for_status()
        rows = r.json()
    except Exception as exc:
        logger.warning("lever_company_fail", company=company, error=str(exc)[:120])
        return out
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("text") or "")
        abs_url = str(row.get("hostedUrl") or row.get("applyUrl") or "")
        if not title or not abs_url:
            continue
        cats = row.get("categories") or {}
        loc = ""
        if isinstance(cats, dict):
            loc = str(cats.get("location") or "")
            team = str(cats.get("team") or "")
        else:
            team = ""
        desc = str(row.get("descriptionPlain") or row.get("description") or "")[:3000]
        blob = f"{title} {loc} {team} {desc}"
        if not _match_query(blob, query):
            continue
        out.append(
            JobListing(
                id=_id_for("lever", company, str(row.get("id") or abs_url)),
                title=title,
                company=company.title(),
                location=loc,
                remote=_is_remote(blob, loc),
                url=abs_url,
                source="ats",
                source_label=SOURCE_CATALOG["ats"]["label"],
                published_at=_parse_ts(row.get("createdAt")),
                tags=["lever", company] + ([team] if team else []),
                description=desc[:3000],
            )
        )
    return out


async def fetch_lever(client: httpx.AsyncClient, query: str = "") -> list[JobListing]:
    companies = get_lever_companies()

    async def one(company: str) -> list[JobListing]:
        return await _fetch_lever_company(client, company, query)

    return await _fetch_boards_batched(companies, one)


async def _fetch_ashby_board(client: httpx.AsyncClient, board: str, query: str = "") -> list[JobListing]:
    """Ashby public posting API — fail soft on missing boards / errors."""
    out: list[JobListing] = []
    url = f"https://api.ashbyhq.com/posting-api/job-board/{board}"
    try:
        r = await client.get(url)
        if r.status_code in (404, 403, 400):
            return out
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        logger.warning("ashby_board_fail", board=board, error=str(exc)[:120])
        return out

    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        return out

    company_label = board.replace("-", " ").title()
    for row in jobs:
        if not isinstance(row, dict):
            continue
        # Skip unlisted postings when the flag is present
        if row.get("isListed") is False:
            continue
        title = str(row.get("title") or "")
        abs_url = str(row.get("jobUrl") or row.get("applyUrl") or "")
        if not title or not abs_url:
            continue
        loc = str(row.get("location") or "")
        team = str(row.get("team") or row.get("department") or "")
        desc = str(row.get("descriptionPlain") or "")[:3000]
        if not desc and row.get("descriptionHtml"):
            desc = _strip_html(str(row.get("descriptionHtml") or ""))[:3000]
        workplace = str(row.get("workplaceType") or "")
        remote = (
            bool(row.get("isRemote"))
            or workplace.lower() == "remote"
            or _is_remote(f"{title} {desc} {workplace}", loc)
        )
        blob = f"{title} {loc} {team} {workplace} {desc}"
        if not _match_query(blob, query):
            continue
        tags = ["ashby", board]
        if team:
            tags.append(team)
        if workplace:
            tags.append(workplace.lower())
        out.append(
            JobListing(
                id=_id_for("ashby", board, abs_url, title),
                title=title,
                company=company_label,
                location=loc,
                remote=remote,
                url=abs_url,
                source="ats",
                source_label=SOURCE_CATALOG["ats"]["label"],
                published_at=_parse_ts(row.get("publishedAt")),
                tags=tags,
                description=desc[:3000],
            )
        )
    return out


async def fetch_ashby(client: httpx.AsyncClient, query: str = "") -> list[JobListing]:
    boards = get_ashby_boards()

    async def one(board: str) -> list[JobListing]:
        return await _fetch_ashby_board(client, board, query)

    return await _fetch_boards_batched(boards, one)


async def fetch_ats(client: httpx.AsyncClient, query: str = "") -> list[JobListing]:
    # Providers run concurrently; each provider batches boards internally.
    gh, lv, ab = await asyncio.gather(
        fetch_greenhouse(client, query),
        fetch_lever(client, query),
        fetch_ashby(client, query),
    )
    logger.info("ats_fetch", greenhouse=len(gh), lever=len(lv), ashby=len(ab))
    return gh + lv + ab
