# Awareness Continuous Development Loop

**Branch:** `loop/continuous-dev`  
**Started:** 2026-07-13  
**Stop condition:** User says stop / `touch .ralph/STOP` / explicit cancel.  
**Method:** subagent-driven-development + TDD + rotation (bug hunt → search → dedup → features).

## Rotation order (repeat forever)

1. **Bug hunt** — broken tests, NameErrors, silent data loss  
2. **Search system** — FTS, ranking, collapse, UX  
3. **Dedup / re-fetch prevention** — URL gate, banding, cross-source keys  
4. **New features / gaps** — product improvements  
5. *(repeat)*

## Completed (Cycle 1+)

| ID | Area | Task | Status |
|----|------|------|--------|
| C1-T1 | search/bug | API `_get_index` singleton | ✅ |
| C1-T2 | search/bug | Index `.jsonl.gz` staging | ✅ |
| C1-T2b | search/bug | Exclude `.tmp` from globs | ✅ |
| C1-T3 | dedup | Unify RSS/GDELT partition keys | ✅ |
| C1-T4 | dedup | Pre-fetch URL seen-gate | ✅ |
| C1-T5 | dedup | 32×4 SimHash banding | ✅ |
| C1-T6 | search | Collapse by parent_doc_or_dup_group | ✅ |
| C1-T7 | bug hunt | Import/schema/cc-wet/gdrive/idf/cli window | ✅ |
| C1-T8 | search | Multi-term prefix OR | ✅ |
| C1-T9 | search | FTS rebuild on content swap | ✅ |
| C1-T10 | search | Order-insensitive FTS + wire `_rerank` | ✅ |
| C1-T11 | search | Empty-result diagnostics | ✅ |
| C1-T12 | api | `/captures?unique=` collapse | ✅ |
| C1-T13 | search | Inclusive end-of-day windows | ✅ |
| C1-T14 | chore | Version 0.2.0 + architecture banding docs | ✅ |

## Next backlog (Cycle 2)

| ID | Area | Task | Status |
|----|------|------|--------|
| C2-T1 | dedup | Optional skip-store for tight NEAR_DUP (Hamming ≤12) | pending |
| C2-T2 | search | SPA mode/fields controls + real mode label | pending |
| C2-T3 | systems | Long-lived DuckDB search connection pool | pending |
| C2-T4 | systems | Incremental FTS (no full rebuild) | pending |
| C2-T5 | dedup | Union-find cluster resolve for near-dups | pending |
| C2-T6 | bug | Broader non-slow unit suite green | pending |

## Rules

- Fresh subagent per task (implementer → review).  
- Commit each completed task on `loop/continuous-dev`.  
- Do not push unless user asks.  
- Do not stop between tasks unless blocked or user stops.
