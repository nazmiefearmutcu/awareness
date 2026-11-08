# Plan — Awareness Markets backbone for showme (2026-11-08)

Spec: `docs/superpowers/specs/2026-11-08-markets-data-backbone-design.md` (binding).
Execution: subagent-driven development; per-task briefs live in the session's `sdd/` folder and are
summarised here. Branch `feat/markets-backbone` (awareness) and `feat/awareness-data-backbone` (showme).

## Phase A — foundation (done)
- **T0** models, SQLite store, fetcher, health, registry, SEC ticker map, recorded fixtures — `a52f7ea`, `eadda33`.

## Phase B — parallel modules (one worktree each, file-disjoint)
| Task | Scope | Owned files |
| --- | --- | --- |
| T1 | publisher registry (~70 verified feeds), RSS/Atom + Google News parsing, article extraction, Feed/GoogleNews collectors | `data/publishers.yaml`, `news/feeds.py`, `news/extract.py` |
| T2 | entity linker (tickers, crypto, FX, commodities, indices, countries, central banks, people, BIST) + taxonomy | `nlp/entities.py`, `nlp/taxonomy.py`, `data/entities/*.yaml`, `data/taxonomy.yaml` |
| T3 | FinBERT + finance lexicon scorer, entity sentiment, aggregation | `nlp/sentiment.py`, `nlp/lexicon.py`, `sentiment/aggregate.py` |
| T4 | quality gate, importance, story clustering | `news/quality.py`, `news/cluster.py`, `data/quality_rules.yaml` |
| T5 | economic calendar (Nasdaq, ForexFactory, BEA, merge) + central banks | `calendar/economic.py`, `calendar/central_banks.py`, `data/econ_importance.yaml`, `data/central_banks.yaml` |
| T6 | corporate calendar (Nasdaq), SEC filings, holidays | `calendar/corporate.py`, `calendar/filings.py`, `calendar/holidays.py` |
| T7 | market-sentiment indicators + snapshots | `sentiment/indicators.py`, `sentiment/snapshot.py` |

## Phase C — runtime (done in main checkout, `0abaada`)
- **T8** news pipeline, MarketsRunner, `/v1/markets` API, `awareness-markets` app. After Phase B merges:
  integration tests with real components, live smoke run, fixes.

## Phase D — showme integration (worktree `showme-awareness`)
- **S0** `AwarenessClient` + autostart (dispatcher).
- **S1** news functions TOP (incl. SWR warming P0), NI, CN, NALRT, NSE.
- **S2** calendars ECO, MEET, EVTS, DVD, CACT, TRDH + new ERN, IPO.
- **S3** sentiment SOSC + new MSENT + Welcome gauge source.
- Gates: truth-gate ratchet, pane params contract, data-mode vocabulary, isolated tests.

## Phase E — UI
- Design pass (Claude design) for News, Calendar, Earnings/IPO, Sentiment panes; implement in `ui/src/functions/`.

## Phase F — verification & delivery
- Final whole-branch review (Opus fork), one fix wave, live E2E (awareness-markets + showme sidecar),
  owner-facing summary with rulings.
