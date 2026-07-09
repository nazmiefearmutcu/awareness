# Awareness — session changes & UI verification

**Date:** 2026-07-09  
**Branch:** `feat/benchmarks`  
**API tested live at:** `http://127.0.0.1:8085/`

---

## 1. What was wrong (before)

| Symptom | Root cause |
|---------|------------|
| CLI `search` / `browse` returned 0 docs for valid data | Default `--start "30 days ago"` excluded older captures; BM25 IDF prune wiped terms on tiny corpora |
| `init` exited 1 in scripts/CI | Interactive confirm on non-TTY → Aborted |
| `awareness stop` under temp project unloaded system LaunchAgent | Unconditional `com.awareness.api.8085` unload |
| `start` ignored `AW_API_PORT` | Hardcoded default 8085 |
| `tail status` said `running: true` for months-old dead process | No PID tracking / no reconcile |
| Jobs table full of phantom `RUNNING` tails | Crash left job rows dirty |
| Web **Tail** page showed “5 fetching / 350 pending” while stopped | Stale PENDING/RUNNING tasks on cancelled job still fed `/tail/status` |
| `xscraper` tests failed at import | Missing `SessionStore` module |
| Deprecation noise from `tldextract` | `registered_domain` fallback always evaluated |

---

## 2. What we changed (by file)

### Core state (`src/awareness/storage/state.py`)
- `TailRow.pid` + SQLite/Postgres migration
- `set_tail(..., pid=)` defaults to `os.getpid()`
- `get_tail(reconcile=True)` — conditional UPDATE for dead/missing PID (race-safe)
- `abandon_inflight_tasks()` — PENDING/RUNNING → SKIPPED for dead jobs
- `reconcile_orphan_tail_jobs()` — cancels phantom TAIL RUNNING jobs + abandons tasks

### CLI (`src/awareness/cli/main.py`)
- `init`: auto non-interactive when stdin is not a TTY
- `start` / `stop` / `restart` / `dashboard`: port from `AW_API_PORT` via `_default_api_port`
- `stop`: launchd only if plist `WorkingDirectory` **exactly** matches project root (`plistlib`)
- `service install|uninstall --port`: dynamic label/env (`AW_API_PORT`, `AW_PROJECT_ROOT`)
- `_get_api_pid()`: deletes stale `api.pid`
- `search` / `browse`: default start = full corpus (empty lower bound)
- `status`: runs orphan job reconcile
- TUI API spawn writes `api.pid` and uses default port

### API (`src/awareness/api/server.py`)
- Startup: `get_tail(reconcile=True)` + `reconcile_orphan_tail_jobs()`
- `/tail/status`: if tail not live → abandon inflight tasks, hide pending/running, empty `running_tasks`

### Search (`src/awareness/storage/duckdb_index.py`)
- Skip BM25 IDF prune when `N < 20`
- Never drop every query term
- DuckDB `SET TimeZone = 'UTC'` on every connection (shared `_configure_connection`)

### Other
- `util/urls.py`: prefer `top_domain_under_public_suffix` without deprecation path
- `xscraper/store.py`: full async SQLite SessionStore (new)
- `tail/engine.py`: import `JobStatus` (resume path)
- Tests: `tests/unit/test_tail_reconcile.py` (9 tests), IDF test corpus size, CLI deep harness `scripts/deep_cli_test.py`
- Docs: `docs/troubleshooting.md` search/browse empty results section

---

## 3. Live UI test (Chrome DevTools on this machine)

API started with:

```bash
awareness start --no-tail --host 127.0.0.1 --port 8085
```

Screenshots under `docs/ui-test-screenshots/`:

| # | File | Result |
|---|------|--------|
| 1 | `01-dashboard.png` | KPIs: 2302 captures, 1136 hashes, 10 jobs; tail STOPPED; live activity list |
| 2 | `02-captures.png` | 1–30 of 1975 chronological |
| 3 | `03-capture-detail.png` | Reader: title, arxiv URL, full text, provenance, related |
| 4 | `04-jobs.png` | Backfill form + job list shows CANCELLED/COMPLETED (no fake RUNNING) |
| 5 | `05-tail.png` → `05-tail-fixed.png` | **Before:** zombie “5 fetching”; **After fix:** PENDING 0, FETCHING 0, “Tail is stopped.” |
| 6 | `06-settings.png` | health ok, paths, dedup stats |

### UI flows exercised
- Dashboard load + activity stream
- Captures list, BM25 search `neural` → **52 matches**
- Capture detail + related captures
- Jobs list + new backfill form
- Tail panel (before/after stale-task fix)
- Settings
- Command palette (`⌘K`) opens

### Network / console
- All SPA `fetch`/`xhr` observed: **HTTP 200** (`/status`, `/dedup-stats`, `/captures`, `/search`, `/captures/{id}`, `/related`, `/tail/status`)
- Console **errors/warnings: none**

### API spot-check
| Endpoint | Result |
|----------|--------|
| `GET /healthz` | `ok: true` |
| `GET /` SPA | 200 |
| `GET /search?q=news` | rows returned |
| `GET /captures` | total 1975 |
| `GET /tail/status` | running false, running_tasks [] |
| `GET /jobs` | cancelled/completed only |

---

## 4. Tests

```bash
pytest tests/unit/test_tail_reconcile.py  # 9 passed
pytest tests/                             # full suite green (prior runs)
python scripts/deep_cli_test.py           # 74 PASS / 0 FAIL CLI deep suite
```

---

## 5. How to re-verify yourself

```bash
cd /Users/nazmi/awareness_dev
.venv/bin/awareness start --no-tail --port 8085
open http://127.0.0.1:8085/
.venv/bin/awareness tail status   # should be running:false
.venv/bin/awareness stop --port 8085
```

Screenshots: `docs/ui-test-screenshots/`.
