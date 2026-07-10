# Job Search Quality Upgrade Plan

Goal: Make Awareness Work/LinkedIn job search *actually good* — deeper LinkedIn coverage, richer descriptions, better ranking, faster UX, solid tests.

Constraints:
- Public/guest surface only (no login bypass, no CAPTCHA solve, no proxies)
- Keep UI simple
- Project: `/Volumes/disk 2/home_stray_2026-06-07/awareness_dev`
- Package: `src/awareness/jobsearch/`
- API: `POST /jobsearch/search`, profile endpoints
- UI: Work view in `src/awareness/api/web/`

## Gaps (current)

1. LinkedIn only parses search cards — weak descriptions → ranking underfits LI
2. Single keyword + single location — misses profile breadth
3. No short-TTL cache — every search re-hits LinkedIn (slow + rate risk)
4. ATS is tiny hardcoded list
5. Ranking/diversity is crude
6. UI shows little why-match / description

## Task 1 — LinkedIn depth + fanout + cache

**Files:** `linkedin.py`, new `cache.py`, `engine.py`, tests

- Guest search: fan out queries from `q + titles + top skills` (cap 4 queries)
- Multi-location: fan out up to 3 profile locations (plus empty = worldwide)
- After search, enrich top K (default 25) via guest job detail HTML:
  - `https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}`
  - Extract description text, criteria (seniority, employment type), company
- In-memory + disk cache under `data_dir/jobsearch_cache/` TTL 20 min for search pages, 2h for details
- Polite pacing: 0.35s between LI page fetches; retry once on 429 with 2s sleep
- Tests: parse fixture HTML for search cards + job detail; unit test query fanout

## Task 2 — ATS expansion + config

**Files:** `ats.py`, `configs/jobsearch_boards.yaml`, models/engine load

- Move Greenhouse/Lever board lists to YAML (extend to ~40 GH + ~15 Lever)
- Add Ashby public board endpoint where known
- Filter by query before keeping (already partially there)
- Fail soft per board

## Task 3 — Ranking v2

**Files:** `rank.py`, tests

- Field-weighted score: title×3, company×0.5, location×2, tags×1.5, description×1
- Phrase match bonus for multi-word titles
- Freshness curve continuous (not just buckets)
- Soft remote preference vs hard filter already there
- Diversify: min 30% slots reserved for linkedin when LI source enabled and produced results
- Return clearer `score_reasons`

## Task 4 — API/UI quality

**Files:** `server.py`, `index.html`, `app.js`, `style.css`

- Search response: include `enriched` count, `cache_hit` bool, per-source counts
- Work card: show 2-line description snippet + score reasons chips
- “Deep LinkedIn” is default when linkedin checked (server-side enrich)
- Loading state already exists — show source progress if cheap (optional meta string)

## Task 5 — Live verification

- Script/tests prove: LinkedIn-only search returns ≥10 jobs with real URLs + non-empty description after enrich for ≥5 jobs
- Restart API, curl proof, pytest suite green

## Done when

- LinkedIn results have real descriptions for top results
- Multi-query fanout improves recall
- Tests pass; live LI proof script passes
- UI shows snippets/reasons
