# Data quality verdict — 2026-07-09

Automated report: `docs/data_quality_report.json`  
Runner: `python scripts/data_quality_audit.py`

## Scorecard

| Gate | Result | Evidence |
|------|--------|----------|
| Keyword search returns rows | **PASS** | 8/8 queries |
| Search precision@20 | **PASS 99.4%** | token present in title/text |
| Search page exact dups | **PASS 0** | after collapse by `content_hash` |
| Empty/short/junk sample | **PASS** | 0/200 empty title, short, junk |
| Live HN tail fetch | **PASS** | first probe: +15 docs / 19s; re-probe no flood |
| Re-fetch same feed | **PASS** | corpus_delta=0 (no duplicate flood) |
| Historical multi-hash groups | **SOFT WARN** | old syndicated iHeart copies pre-fix |

**HARD: 15/15 pass.**

## What we fixed after the first failed audit

1. **Search collapse** (`duckdb_index.py`): unique by `content_hash` (else title|domain), keep best score.  
2. **Exact-dup / revision skip** (`workers/engine.py`): do not write EXACT_DUP or REVISION to JSONL/Iceberg.  
3. Live audit harness: proper seed YAML for `TailEngine.start`.

## Interpretation

- **Accuracy:** Search keyword hits are highly relevant (~99% precision@20).  
- **Realtime:** Tail can pull live public RSS (HN) into the corpus.  
- **No re-spam:** Immediate re-poll does not grow the corpus with the same docs.  
- **Legacy noise:** Existing multi-hash rows remain on disk (syndication); **search no longer surfaces them as duplicates**. New exact dups are not stored.

## Re-run

```bash
cd /Users/nazmi/awareness_dev
.venv/bin/awareness start --no-tail --port 8085
.venv/bin/python scripts/data_quality_audit.py
```
