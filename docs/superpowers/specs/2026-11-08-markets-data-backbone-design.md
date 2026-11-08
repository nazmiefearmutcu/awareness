# Awareness Markets — showme's data backbone (design)

Date: 2026-11-08 · Status: approved by owner's standing instruction (autonomous overnight delivery) · Branch: `feat/markets-backbone`

## 1. Why

showme (local trade terminal; Tauri + React UI, Python sidecar) is the shell that FlowMap,
Crocodile and Awareness are folding into. Its text/event data is the weakest layer. A read-only
survey and the truth-audit session's B2 review (showme `docs/audit/function-ledger.md`, branch
`fix/foundation-audit`) found:

- **News** — every RSS headline carries `source: "rss"` (outlet lost), RSS items have `link`
  but no `url`, CN injects Binance/CoinGecko market rows as "news", TOP never leaves the home-SWR
  "warming" envelope (P0), NSE free-text search returns nothing (P0), NALRT matches substrings in
  body text ("by default" trips *credit default*). No quality gate, no syndication collapse, no
  publisher tiering, no author, no corroboration.
- **Sentiment** — SOSC is GDELT tone with a dead FinBERT blend; StockTwits (Welcome gauge
  fallback) now answers a Cloudflare 403; X scraping is fragile and its model is missing; no Fear &
  Greed, no VIX term structure, no social buzz.
- **Economic calendar** — ECO falls through TradingEconomics (key; guest tier discontinued) →
  Finnhub (key) → ForexFactory *this week only*; FF K/M suffix applied twice (P1); `reference=true`
  returns a hard-coded invented schedule. MEET reports an empty forward calendar as `ok`.
- **Company calendar** — no earnings calendar (ERN exists only in a design mock), no IPO
  calendar; DVD **fabricates** two $0.24/$0.23 dividends in its default mode; EVTS is yfinance-only
  and mis-sorts; CACT misses dividends and duplicates splits; holidays depend on an optional lib.

Awareness already is a robust public-text ingestion engine (feeds, robots, dedup, SimHash,
FastAPI, typed records) and `catchem` (same owner) was designed as its finance-analysis
downstream (taxonomy, FinBERT stage, entity/symbol linking, source packs). This design makes
**Awareness the single owner of every text/event/sentiment data responsibility** for showme.

### Success criteria (acceptance)

1. showme's news panes never show an item without publisher name, canonical URL and UTC
   publish time; the default view hides items labelled `low`/`rejected` by the quality gate;
   syndicated copies collapse into one story that lists all corroborating publishers.
2. Every news item carries entities (tickers/crypto/FX/commodities/indices/countries/central
   banks), topics, asset classes, a sentiment score with its model name, a quality score with
   human-readable reasons, and an importance score.
3. Economic calendar covers at least the next 14 days for US/EU/GB/JP/CA/AU/NZ/CH/CN/DE/FR plus
   central-bank meetings (Fed, ECB, BoE, BoJ, TCMB) and public/market holidays, with
   actual/consensus/previous and computed surprise once released, and provenance per field.
4. Company calendar covers earnings (date, BMO/AMC, EPS estimate), dividends (ex/record/pay
   dates, amount), splits, IPOs (priced/upcoming/filed) and SEC filings (8-K items decoded).
5. Market sentiment: CNN Fear & Greed (+7 components), crypto Fear & Greed, VIX/VIX9D/VIX3M term
   structure, Reddit/social buzz (ApeWisdom + Reddit RSS), per-entity news sentiment series.
6. Nothing is ever invented: no demo/model/reference data on any default path; every response
   says which providers fed it and whether they are stale; empty is reported as empty.
7. Runs keyless. Optional keys (Finnhub, FRED) only add coverage.

### Out of scope

Price/quote/order-book data (Crocodile/showme own it), liquidity visualisation (FlowMap),
paywall circumvention, authenticated scraping, X/Twitter scraping, packaging awareness into the
Tauri bundle (dev-mode autostart only), cloud deployment.

## 2. Architecture

