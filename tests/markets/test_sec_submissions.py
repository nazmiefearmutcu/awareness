"""Per-company SEC submissions backfill: parsing, on-demand fetch policy and the /filings route."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from awareness.markets.api import MarketsAPI, create_markets_router
from awareness.markets.calendar.filings import parse_sec_current_atom
from awareness.markets.calendar.sec_submissions import (
    SUBMISSIONS_URL,
    decode_items,
    ensure_symbol_filings,
    parse_submissions,
)
from awareness.markets.config import MarketsSettings
from awareness.markets.http import Fetcher
from awareness.markets.models import stable_id
from awareness.markets.ref.sec_tickers import SecIssuer
from awareness.markets.store import MarketsStore

FIXED_NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
FETCHED = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
APPLE = SecIssuer(cik=320193, ticker="AAPL", title="Apple Inc.", rank=0)
SINCE = date(2025, 10, 4)  # FIXED_NOW minus 400 days


async def _no_sleep(_: float) -> None:
    return None


def _settings(tmp_path: Path, fixture_bytes: Callable[[str], bytes]) -> MarketsSettings:
    settings = MarketsSettings(
        data_dir=tmp_path / "markets", autostart_runner=False, sentiment_backend="lexicon"
    )
    settings.ref_dir.mkdir(parents=True, exist_ok=True)
    (settings.ref_dir / "company_tickers.json").write_bytes(fixture_bytes("sec_company_tickers.json"))
    return settings


def _fetcher(
    settings: MarketsSettings, store: MarketsStore, handler: Callable[[httpx.Request], httpx.Response]
) -> Fetcher:
    return Fetcher(
        settings,
        store,
        transport=httpx.MockTransport(handler),
        sleep=_no_sleep,
        host_intervals={"data.sec.gov": 0.0},
    )


def _submissions_handler(payload: bytes, seen: list[str]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url) == SUBMISSIONS_URL.format(cik=320193):
            return httpx.Response(200, content=payload)
        return httpx.Response(404, text="missing")

    return handler


def _parse(fixture_bytes: Callable[[str], bytes], since: date = SINCE) -> list[Any]:
    return parse_submissions(
        fixture_bytes("sec_submissions_handmade.json"),
        issuer=APPLE,
        fetched_at=FETCHED,
        url=SUBMISSIONS_URL.format(cik=320193),
        since=since,
    )


def _by_accession(events: list[Any]) -> dict[str, Any]:
    return {e.fields["accession"]: e for e in events}


# ── parsing ───────────────────────────────────────────────────────────────


def test_forms_kept_amendments_included_and_others_dropped(fixture_bytes: Callable[[str], bytes]) -> None:
    events = _parse(fixture_bytes)
    forms = sorted(e.fields["form"] for e in events)
    # 424B2 is not a tracked form; the 2025-01-15 8-K predates the window.
    assert forms == ["10-Q", "4", "8-K", "8-K", "8-K", "8-K/A"]
    assert "0000320193-25-000001" not in _by_accession(events)
    assert all(e.type == "filing" and e.status == "filed" and e.symbol == "AAPL" for e in events)
    assert all(e.company == "Apple Inc." for e in events)


def test_items_decoded_with_titles_dedupe_and_unknown_codes(fixture_bytes: Callable[[str], bytes]) -> None:
    by_acc = _by_accession(_parse(fixture_bytes))
    earnings = by_acc["0000320193-26-000024"]
    assert earnings.fields["items"] == [
        {"code": "2.02", "label": "Results of Operations and Financial Condition"},
        {"code": "9.01", "label": "Financial Statements and Exhibits"},
    ]
    mixed = by_acc["0000320193-26-000010"]
    assert [i["code"] for i in mixed.fields["items"]] == ["2.02", "7.01", "9.99"]
    assert mixed.fields["items"][2] == {"code": "9.99", "label": "Item 9.99"}


def test_material_flag_matches_atom_rules(fixture_bytes: Callable[[str], bytes]) -> None:
    by_acc = _by_accession(_parse(fixture_bytes))
    assert by_acc["0000320193-26-000024"].fields["material"] is True  # 2.02 is material
    assert by_acc["0000320193-26-000019"].fields["material"] is True  # 8-K/A with 5.02
    assert by_acc["0000320193-26-000011"].fields["material"] is False  # 5.07 is not a material item
    assert "items" not in by_acc["0000320193-26-000020"].fields  # 10-Q carries no item fields
    assert "material" not in by_acc["0000320193-26-000030"].fields  # Form 4 neither


def test_url_party_and_cik_fields(fixture_bytes: Callable[[str], bytes]) -> None:
    earnings = _by_accession(_parse(fixture_bytes))["0000320193-26-000024"]
    assert (
        earnings.fields["url"]
        == "https://www.sec.gov/Archives/edgar/data/320193/000032019326000024/aapl-20261102.htm"
    )
    assert earnings.fields["party"] == "Filer"
    assert earnings.fields["cik"] == 320193
    assert earnings.fields["form"] == "8-K"
    assert earnings.sources[0].provider == "sec.submissions"
    assert earnings.sources[0].url == SUBMISSIONS_URL.format(cik=320193)


def test_datetime_is_utc_and_date_is_filing_date(fixture_bytes: Callable[[str], bytes]) -> None:
    earnings = _by_accession(_parse(fixture_bytes))["0000320193-26-000024"]
    assert earnings.date == date(2026, 11, 2)
    assert earnings.datetime_utc == datetime(2026, 11, 2, 21, 30, 23, tzinfo=UTC)
    assert earnings.datetime_utc.utcoffset() == timedelta(0)


def test_missing_acceptance_time_is_null_not_invented(fixture_bytes: Callable[[str], bytes]) -> None:
    payload = json.loads(fixture_bytes("sec_submissions_handmade.json"))
    payload["filings"]["recent"]["acceptanceDateTime"][0] = ""
    events = parse_submissions(
        json.dumps(payload).encode(), issuer=APPLE, fetched_at=FETCHED, url="u", since=SINCE
    )
    assert _by_accession(events)["0000320193-26-000024"].datetime_utc is None


def test_ids_match_the_atom_parser_for_the_same_accession() -> None:
    accession = "0000320193-26-000024"
    feed = (
        '<?xml version="1.0" encoding="ISO-8859-1"?>\n<feed xmlns="http://www.w3.org/2005/Atom">'
        "<entry><title>8-K - Apple Inc. (0000320193) (Filer)</title>"
        '<link rel="alternate" type="text/html" href="https://www.sec.gov/x-index.htm"/>'
        f'<summary type="html">&lt;b&gt;Filed:&lt;/b&gt; 2026-11-02 &lt;b&gt;AccNo:&lt;/b&gt; {accession}'
        " Item 2.02: Results</summary>"
        "<updated>2026-11-02T17:30:23-04:00</updated>"
        f"<id>urn:tag:sec.gov,2008:accession-number={accession}</id></entry></feed>"
    ).encode("latin-1")
    atom = parse_sec_current_atom(feed, form="8-K", fetched_at=FETCHED, issuers_by_cik={}, url="u")
    assert len(atom) == 1
    assert atom[0].id == stable_id("filing", accession)


def test_decode_items_handles_blank_and_none() -> None:
    assert decode_items(None) == []
    assert decode_items("") == []
    assert decode_items(" 5.02 , ,") == [
        {"code": "5.02", "label": "Departure/Election of Directors or Officers; Compensatory Arrangements"}
    ]


def test_real_aapl_payload_parses(fixture_bytes: Callable[[str], bytes]) -> None:
    content = fixture_bytes("sec_submissions_AAPL.json")
    events = parse_submissions(content, issuer=APPLE, fetched_at=FETCHED, url="u", since=date(2025, 1, 1))
    assert events and all(e.symbol == "AAPL" for e in events)
    eight_k = [e for e in events if e.fields["form"] == "8-K"]
    assert any(e.fields["items"] for e in eight_k)


def test_unexpected_layout_raises_value_error() -> None:
    with pytest.raises(ValueError):
        parse_submissions(b'{"nope": 1}', issuer=APPLE, fetched_at=FETCHED, url="u", since=SINCE)
    with pytest.raises(ValueError):
        parse_submissions(b"not json", issuer=APPLE, fetched_at=FETCHED, url="u", since=SINCE)


# ── ensure_symbol_filings ─────────────────────────────────────────────────


def test_second_call_within_ttl_does_not_refetch(
    tmp_path: Path, store: MarketsStore, fixture_bytes: Callable[[str], bytes]
) -> None:
    settings = _settings(tmp_path, fixture_bytes)
    seen: list[str] = []
    payload = fixture_bytes("sec_submissions_handmade.json")

    async def run(now: datetime) -> list[str]:
        http = _fetcher(settings, store, _submissions_handler(payload, seen))
        try:
            return await ensure_symbol_filings(
                store=store, fetcher=http, settings=settings, symbols=["AAPL"], now=now
            )
        finally:
            await http.aclose()

    assert asyncio.run(run(FIXED_NOW)) == []
    assert seen == [SUBMISSIONS_URL.format(cik=320193)]
    stored = store.query_corp(date(2025, 11, 1), date(2026, 11, 30), types=["filing"], symbols=["AAPL"])
    assert len(stored) == 6  # 8-K x4, 8-K/A, 10-Q, Form 4
    assert asyncio.run(run(FIXED_NOW + timedelta(hours=1))) == []
    assert len(seen) == 1  # still fresh
    asyncio.run(run(FIXED_NOW + timedelta(hours=7)))
    assert len(seen) == 2  # past the 6h ttl


def test_unknown_ticker_yields_a_note_and_no_request(
    tmp_path: Path, store: MarketsStore, fixture_bytes: Callable[[str], bytes]
) -> None:
    settings = _settings(tmp_path, fixture_bytes)
    seen: list[str] = []

    async def run() -> list[str]:
        http = _fetcher(settings, store, _submissions_handler(b"{}", seen))
        try:
            return await ensure_symbol_filings(
                store=store, fetcher=http, settings=settings, symbols=["ZZZZQ"], now=FIXED_NOW
            )
        finally:
            await http.aclose()

    notes = asyncio.run(run())
    assert seen == []
    assert len(notes) == 1 and "ZZZZQ" in notes[0] and "no SEC CIK" in notes[0]


def test_fetch_error_yields_note_and_failure_health(
    tmp_path: Path, store: MarketsStore, fixture_bytes: Callable[[str], bytes]
) -> None:
    settings = _settings(tmp_path, fixture_bytes)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="missing")

    async def run() -> list[str]:
        http = _fetcher(settings, store, handler)
        try:
            return await ensure_symbol_filings(
                store=store, fetcher=http, settings=settings, symbols=["AAPL"], now=FIXED_NOW
            )
        finally:
            await http.aclose()

    notes = asyncio.run(run())
    assert len(notes) == 1 and "AAPL" in notes[0] and "fetch failed" in notes[0]
    health = store.get_health("corp.sec_submissions")
    assert health is not None and health.consecutive_failures == 1 and health.last_error
    assert store.kv_get(f"sec.submissions.fetched:{320193}") is None  # failure leaves no fresh marker


def test_dotted_ticker_is_normalised_and_max_symbols_defers(
    tmp_path: Path, store: MarketsStore, fixture_bytes: Callable[[str], bytes]
) -> None:
    settings = _settings(tmp_path, fixture_bytes)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=b'{"name": "x", "filings": {"recent": {}}}')

    async def run() -> list[str]:
        http = _fetcher(settings, store, handler)
        try:
            return await ensure_symbol_filings(
                store=store,
                fetcher=http,
                settings=settings,
                symbols=["brk.b", "AAPL"],
                now=FIXED_NOW,
                max_symbols=1,
            )
        finally:
            await http.aclose()

    notes = asyncio.run(run())
    assert seen == [SUBMISSIONS_URL.format(cik=1067983)]  # BRK-B, not BRK.B
    assert len(notes) == 1 and "AAPL" in notes[0] and "deferred" in notes[0]


# ── /v1/markets/filings ───────────────────────────────────────────────────


def _api_client(store: MarketsStore, settings: MarketsSettings, fetcher: Fetcher | None) -> TestClient:
    api = MarketsAPI(
        settings=settings,
        get_store=lambda: store,
        get_registry=lambda: None,  # type: ignore[arg-type,return-value]
        get_fetcher=lambda: fetcher,
        now=lambda: FIXED_NOW,
    )
    app = FastAPI()
    app.include_router(create_markets_router(api))
    return TestClient(app)


def test_filings_symbols_backfills_and_returns_rows(
    tmp_path: Path, store: MarketsStore, fixture_bytes: Callable[[str], bytes]
) -> None:
    settings = _settings(tmp_path, fixture_bytes)
    seen: list[str] = []
    fetcher = _fetcher(
        settings, store, _submissions_handler(fixture_bytes("sec_submissions_handmade.json"), seen)
    )
    with _api_client(store, settings, fetcher) as client:
        body = client.get(
            "/v1/markets/filings", params={"symbols": "AAPL", "forms": "8-K", "days": 365}
        ).json()
    assert seen == [SUBMISSIONS_URL.format(cik=320193)]
    rows = body["data"]
    assert [r["fields"]["form"] for r in rows] == ["8-K", "8-K", "8-K"]
    assert [r["date"] for r in rows] == ["2026-11-02", "2026-06-01", "2026-05-01"]  # newest first
    assert rows[0]["fields"]["items"][0]["code"] == "2.02"
    assert body["meta"]["filters"]["days"] == 365
    assert "corp.sec_submissions" in [p["provider"] for p in body["meta"]["providers"]]
    assert not any("backfill" in n for n in body["meta"]["notes"])


def test_filings_without_symbols_fetches_nothing(
    tmp_path: Path, store: MarketsStore, fixture_bytes: Callable[[str], bytes]
) -> None:
    settings = _settings(tmp_path, fixture_bytes)
    seen: list[str] = []
    fetcher = _fetcher(
        settings, store, _submissions_handler(fixture_bytes("sec_submissions_handmade.json"), seen)
    )
    with _api_client(store, settings, fetcher) as client:
        body = client.get("/v1/markets/filings", params={"forms": "8-K"}).json()
    assert seen == []
    assert body["data"] == []


def test_filings_without_fetcher_notes_the_skip(
    tmp_path: Path, store: MarketsStore, fixture_bytes: Callable[[str], bytes]
) -> None:
    settings = _settings(tmp_path, fixture_bytes)
    with _api_client(store, settings, None) as client:
        body = client.get("/v1/markets/filings", params={"symbols": "AAPL", "days": 365}).json()
    assert body["data"] == []
    assert any("no fetcher configured" in n for n in body["meta"]["notes"])


def test_filings_days_limit_is_400(store: MarketsStore, markets_settings: MarketsSettings) -> None:
    with _api_client(store, markets_settings, None) as client:
        assert client.get("/v1/markets/filings", params={"days": 400}).status_code == 200
        assert client.get("/v1/markets/filings", params={"days": 401}).status_code == 422
