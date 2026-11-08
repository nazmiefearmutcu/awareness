# Awareness Markets — the data backbone of showme

`awareness.markets` turns Awareness into the single owner of every text, event and sentiment data
responsibility of the showme trade terminal: quality-gated, **sourced** news clustered into stories,
financial sentiment, economic and corporate calendars, holidays and trading sessions, SEC filings and
market-sentiment indicators. Everything is keyless and every record carries provenance.

Design: [`superpowers/specs/2026-11-08-markets-data-backbone-design.md`](superpowers/specs/2026-11-08-markets-data-backbone-design.md).

## Run

```bash
python -m awareness.markets.app            # or: awareness-markets (after pip install -e .)
# API + collectors on http://127.0.0.1:8086 ; --no-runner serves the API only
```

showme starts it automatically on first use when `SHOWME_AWARENESS_HOME` (default `~/awareness`) has a
virtualenv. Data lives in `data/markets/markets.sqlite` (SQLite WAL + FTS5); service log in
`data/markets/service.log`.

## What is collected (all keyless)

| Area | Providers | Cadence |
| --- | --- | --- |
| News | ~90 curated RSS/Atom feeds from ~70 publishers (tier 1 official/wires … tier 5 community), Google News for the watchlist (only allowlisted tier ≤ 3 origins), Reddit | 2–15 min per feed |
| Economic calendar | Nasdaq economic events (actual/consensus/previous), ForexFactory (impact), BEA official release dates, Fed/ECB/BoE/BoJ meeting pages, TCMB PPK schedule (official 2026 policy document) | 30 min – 24 h |
| Corporate calendar | Nasdaq earnings (BMO/AMC, EPS estimate), dividends, splits, IPOs (priced/upcoming/filed/withdrawn) | 1 h |
| Filings | SEC EDGAR latest filings (8-K items decoded, 10-Q/10-K/S-1/6-K/20-F/SC 13D/G/Form 4) | 5 min |
| Holidays & sessions | Nager.Date public holidays (20 countries), `exchange_calendars` closures, early closes, lunch breaks | daily / offline |
| Market sentiment | CNN Fear & Greed (+7 components), crypto Fear & Greed, CBOE VIX/VIX9D/VIX3M, ApeWisdom social buzz, GDELT tone | 30 min – 6 h |

## News pipeline

parse → canonical URL → optional full text (non-paywalled tier 1–3, robots-respecting) → entities
(tickers, crypto, FX, commodities, indices, countries, central banks, people, BIST; EN + TR) → taxonomy
(asset classes, topics, finance relevance) → FinBERT sentiment (item + per entity; lexicon fallback) →
quality gate (hard rejects + explainable 0–100 score: premium / standard / low / rejected) → story
clustering (syndication collapse, corroboration count) → importance (0–100).

Default views show only **premium** and **standard** items. Every verdict lists its reasons
(`quality_reasons`), so a hidden item is always explainable.

## HTTP API (`/v1/markets`)

Envelope `{"data": …, "meta": {generated_at, count, providers[{provider,status,last_ok_at,last_error}], filters, notes}}`.

| Endpoint | Notes |
| --- | --- |
| `GET /news` | `symbols, countries, topics, asset_classes, publishers, content_types, min_quality, min_importance, since, until, hours, q (FTS), story (collapse), limit, cursor` |
| `GET /news/{id}` | detail + story with all corroborating sources |
| `GET /stories` | clustered stories with lead item |
| `GET /sentiment/entity/{id}` | `window 1d/7d/30d`, `bucket 1h/1d` — series, momentum z, dispersion, headlines, GDELT tone |
| `GET /sentiment/market` | CNN F&G (+components, history), crypto F&G, VIX term structure, social buzz |
| `GET /sentiment/social` | ApeWisdom stocks/crypto + Reddit 24 h volume |
| `GET /calendar/economic` | `from, to, countries, min_importance, categories, status` |
| `GET /calendar/corporate` | `from, to, types (earnings,dividend,split,ipo,filing), symbols` |
| `GET /calendar/holidays` | `from, to, countries, exchanges` |
| `GET /calendar/sessions` | `exchange (MIC), from, to` — open/close/lunch break, early closes |
| `GET /filings` | `symbols, forms, days (1–400), material_only` — with `symbols`, backfills each company's own SEC submissions history first (see below) |
| `GET /sources`, `GET /health` | registry with per-feed health; provider freshness, counts, runner state |
| `GET/POST /watchlist`, `POST /collect/{name}` | watchlist drives Google News / GDELT; manual collector trigger |

**Filing history per symbol.** The EDGAR "latest filings" feed only holds what the service has seen since it started. When `/filings` is called with `symbols`, Awareness also fetches each company's `data.sec.gov/submissions` JSON (keyless, SEC user agent, at most 10 companies per request, sequential) and stores its last 400 days of tracked forms (the `FORMS` list plus `/A` amendments), with 8-K items decoded the same way. Each CIK is refetched at most every 6 hours, and the two sources share event ids so they de-duplicate. Symbols without a CIK, and failed fetches, are reported in `meta.notes`; the health of this backfill is the `corp.sec_submissions` provider. Calls without `symbols` fetch nothing.

## Honesty rules

* Nothing is invented: unknown fields stay null, a failing provider raises and is recorded in health.
* Responses always say which providers fed them and whether they are stale (`meta.providers`, `meta.notes`).
* Empty is reported as empty (200 with notes), never padded with sample rows.

## Configuration (`AW_MARKETS_*`)

`DATA_DIR`, `HOST` (127.0.0.1), `PORT` (8086), `AUTOSTART_RUNNER` (true), `SENTIMENT_BACKEND`
(auto|finbert|lexicon), `FULLTEXT` (true), `MAX_ITEM_AGE_DAYS` (7), `NEWS_RETENTION_DAYS` (120),
`WATCHLIST`, `HOLIDAY_COUNTRIES`, `EXCHANGES`, `SEC_USER_AGENT` (add your contact address here if you want;
the default is a generic descriptive agent), `API_KEY` (optional bearer for POST endpoints). Static
configuration (publishers, taxonomy, entities, importance rules, curated central-bank schedules) lives in
`src/awareness/markets/data/`.

## Tests

```bash
pytest tests/markets -q          # unit + contract + real-component integration (recorded payloads, no network)
```