```
showme UI panes ──► showme sidecar /api/fn/{CODE} ──► AwarenessClient (httpx) ──HTTP──►  awareness-markets (FastAPI, 127.0.0.1:8086)
                                                                                         │
            MarketsRunner (asyncio, in-process) ── collectors ──► providers (RSS, Nasdaq, FF, BEA, Fed, ECB, BoE, BoJ, SEC, Nager, CNN, alternative.me, CBOE, ApeWisdom, Reddit, GDELT)
                         │                                                                │
                         └──► news pipeline: parse → extract → entities → taxonomy → sentiment → quality → cluster ──► MarketsStore (SQLite WAL + FTS5)
```

- New package `src/awareness/markets/` inside the awareness repo; reuses awareness utilities
  (SimHash, text normalisation, robots, logging) where they fit, adds its own small HTTP layer
  with ETag/Last-Modified caching and per-host pacing.
- **Process model:** one process, `awareness-markets` (`python -m awareness.markets.app`), serves
  the API and runs the collectors (MarketsRunner) so there is exactly one writer. The main
  awareness dashboard (`awareness-api`) also mounts the read-only router (no runner) so the
  existing UI can browse market data.
- **Storage:** `MarketsStore` = SQLite in WAL mode at `<data_dir>/markets/markets.sqlite`
  (`busy_timeout`, `foreign_keys`), FTS5 virtual table over news title/summary/body. SQLite over
  DuckDB because the workload is many small concurrent upserts + concurrent readers; volumes are
  ~10k news/day, trivial for SQLite. Raw provider payloads are not persisted; parsed records keep
  provenance (`ProviderRef`).
- **Boundaries:** collectors only fetch+parse+upsert; NLP units are pure functions over text;
  the API only reads the store; showme only talks HTTP.

## 3. Data model (contracts — `markets/models.py`)

All timestamps are timezone-aware UTC. All models are pydantic v2, `extra="ignore"`.

