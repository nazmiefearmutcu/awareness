"""SEC EDGAR latest-filings parsing, 8-K item decoding and the filings collector."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from awareness.markets.calendar.filings import (
    EIGHT_K_ITEMS,
    FORMS,
    MATERIAL_8K_ITEMS,
    SecFilingsCollector,
    current_filings_url,
    decode_8k_items,
    parse_sec_current_atom,
)
from awareness.markets.collectors.base import CollectorContext
from awareness.markets.config import MarketsSettings
from awareness.markets.http import Fetcher
from awareness.markets.ref.sec_tickers import cik_to_issuer, parse_company_tickers
from awareness.markets.sources.registry import PublisherRegistry
from awareness.markets.store import MarketsStore

FETCHED = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
FEED_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom"


def _issuers(fixture_bytes: Callable[[str], bytes]) -> dict[int, Any]:
    return cik_to_issuer(parse_company_tickers(json.loads(fixture_bytes("sec_company_tickers.json"))))


def test_item_table_is_the_official_list() -> None:
    assert len(EIGHT_K_ITEMS) == 32
    assert EIGHT_K_ITEMS["9.01"] == "Financial Statements and Exhibits"
    assert EIGHT_K_ITEMS["2.02"] == "Results of Operations and Financial Condition"
    assert MATERIAL_8K_ITEMS <= set(EIGHT_K_ITEMS)
    assert "9.01" not in MATERIAL_8K_ITEMS and "2.02" in MATERIAL_8K_ITEMS


def test_decode_8k_items_orders_dedupes_and_drops_unknown() -> None:
    text = "Item 2.02: Results of Operations ... Item 9.01: Exhibits ... Item 2.02 again ... Item 6.99: Bogus"
    assert decode_8k_items(text) == [
        {"code": "2.02", "label": EIGHT_K_ITEMS["2.02"]},
        {"code": "9.01", "label": EIGHT_K_ITEMS["9.01"]},
    ]


def test_decode_8k_items_handles_html_markup() -> None:
    text = "<b>Filed:</b> 2026-11-06<br />Item 1.01: Entry into a Material Definitive Agreement"
    assert [item["code"] for item in decode_8k_items(text)] == ["1.01"]


def test_atom_fixture_parses_8k_entries(fixture_bytes: Callable[[str], bytes]) -> None:
    events = parse_sec_current_atom(
        fixture_bytes("sec_current_8k.atom"),
        form="8-K",
        fetched_at=FETCHED,
        issuers_by_cik={},
        url=FEED_URL,
    )
    assert len(events) == 10
    first = events[0]
    assert first.type == "filing" and first.status == "filed"
    assert first.company == "Pono Capital Four, Inc."
    assert first.date == date(2026, 11, 6)
    assert first.datetime_utc == datetime(2026, 11, 6, 22, 30, 10, tzinfo=UTC)
    assert first.symbol is None  # CIK 2108164 is not in the tickers fixture
    assert first.fields["cik"] == 2108164
    assert first.fields["accession"] == "0001493152-26-050548"
    assert first.fields["form"] == "8-K"
    assert first.fields["url"].endswith("0001493152-26-050548-index.htm")
    assert [i["code"] for i in first.fields["items"]] == ["1.01", "9.01"]
    assert first.fields["material"] is True
    assert first.sources[0].provider == "sec" and first.sources[0].url == FEED_URL
    assert first.sources[0].fetched_at == FETCHED

    ten_holdings = next(e for e in events if e.company == "TEN Holdings, Inc.")
    assert [i["code"] for i in ten_holdings.fields["items"]] == ["2.02", "9.01"]
    assert ten_holdings.fields["material"] is True

    assert len({e.fields["accession"] for e in events}) == 10
    assert all(len(e.id) == 32 for e in events)
    assert len({e.id for e in events}) == 10


def test_ids_are_stable_across_parses(fixture_bytes: Callable[[str], bytes]) -> None:
    content = fixture_bytes("sec_current_8k.atom")
    kwargs: dict[str, Any] = {"form": "8-K", "fetched_at": FETCHED, "issuers_by_cik": {}, "url": FEED_URL}
    assert [e.id for e in parse_sec_current_atom(content, **kwargs)] == [
        e.id for e in parse_sec_current_atom(content, **kwargs)
    ]


def test_symbol_resolved_through_issuer_map(fixture_bytes: Callable[[str], bytes]) -> None:
    """Re-point the first entry at Apple's CIK (320193, in the tickers fixture) to exercise resolution."""
    content = fixture_bytes("sec_current_8k.atom").replace(b"(0002108164)", b"(0000320193)")
    events = parse_sec_current_atom(
        content, form="8-K", fetched_at=FETCHED, issuers_by_cik=_issuers(fixture_bytes), url=FEED_URL
    )
    apple = events[0]
    assert apple.symbol == "AAPL"
    assert apple.fields["cik"] == 320193
    others = [e for e in events[1:] if e.symbol is None]
    assert len(others) == len(events) - 1


