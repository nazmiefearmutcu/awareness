"""Per-company SEC filing history from ``data.sec.gov/submissions`` (on-demand backfill).

The "Latest Filings" Atom collector only sees filings made since the service started. This module
fetches one company's own submissions JSON (``filings.recent``) so a symbol's last year of forms
(8-K item codes in particular) is available at once. Events use the same ``stable_id("filing",
accession)`` scheme as the Atom parser, so the two sources de-duplicate in the store.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from awareness.markets.calendar.filings import EIGHT_K_ITEMS, FORMS, MATERIAL_8K_ITEMS
from awareness.markets.config import MarketsSettings
from awareness.markets.health import HealthTracker
from awareness.markets.http import Fetcher, FetchError
from awareness.markets.models import CorpEvent, ProviderRef, stable_id
from awareness.markets.ref.sec_tickers import SecIssuer, load_issuers, ticker_to_issuer
from awareness.markets.store import MarketsStore

log = logging.getLogger(__name__)

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
PROVIDER = "corp.sec_submissions"
SOURCE = "sec.submissions"
LOOKBACK_DAYS = 400
KV_PREFIX = "sec.submissions.fetched:"
# On-demand provider: it is healthy for weeks without a request, so health uses a long interval
# instead of the 6 h refresh ttl (which made an idle week read as "down").
HEALTH_INTERVAL_S = 7 * 24 * 3600


def _column(recent: dict[str, Any], key: str) -> list[Any]:
    value = recent.get(key)
    return value if isinstance(value, list) else []


def _at(column: list[Any], index: int) -> Any:
    return column[index] if index < len(column) else None


def _base_form(form: str) -> str:
    """``8-K/A`` → ``8-K``; ``SC 13D/A`` → ``SC 13D``."""
    upper = form.strip().upper()
    return upper[:-2] if upper.endswith("/A") else upper


def _wanted_form(form: str) -> bool:
    return _base_form(form) in {f.upper() for f in FORMS}


def decode_items(raw: object) -> list[dict[str, str]]:
    """Submissions ``items`` string (``"2.02,9.01"``) as ``[{code, label}]`` in order, unique.

    Known codes take their official title; an unknown code keeps the code with label ``Item <code>``.
    """
    if not isinstance(raw, str):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for part in raw.split(","):
        code = part.strip()
        if not code or code in seen:
            continue
        seen.add(code)
        out.append({"code": code, "label": EIGHT_K_ITEMS.get(code, f"Item {code}")})
    return out


def _accepted_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _filing_url(cik: int, accession: str, document: object) -> str:
    folder = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/"
    return folder + str(document) if isinstance(document, str) and document else folder


def parse_submissions(
    content: bytes,
    *,
    issuer: SecIssuer,
    fetched_at: datetime,
    url: str,
    since: date,
) -> list[CorpEvent]:
    """Filings in one company's submissions JSON as ``filing`` corporate events.

    Keeps the forms in :data:`FORMS` and their ``/A`` amendments, filed on or after ``since``.
    Raises ``ValueError`` when the payload is not a submissions document.
    """
    payload = json.loads(content)
    if not isinstance(payload, dict) or not isinstance(payload.get("filings"), dict):
        raise ValueError("unexpected SEC submissions layout")
    recent = payload["filings"].get("recent")
    if not isinstance(recent, dict):
        raise ValueError("SEC submissions has no filings.recent")
    name = payload.get("name")
    company = name.strip() if isinstance(name, str) and name.strip() else issuer.title
    provider = ProviderRef(provider=SOURCE, url=url, fetched_at=fetched_at, basis="native")

    forms = _column(recent, "form")
    filed_col = _column(recent, "filingDate")
    accepted_col = _column(recent, "acceptanceDateTime")
    accession_col = _column(recent, "accessionNumber")
    document_col = _column(recent, "primaryDocument")
    items_col = _column(recent, "items")

    events: list[CorpEvent] = []
    seen: set[str] = set()
    for index, raw_form in enumerate(forms):
        if not isinstance(raw_form, str) or not _wanted_form(raw_form):
            continue
        accession = _at(accession_col, index)
        if not isinstance(accession, str) or not accession or accession in seen:
            continue
        try:
            filed = date.fromisoformat(str(_at(filed_col, index)))
        except ValueError:
            log.warning("sec.submissions: invalid filingDate for %s", accession)
            continue
        if filed < since:
            continue
        seen.add(accession)
        form = raw_form.strip()
        fields: dict[str, object] = {
            "form": form,
            "accession": accession,
            "cik": issuer.cik,
            "party": "Filer",
            "url": _filing_url(issuer.cik, accession, _at(document_col, index)),
        }
        if _base_form(form).startswith("8-K"):
            items = decode_items(_at(items_col, index))
            fields["items"] = items
            fields["material"] = any(item["code"] in MATERIAL_8K_ITEMS for item in items)
        events.append(
            CorpEvent(
                id=stable_id("filing", accession),
                type="filing",
                symbol=issuer.ticker,
                company=company,
                date=filed,
                datetime_utc=_accepted_utc(_at(accepted_col, index)),
                status="filed",
                fields=fields,
                sources=[provider],
            )
        )
    return events


def _normalise_ticker(raw: str) -> str:
    return raw.strip().upper().replace(".", "-")


def sibling_tickers(settings: MarketsSettings, symbols: Sequence[str]) -> list[str]:
    """Requested tickers plus every ticker sharing their CIK (GOOG/GOOGL, BRK-A/BRK-B).

    A filing is stored once per accession under one ticker, so a share-class sibling must be
    included in the symbol filter or its filings disappear. Unknown tickers are kept as given.
    """
    requested = [t for t in (_normalise_ticker(s) for s in symbols) if t]
    try:
        issuers = load_issuers(settings.ref_dir)
    except Exception:  # the plain ticker filter still works without the reference file
        return requested
    by_ticker = ticker_to_issuer(issuers)
    ciks = {by_ticker[t].cik for t in requested if t in by_ticker}
    out = list(requested)
    for issuer in issuers:
        if issuer.cik in ciks and issuer.ticker not in out:
            out.append(issuer.ticker)
    return out


def _is_fresh(store: MarketsStore, key: str, now: datetime, ttl: timedelta) -> bool:
    raw = store.kv_get(key)
    if not isinstance(raw, str):
        return False
    try:
        fetched = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=UTC)
    return now - fetched < ttl


async def ensure_symbol_filings(
    *,
    store: MarketsStore,
    fetcher: Fetcher,
    settings: MarketsSettings,
    symbols: Sequence[str],
    now: datetime,
    max_symbols: int = 10,
    ttl: timedelta = timedelta(hours=6),
) -> list[str]:
    """Backfill each symbol's submissions history into the store; returns notes, never raises.

    A CIK fetched less than ``ttl`` ago is not fetched again. At most ``max_symbols`` requests are
    made per call, sequentially, to respect SEC fair-access limits.
    """
    notes: list[str] = []
    issuers = ticker_to_issuer(load_issuers(settings.ref_dir))
    health = HealthTracker(store, now=lambda: now)
    interval_s = HEALTH_INTERVAL_S
    since = now.date() - timedelta(days=LOOKBACK_DAYS)

    requested: list[str] = []
    for raw in symbols:
        ticker = _normalise_ticker(raw)
        if ticker and ticker not in requested:
            requested.append(ticker)

    fetches = 0
    for ticker in requested:
        issuer = issuers.get(ticker)
        if issuer is None:
            notes.append(f"{ticker}: no SEC CIK for this ticker, filing history not backfilled")
            continue
        marker = f"{KV_PREFIX}{issuer.cik}"
        if _is_fresh(store, marker, now, ttl):
            continue
        if fetches >= max_symbols:
            notes.append(f"{ticker}: deferred, at most {max_symbols} symbols are fetched per request")
            continue
        fetches += 1
        url = SUBMISSIONS_URL.format(cik=issuer.cik)
        try:
            result = await fetcher.get(url, profile="sec")
            events = parse_submissions(
                result.content, issuer=issuer, fetched_at=result.fetched_at, url=url, since=since
            )
        except (FetchError, ValueError) as exc:
            health.error(PROVIDER, str(exc), expected_interval_s=interval_s)
            notes.append(f"{ticker}: SEC submissions fetch failed: {exc}")
            log.warning("corp.sec_submissions %s failed: %s", ticker, exc)
            continue
        store.upsert_corp_events(events)
        store.kv_set(marker, now.isoformat())
        health.ok(PROVIDER, items=len(events), expected_interval_s=interval_s)
    return notes