- `ProviderRef{provider, url?, fetched_at}` — provenance attached to every record.
- `Publisher{id, name, domain, tier 1–5, kind, region, languages, paywalled, fetch_fulltext,
  feeds[FeedSpec]}`; `FeedSpec{id, url, categories, poll_seconds, kind: rss|atom|gnews}`.
  Tiers: 1 official sources + top wires (Fed, ECB, SEC, BEA, Reuters, AP, Bloomberg); 2 major
  financial press (WSJ, FT, CNBC, MarketWatch, Barron's, Nikkei, Economist); 3 quality general /
  regional / specialist (BBC, Guardian, NYT, DW, CoinDesk, AA, Bloomberg HT, Seeking Alpha news);
  4 aggregators, PR wires, opinion platforms (Yahoo syndication, PR Newswire, Business Wire,
  Investing.com); 5 community/social (Reddit).
- `EntityRef{type: equity|crypto|fx|commodity|index|country|central_bank|person, id, name,
  confidence 0–1, mentions, salience 0–1}`. Canonical ids: tickers (`AAPL`), crypto (`BTC`),
  currencies (`USD`, pairs `EURUSD`), commodities (`XAU`, `WTI`, `BRENT`), indices (`SPX`, `NDX`,
  `XU100`), countries ISO-2 (`US`, `EU` for euro area, `TR`), central banks (`FED`, `ECB`,
  `TCMB`).
- `SentimentScore{label positive|negative|neutral, score −1..1, confidence 0–1, model}`.
- `RawNewsItem{url, title, summary?, published_at?, author?, feed_id, publisher_id, categories,
  language?, body_text?, origin_publisher_name?, origin_publisher_domain?}` — collector output.
- `NewsItem` — `RawNewsItem` plus `id` (xxhash of canonical URL), `excerpt`, publisher
  name/domain/tier, `first_seen_at`, `content_type: news|analysis|opinion|press_release|official|
  filing|social`, `entities`, `symbols`, `countries`, `asset_classes`, `topics`, `sentiment`,
  `entity_sentiment{id: SentimentScore}`, `quality_score 0–100`, `quality_label
  premium|standard|low|rejected`, `quality_reasons[]`, `importance 0–100`, `story_id`,
  `is_paywalled`, `word_count`.
- `Story{id, title, lead_item_id, item_ids, publishers, corroboration, first_seen_at,
  last_updated_at, symbols, topics, countries, sentiment?, importance}`.
- `EconEvent{id, country, currency?, title, category?, scheduled_at, all_day, importance 1–3,
  actual?, forecast?, previous?, revised? (display strings), actual_value?, forecast_value?,
  previous_value? (floats in the display unit), unit?, surprise?, surprise_direction
  above|below|inline?, status scheduled|released|revised|cancelled, description?, sources[]}`.
- `CorpEvent{id, type earnings|dividend|split|ipo|filing, symbol?, company, exchange?, date,
  time_of_day bmo|amc|dmh|unknown?, datetime_utc?, status, fields{…type specific…}, sources[]}`.
- `Holiday{id, date, country?, exchange?, name, local_name?, kind public|market_closed|
  early_close, early_close_time?, source}`.
- `IndicatorPoint{indicator, ts, value, label?, meta{}, source}` — market-sentiment series
  (`cnn_fear_greed`, `cnn_fg.<component>`, `crypto_fear_greed`, `vix`, `vix9d`, `vix3m`,
  `social.mentions` with `meta.ticker`, `gdelt.tone` with `meta.query`).
- `ProviderHealth{provider, last_attempt_at?, last_ok_at?, last_error_at?, last_error?,
  consecutive_failures, items_last_ok, expected_interval_s, status ok|degraded|down|never}`;
  status is derived: ok if last_ok within 3× interval, degraded within 12×, else down.

## 4. Collectors (all keyless; verified live 2026-11-08 03:40 TRT)

| Collector | Provider / endpoint | Cadence | Output |
| --- | --- | --- | --- |
| `news.feeds` | ~70 curated publisher RSS/Atom feeds (`configs/markets/publishers.yaml`) | 60–300 s per feed, ETag | NewsItem |
| `news.gnews` | Google News RSS search for watchlist tickers; **only allowlisted publishers (tier ≤ 3)** kept | 15 min | NewsItem |
| `news.reddit` | r/stocks, r/investing, r/wallstreetbets, r/CryptoCurrency `.rss` (social, tier 5) | 10 min | NewsItem(social) |
| `econ.nasdaq` | `api.nasdaq.com/api/calendar/economicevents?date=` for today−3 … today+14 | 30 min; 2 min for today | EconEvent (actual/consensus/previous) |
| `econ.forexfactory` | `nfs.faireconomy.media/ff_calendar_thisweek.json` | 60 min | EconEvent (impact, forecast) |
| `econ.bea` | `apps.bea.gov/API/signup/release_dates.json` | 24 h | EconEvent (official US release times) |
| `econ.central_banks` | Fed FOMC page, ECB MP calendar, BoE MPC dates, BoJ MPM schedule; TCMB curated official 2026–27 list | 24 h | EconEvent(category=central_bank) |
| `holidays.public` | `date.nager.at/api/v3/PublicHolidays/{year}/{cc}` for configured countries | 24 h | Holiday |
| `holidays.exchanges` | `exchange_calendars` (offline) for XNYS, XNAS, XIST, XLON, XETR, XPAR, XTKS, XHKG, XSHG, XTSE, XASX, XSWX | 24 h | Holiday (market_closed / early_close) |
| `corp.nasdaq` | earnings / dividends / splits (per date, today−3 … +21), IPO (per month) | 60 min | CorpEvent |
| `corp.sec_filings` | SEC "current events" Atom per form (8-K, 10-Q, 10-K, S-1, 6-K, SC 13D/G, 4) | 5 min | CorpEvent(filing) (+ NewsItem for 8-K 2.02/5.02/1.01 etc.) |
| `sent.cnn_fng` | `production.dataviz.cnn.io/index/fearandgreed/graphdata` | 30 min | IndicatorPoint (+components) |
| `sent.crypto_fng` | `api.alternative.me/fng/?limit=30` | 60 min | IndicatorPoint |
| `sent.cboe_vix` | CBOE `VIX/VIX9D/VIX3M_History.csv` | 6 h | IndicatorPoint |
| `sent.apewisdom` | `apewisdom.io/api/v1.0/filter/{all-stocks,all-crypto}/page/1..2` | 30 min | IndicatorPoint(social.mentions) |
| `sent.gdelt_tone` | GDELT DOC `timelinetone` for watchlist + countries (rate limited, 1 req/6 s) | 60 min | IndicatorPoint |
| `ref.sec_tickers` | `sec.gov/files/company_tickers.json` | 7 d | entity dictionary cache |

Blocked/unsuitable (verified): TradingEconomics guest (410), StockTwits (Cloudflare), BLS ICS
(403), GlobeNewswire (403), KAP (timeout), ForexFactory next-week (404). SEC requires a
descriptive User-Agent; the default is generic and `AW_SEC_USER_AGENT` lets the owner add a
contact address (we never inject the owner's e-mail).

## 5. News pipeline

1. **Parse** feed entries (feedparser) → `RawNewsItem`; canonicalise URL (strip utm/fbclid,
   resolve Google News redirect domain via `<source url>`), clean HTML from summaries.
2. **Extract** (tier 1–3, non-paywalled, robots-allowed, `fetch_fulltext: true`): fetch the
   article page with trafilatura → body text, author, precise date. Paywalled publishers keep the
   feed summary only. Body text stays local; the API serves an `excerpt` (≤ 400 chars).
3. **Entities** (`nlp/entities.py`): cashtags and `(NASDAQ: AAPL)` patterns (0.99), parenthesised
   tickers (0.95), curated aliases and SEC titles for the top ~1,500 issuers (0.85), ambiguous
   names/tickers (`Target`, `Visa`, `SOL`, `LINK`, `ALL`) need cashtag or context words (stock,
   shares, Inc, CEO, earnings, token, coin) within the sentence; countries with English and
   Turkish names/demonyms; central banks and their chairs; FX pairs, commodities, indices.
   Salience 1.0 when in title, else by mention count. Keep confidence ≥ 0.6.
4. **Taxonomy** (`nlp/taxonomy.py`): rule-based multi-label asset classes, topics (catchem
   reason codes: earnings, guidance, m_and_a, central_bank, inflation, employment,
   growth_recession, regulation, litigation, geopolitics, sanctions_trade, energy, metals,
   crypto_market, funding_liquidity, supply_chain, cyber_outage, product_launch,
   fraud_governance, natural_disaster, ipo, dividends_buybacks, analyst_rating), finance-relevance
   score, content-type hints (Opinion:/Column:/Analysis:, PR wires, official sources).
5. **Sentiment** (`nlp/sentiment.py`): FinBERT (ProsusAI/finbert, cached locally) over title +
   lead (≤ 256 tokens) when `transformers`+`torch` import; otherwise the deterministic finance
   lexicon (phrase-aware, negation window 3, intensifiers). Entity sentiment = mean over
   sentences that mention the entity. The model name is always reported.
6. **Quality gate** (`news/quality.py`): hard rejects → `rejected` with reasons
   (`missing_url`, `missing_title`, `missing_timestamp`, `unknown_publisher`, `stale` > 7 days at
   first sight, `future_timestamp` > 1 day, `non_finance` for general feeds,
   `title_too_short` < 20 chars). Score: tier base (T1 45, T2 38, T3 28, T4 15, T5 5) + depth
   (body ≥ 300 words +20, ≥ 120 +12, summary ≥ 140 chars +8) + author +5 + precise time +3 +
   high-confidence entity +7 + topic +5 + corroboration +5 per extra tier ≤ 3 publisher (max +15);
   penalties: clickbait/promo patterns −25, all-caps −10, opinion −5, PR wire −10 unless the
   issuer is the linked entity, paywalled-without-summary −10. Labels: ≥ 60 premium, 40–59
   standard, 20–39 low, else low; hard reject → rejected. Reasons are stored and served.
7. **Cluster** (`news/cluster.py`): within 36 h, same story when title-shingle Jaccard ≥ 0.5,
   or SimHash(title+summary) Hamming ≤ 10 with at least one shared entity. Lead = lowest tier,
   then earliest. Corroboration = distinct publishers with tier ≤ 3. Re-scores members.
8. **Importance** (0–100): tier/content-type base + topic weight (central_bank 25, inflation 20,
   employment 20, earnings 15, guidance 15, m_and_a 15, geopolitics 15, sanctions_trade 12,
   regulation 10, …) + entity salience (mega-cap/index/major country +10) + corroboration (+5 per
   extra source, max +20). NALRT uses importance ≥ 70 in the last 6 h.

## 6. Calendars

- **Economic merge** (`calendar/economic.py`): event key = country + normalised title (case,
  punctuation, `m/m`→`mom`, `y/y`→`yoy`, provider prefixes removed) + scheduled UTC within ±90
  min. Field precedence: actual/consensus/previous from Nasdaq, impact from ForexFactory,
  official time from BEA/central banks; every contributing provider appears in `sources`.
  Importance: FF High/Medium/Low → 3/2/1; otherwise `configs/markets/econ_importance.yaml`
  (CPI, NFP, GDP, rate decisions, PCE, retail sales, ISM/PMI = 3, …). Values parsed once: `K/M/B/T`
  suffix becomes the unit and the numeric stays in that unit (fixes showme ECO#1), `%` kept as
  unit. Surprise = actual − forecast in the same unit; inline when |surprise| < 1e-9.
- **Central banks:** parsed from official pages where they publish a static schedule (Fed,
  ECB, BoE, BoJ). TCMB's schedule page is not reachable keylessly; the 2026–2027 PPK dates are
  curated in `configs/markets/central_banks.yaml` with the source URL and a `curated: true` flag,
  served with that provenance.
- **Corporate:** Nasdaq earnings rows → `time_of_day` from `time-pre-market` / `time-after-hours`
  / `time-not-supplied`; EPS forecast, # estimates, fiscal quarter, market cap, last-year EPS;
  dividends with ex/record/pay/announce dates and annualised amount; splits with ratio; IPOs from
  priced / upcoming / filed / withdrawn buckets. SEC filings: CIK→ticker via the cached SEC
  ticker map; 8-K item numbers decoded (2.02 Results of Operations, 5.02 officer change, 1.01
  material agreement, 8.01 other events, 7.01 Reg FD, 2.01 acquisition/disposition, 1.03
  bankruptcy, 4.02 non-reliance, …).
- **Holidays:** Nager for public holidays (US, TR, GB, DE, FR, IT, ES, NL, CH, JP, CN, HK, KR,
  IN, AU, NZ, CA, BR, MX, ZA, SA, AE), exchange_calendars for exchange closures and early closes.

## 7. API (`/v1/markets`, JSON)

Envelope: `{"data": …, "meta": {"generated_at", "count", "next_cursor"?, "providers":
[{provider, status, last_ok_at}], "filters": {…}, "notes": [..]}}`.

| Method + path | Purpose |
| --- | --- |
| `GET /news` | `symbols, countries, topics, asset_classes, publishers, content_types, min_quality (default standard), min_importance, since, until, q (FTS), story=true (collapse to story leads), limit ≤ 200, cursor` |
| `GET /news/{id}` | detail incl. excerpt, entities, entity sentiment, quality reasons, story siblings |
| `GET /stories` | clustered stories with publishers + corroboration |
| `GET /sentiment/entity/{id}` | `window (1d/7d/30d), bucket (1h/1d)` → series + summary (mean, count, pos/neg share, momentum z, dispersion) |
| `GET /sentiment/market` | latest CNN F&G (+components, history), crypto F&G, VIX term structure, social buzz top movers |
| `GET /sentiment/social` | ApeWisdom stocks/crypto ranks + Reddit item volume |
| `GET /calendar/economic` | `from, to, countries, min_importance, categories, status` |
| `GET /calendar/corporate` | `from, to, types, symbols` |
| `GET /calendar/holidays` | `from, to, countries, exchanges` |
| `GET /filings` | `symbols, forms, since, limit` |
| `GET /sources` | publisher registry with feed health |
| `GET /health` | provider freshness, store counts, runner state |
| `POST /watchlist` / `GET /watchlist` | symbols that drive gnews/GDELT per-entity collection |

## 8. showme integration

Separate worktree of showme on `feat/awareness-data-backbone` (the truth-audit session keeps
`fix/foundation-audit`; it has agreed not to touch the files below).

- `backend/showme/data_sources/awareness/client.py`: `AwarenessClient` (httpx, 4 s timeout,
  base `SHOWME_AWARENESS_URL` default `http://127.0.0.1:8086`), returns typed results or a
  structured `ProviderUnavailable`; lazy autostart of `awareness-markets` when
  `SHOWME_AWARENESS_AUTOSTART` (default on) and `SHOWME_AWARENESS_HOME` (default
  `~/awareness`) contains a venv; never falls back to invented data.
- Rewired functions (backend + panes): **TOP** (story leads, quality ≥ standard; also fixes the
  SWR warming envelope P0), **NI** (topic news), **CN** (company news by symbol; no market rows),
  **NALRT** (importance ≥ 70), **NSE** (FTS + company-name→ticker resolution; fixes P0),
  **SOSC** (entity sentiment series + social buzz + GDELT tone, honest methodology), **ECO**
  (merged calendar, no invented reference schedule on default path), **MEET** (calendar +
  holidays; empty ≠ ok), **EVTS** (corporate events for a symbol, proper date sorting), **DVD**
  (live dividends; fabricated template removed), **CACT** (dividends+splits+8-Ks deduplicated),
  **TRDH** (exchange holidays/hours from awareness), Welcome gauge (`lib/sentiment-store.ts` →
  CNN F&G). New functions **ERN** (earnings calendar), **IPO** (IPO calendar), **MSENT** (market
  sentiment dashboard).
- Inherited gates: truth-gate ratchet (`tests/_truth_baseline.json` may only go down; new
  functions need real descriptions, specific inputs, every input read, no demo defaults),
  `contracts/pane_params.json` regeneration, frozen data-mode vocabulary (`awareness` sources
  must be classified, not pilled LIVE by accident), test isolation via `SHOWME_HOME`.

## 9. Error handling & honesty rules

- Collector failure → health row updated, previous data kept and served with `stale` flags;
  exponential backoff (cap 30 min) per provider; one failing feed never blocks others.
- HTTP layer: retries with jitter for 429/5xx/timeouts, honours `Retry-After`, per-host
  min-interval, conditional GET (ETag / Last-Modified stored in `kv`).
- No fabricated values anywhere; parsing failures drop the field (and log), never guess.
- API never 500s on empty data; returns `data: []` + provider status + notes.

## 10. Testing

- Unit tests per module against real recorded payloads in `tests/markets/fixtures/` (captured
  2026-11-08), no network.
- Store tests (migrations, upserts idempotent, FTS, filters, cursoring).
- API contract tests with a seeded store (FastAPI TestClient).
- Live smoke (`-m live`, opt-in): each collector once against real providers; asserts schema
  + non-empty where the provider guarantees data.
- showme: adapter tests with `httpx.MockTransport`, function tests, truth gate, pane vitest.
- E2E: start awareness-markets, run showme `/api/fn/{CODE}` for every rewired code, assert every
  news item has publisher/url/published_at and no rejected items appear.

## 11. Configuration

`AW_MARKETS_*` env (pydantic-settings): `DATA_DIR`, `HOST` (127.0.0.1), `PORT` (8086),
`AUTOSTART_RUNNER` (true), `SENTIMENT_BACKEND` (auto|finbert|lexicon), `FULLTEXT` (true),
`MAX_ITEM_AGE_DAYS` (7), `NEWS_RETENTION_DAYS` (120), `WATCHLIST` (comma list),
`HOLIDAY_COUNTRIES`, `EXCHANGES`, `SEC_USER_AGENT`, `FINNHUB_API_KEY` (optional), `API_KEY`
(optional bearer). Static config lives in `configs/markets/*.yaml`.
