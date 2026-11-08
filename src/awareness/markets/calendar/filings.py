"""SEC EDGAR "latest filings" feed parsing and the 8-K item decoder.

The Atom feed lists one entry per filing. Entries are kept only when the filer
(not a reporting owner) is the subject, de-duplicated by accession number, and
resolved to a ticker through the cached SEC issuer map. An unknown CIK keeps the
event with ``symbol=None``.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote

import feedparser

from awareness.markets.collectors.base import CollectorContext, CollectorResult
from awareness.markets.http import FetchError
from awareness.markets.models import CorpEvent, ProviderRef, stable_id
from awareness.markets.ref.sec_tickers import SecIssuer, cik_to_issuer, load_issuers

log = logging.getLogger(__name__)

SEC_PROVIDER = "sec"
SEC_CURRENT_URL = "https://www.sec.gov/cgi-bin/browse-edgar"

# Official Form 8-K item numbers and titles.
EIGHT_K_ITEMS: dict[str, str] = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "1.04": "Mine Safety - Reporting of Shutdowns and Patterns of Violations",
    "1.05": "Material Cybersecurity Incidents",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition",
    "2.03": "Creation of a Direct Financial Obligation or an Obligation under an Off-Balance Sheet Arrangement",
    "2.04": "Triggering Events That Accelerate or Increase a Direct Financial Obligation",
    "2.05": "Costs Associated with Exit or Disposal Activities",
    "2.06": "Material Impairments",
    "3.01": "Notice of Delisting or Failure to Satisfy a Continued Listing Rule or Standard; Transfer of Listing",
    "3.02": "Unregistered Sales of Equity Securities",
    "3.03": "Material Modification to Rights of Security Holders",
    "4.01": "Changes in Registrant's Certifying Accountant",
    "4.02": "Non-Reliance on Previously Issued Financial Statements",
    "5.01": "Changes in Control of Registrant",
    "5.02": "Departure/Election of Directors or Officers; Compensatory Arrangements",
    "5.03": "Amendments to Articles of Incorporation or Bylaws; Change in Fiscal Year",
    "5.04": "Temporary Suspension of Trading Under Registrant's Employee Benefit Plans",
    "5.05": "Amendments to the Registrant's Code of Ethics, or Waiver of a Provision",
    "5.06": "Change in Shell Company Status",
    "5.07": "Submission of Matters to a Vote of Security Holders",
    "5.08": "Shareholder Director Nominations",
    "6.01": "ABS Informational and Computational Material",
    "6.02": "Change of Servicer or Trustee",
    "6.03": "Change in Credit Enhancement or Other External Support",
    "6.04": "Failure to Make a Required Distribution",
    "6.05": "Securities Act Updating Disclosure",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}

MATERIAL_8K_ITEMS: frozenset[str] = frozenset(
    {"1.01", "1.02", "1.03", "2.01", "2.02", "2.05", "2.06", "3.01", "4.01", "4.02", "5.01", "5.02", "7.01", "8.01"}
)

FORMS: tuple[str, ...] = ("8-K", "10-Q", "10-K", "S-1", "6-K", "20-F", "SC 13D", "SC 13G", "4")

_TAG_RE = re.compile(r"<[^>]+>")
_ITEM_RE = re.compile(r"Item\s+(\d\.\d{2})", re.IGNORECASE)
_TITLE_RE = re.compile(
    r"^(?P<form>.+?) - (?P<company>.+) \((?P<cik>\d+)\) \((?P<role>Filer|Issuer|Subject|Reporting)\)\s*$"
)
_FILED_RE = re.compile(r"Filed:\s*(\d{4}-\d{2}-\d{2})")
_ACCNO_RE = re.compile(r"AccNo:\s*(\d{10}-\d{2}-\d{6})")
_ID_ACCESSION_RE = re.compile(r"accession-number=(\d{10}-\d{2}-\d{6})")


def _plain(text: str | None) -> str:
    """Strip markup and entities from feed text."""
    if not text:
        return ""
    return html.unescape(_TAG_RE.sub(" ", text))


def decode_8k_items(text: str) -> list[dict[str, str]]:
    """Official item codes mentioned in an 8-K summary, in order, unique, with their titles.

    Codes that are not in :data:`EIGHT_K_ITEMS` are ignored rather than labelled.
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in _ITEM_RE.finditer(_plain(text)):
        code = match.group(1)
        if code in seen or code not in EIGHT_K_ITEMS:
            continue
        seen.add(code)
        out.append({"code": code, "label": EIGHT_K_ITEMS[code]})
    return out


def _entry_updated(entry: Any) -> datetime | None:
    """feedparser's ``updated_parsed`` is already normalised to UTC."""
    parsed = entry.get("updated_parsed")
    if parsed is None:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _accession(summary: str, entry_id: object) -> str | None:
    match = _ACCNO_RE.search(summary)
    if match:
        return match.group(1)
    id_match = _ID_ACCESSION_RE.search(str(entry_id or ""))
    return id_match.group(1) if id_match else None


@dataclass(frozen=True)
class _Entry:
    """One usable Atom entry: a filer, issuer, subject or reporting-owner view of a filing."""

    role: str
    form: str
    company: str
    cik: int
    accession: str
    filed: date
    summary: str
    link: str | None
    updated: datetime | None