def test_reporting_owner_entries_are_skipped(fixture_bytes: Callable[[str], bytes]) -> None:
    content = fixture_bytes("sec_current_8k.atom").replace(b"(Filer)", b"(Reporting)", 1)
    events = parse_sec_current_atom(content, form="8-K", fetched_at=FETCHED, issuers_by_cik={}, url=FEED_URL)
    assert len(events) == 9
    assert all(e.company != "Pono Capital Four, Inc." for e in events)


def test_duplicate_accessions_are_collapsed(fixture_bytes: Callable[[str], bytes]) -> None:
    content = fixture_bytes("sec_current_8k.atom")
    first_entry = re.search(rb"<entry>.*?</entry>", content, re.DOTALL)
    assert first_entry is not None
    doubled = content.replace(first_entry.group(0), first_entry.group(0) + first_entry.group(0), 1)
    events = parse_sec_current_atom(doubled, form="8-K", fetched_at=FETCHED, issuers_by_cik={}, url=FEED_URL)
    assert len(events) == 10


def test_non_8k_forms_have_no_item_fields(fixture_bytes: Callable[[str], bytes]) -> None:
    events = parse_sec_current_atom(
        fixture_bytes("sec_current_8k.atom"), form="10-Q", fetched_at=FETCHED, issuers_by_cik={}, url=FEED_URL
    )
    assert events and all("items" not in e.fields and "material" not in e.fields for e in events)


def test_material_flag_false_for_non_material_items() -> None:
    atom = b"""<?xml version="1.0" encoding="ISO-8859-1"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry>
<title>8-K - Quiet Co (0000999999) (Filer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/999999/x-index.htm"/>
<summary type="html">&lt;b&gt;Filed:&lt;/b&gt; 2026-11-07 &lt;b&gt;AccNo:&lt;/b&gt; 0000999999-26-000001 &lt;b&gt;Size:&lt;/b&gt; 10 KB
&lt;br&gt;Item 9.01: Financial Statements and Exhibits
</summary>
<updated>2026-11-07T09:00:00-05:00</updated>
<id>urn:tag:sec.gov,2008:accession-number=0000999999-26-000001</id>
</entry>
</feed>"""
    (event,) = parse_sec_current_atom(atom, form="8-K", fetched_at=FETCHED, issuers_by_cik={}, url=FEED_URL)
    assert event.fields["material"] is False
    assert [i["code"] for i in event.fields["items"]] == ["9.01"]
    assert event.datetime_utc == datetime(2026, 11, 7, 14, 0, tzinfo=UTC)


def test_form_urls_are_encoded() -> None:
    assert "type=SC%2013D&" in current_filings_url("SC 13D")
    assert "type=8-K&" in current_filings_url("8-K")
    assert FORMS[0] == "8-K" and "SC 13G" in FORMS


async def _no_sleep(_: float) -> None:
    return None


def test_collector_upserts_and_tolerates_failures(
    tmp_path: Path, store: MarketsStore, fixture_bytes: Callable[[str], bytes]
) -> None:
    settings = MarketsSettings(data_dir=tmp_path / "markets", autostart_runner=False, sentiment_backend="lexicon")
    settings.ref_dir.mkdir(parents=True, exist_ok=True)
    (settings.ref_dir / "company_tickers.json").write_bytes(fixture_bytes("sec_company_tickers.json"))
    atom = fixture_bytes("sec_current_8k.atom")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if "type=8-K&" in url:
            return httpx.Response(200, content=atom)
        return httpx.Response(404, text="missing")

    now = FETCHED

    async def run() -> Any:
        http = Fetcher(
            settings, store, transport=httpx.MockTransport(handler),
            sleep=_no_sleep, host_intervals={"www.sec.gov": 0.0},
        )
        ctx = CollectorContext(
            settings=settings, store=store, http=http, registry=PublisherRegistry([]), now=lambda: now
        )
        try:
            return await SecFilingsCollector().run(ctx)
        finally:
            await http.aclose()

    result = asyncio.run(run())
    assert len(seen) == len(FORMS)
    assert result.items == 10
    stored = store.query_corp(date(2026, 11, 1), date(2026, 11, 30), types=["filing"])
    assert len(stored) == 10
    assert all(e.sources[0].provider == "sec" for e in stored)


def test_collector_raises_when_every_form_fails(tmp_path: Path, store: MarketsStore) -> None:
    settings = MarketsSettings(data_dir=tmp_path / "markets", autostart_runner=False, sentiment_backend="lexicon")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="missing")

    async def run() -> None:
        http = Fetcher(
            settings, store, transport=httpx.MockTransport(handler),
            sleep=_no_sleep, host_intervals={"www.sec.gov": 0.0},
        )
        ctx = CollectorContext(
            settings=settings, store=store, http=http, registry=PublisherRegistry([]), now=lambda: FETCHED
        )
        try:
            await SecFilingsCollector().run(ctx)
        finally:
            await http.aclose()

    with pytest.raises(RuntimeError):
        asyncio.run(run())