def _entry_record(entry: Any) -> _Entry | None:
    match = _TITLE_RE.match(_plain(entry.get("title")).strip())
    if match is None:
        return None
    summary = _plain(entry.get("summary"))
    filed_match = _FILED_RE.search(summary)
    accession = _accession(summary, entry.get("id"))
    if filed_match is None or accession is None:
        return None
    try:
        filed = date.fromisoformat(filed_match.group(1))
    except ValueError:  # regex matched but the date is impossible (e.g. 2026-02-30): skip this entry only
        log.warning("corp.sec_filings: invalid filed date in entry %s", accession)
        return None
    return _Entry(
        role=match["role"],
        form=match["form"].strip(),
        company=match["company"].strip(),
        cik=int(match["cik"]),
        accession=accession,
        filed=filed,
        summary=summary,
        link=entry.get("link") or None,
        updated=_entry_updated(entry),
    )


def _wants_issuer_side(form: str) -> bool:
    """Form 4 and Schedule 13D/G are about the issuer or subject company, not the filer."""
    upper = form.upper()
    return upper == "4" or upper.startswith("SC 13D") or upper.startswith("SC 13G")


def _pick(records: list[_Entry], issuer_side: bool) -> list[_Entry]:
    """One entry per accession. Filing forms keep the ``Filer`` view only; issuer-side forms
    keep the ``Issuer``/``Subject`` view and fall back to whatever entry exists."""
    groups: dict[str, list[_Entry]] = {}
    for record in records:
        groups.setdefault(record.accession, []).append(record)
    picked: list[_Entry] = []
    for group in groups.values():
        if issuer_side:
            preferred = [r for r in group if r.role in ("Issuer", "Subject")]
            picked.append(preferred[0] if preferred else group[0])
        else:
            filers = [r for r in group if r.role == "Filer"]
            if filers:
                picked.append(filers[0])
    return picked


def parse_sec_current_atom(
    content: bytes,
    *,
    form: str,
    fetched_at: datetime,
    issuers_by_cik: Mapping[int, SecIssuer],
    url: str,
) -> list[CorpEvent]:
    """Filings from an EDGAR "Latest Filings" Atom feed as ``filing`` corporate events.

    ``form`` is the requested form type; item codes are decoded for 8-K requests only.
    The event id is keyed on the accession number alone, so it does not change when
    the ticker cache appears; the resolved ``symbol`` is an ordinary field.
    """
    feed = feedparser.parse(content)
    provider = ProviderRef(provider=SEC_PROVIDER, url=url, fetched_at=fetched_at)
    is_8k = form.upper().startswith("8-K")
    issuer_side = _wants_issuer_side(form)
    records = [r for r in (_entry_record(entry) for entry in feed.entries) if r is not None]
    events: list[CorpEvent] = []
    for rec in _pick(records, issuer_side):
        issuer = issuers_by_cik.get(rec.cik)
        symbol = issuer.ticker if issuer else None
        fields: dict[str, object] = {
            "form": rec.form,
            "accession": rec.accession,
            "cik": rec.cik,
            "party": rec.role,
            "url": rec.link,
        }
        if is_8k:
            items = decode_8k_items(rec.summary)
            fields["items"] = items
            fields["material"] = any(item["code"] in MATERIAL_8K_ITEMS for item in items)
        events.append(
            CorpEvent(
                id=stable_id("filing", rec.accession),
                type="filing",
                symbol=symbol,
                company=rec.company,
                date=rec.filed,
                datetime_utc=rec.updated,
                status="filed",
                fields=fields,
                sources=[provider],
            )
        )
    return events


def current_filings_url(form: str) -> str:
    """EDGAR "getcurrent" Atom URL for one form type (most recent 40 filings)."""
    return (
        f"{SEC_CURRENT_URL}?action=getcurrent&type={quote(form, safe='')}"
        "&company=&dateb=&owner=include&start=0&count=40&output=atom"
    )


class SecFilingsCollector:
    """Latest EDGAR filings for the forms in :data:`FORMS`, resolved to tickers."""

    name = "corp.sec_filings"
    interval_s = 300
    lock_group: str | None = "corp"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        issuers = cik_to_issuer(load_issuers(ctx.settings.ref_dir))
        events: list[CorpEvent] = []
        errors: list[str] = []
        notes: list[str] = []
        for form in FORMS:
            url = current_filings_url(form)
            try:
                result = await ctx.http.get(url, profile="sec")
                events.extend(
                    parse_sec_current_atom(
                        result.content,
                        form=form,
                        fetched_at=result.fetched_at,
                        issuers_by_cik=issuers,
                        url=url,
                    )
                )
            except (FetchError, ValueError) as exc:
                errors.append(f"{form}: {exc}")
                log.warning("corp.sec_filings %s failed: %s", form, exc)
        if errors and len(errors) == len(FORMS):
            raise RuntimeError(f"all {len(FORMS)} SEC current-filings requests failed; last: {errors[-1]}")
        if errors:
            notes.append(f"{len(errors)} of {len(FORMS)} form requests failed")
        upserted = ctx.store.upsert_corp_events(events)
        return CollectorResult(items=upserted, notes=notes)
