"""SEC ``company_tickers.json`` → ticker/CIK/title map, cached under ``<data>/ref``.

The SEC file is ordered roughly by market value, so ``rank`` (0-based position)
is a usable proxy for issuer size (the entity linker only auto-aliases the top
issuers by name). Fetched weekly by :class:`SecTickersCollector`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from awareness.markets.collectors.base import CollectorContext, CollectorResult

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
CACHE_NAME = "company_tickers.json"


@dataclass(frozen=True)
class SecIssuer:
    cik: int
    ticker: str
    title: str
    rank: int


def parse_company_tickers(payload: dict[str, Any] | list[Any]) -> list[SecIssuer]:
    """Parse the SEC JSON (``{"0": {"cik_str":…, "ticker":…, "title":…}, …}``).

    Tickers are upper-cased with ``.`` normalised to ``-`` (``BRK.B`` → ``BRK-B``),
    the convention showme and Yahoo use. Duplicate tickers keep the first (largest).
    """
    rows: list[Any]
    if isinstance(payload, dict):
        rows = [payload[k] for k in sorted(payload, key=lambda k: int(k) if str(k).isdigit() else 10**9)]
    else:
        rows = list(payload)
    out: list[SecIssuer] = []
    seen: set[str] = set()
    for rank, row in enumerate(rows):
        try:
            ticker = str(row["ticker"]).strip().upper().replace(".", "-")
            cik = int(row["cik_str"])
            title = str(row["title"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        out.append(SecIssuer(cik=cik, ticker=ticker, title=title, rank=rank))
    return out


def cache_path(ref_dir: Path) -> Path:
    return Path(ref_dir) / CACHE_NAME


def load_issuers(ref_dir: Path) -> list[SecIssuer]:
    """Issuers from the local cache; empty list when the cache does not exist yet."""
    path = cache_path(ref_dir)
    if not path.exists():
        return []
    try:
        return parse_company_tickers(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []


def cik_to_issuer(issuers: list[SecIssuer]) -> dict[int, SecIssuer]:
    out: dict[int, SecIssuer] = {}
    for issuer in issuers:
        out.setdefault(issuer.cik, issuer)
    return out


def ticker_to_issuer(issuers: list[SecIssuer]) -> dict[str, SecIssuer]:
    return {issuer.ticker: issuer for issuer in issuers}


class SecTickersCollector:
    name = "ref.sec_tickers"
    interval_s = 7 * 24 * 3600
    lock_group: str | None = None

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        result = await ctx.http.get(SEC_TICKERS_URL, profile="sec")
        payload = result.json()
        issuers = parse_company_tickers(payload)
        if len(issuers) < 1000:
            raise ValueError(f"SEC ticker file looks truncated ({len(issuers)} issuers)")
        path = cache_path(ctx.settings.ref_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
        ctx.store.kv_set("ref.sec_tickers.fetched_at", datetime.now(UTC).isoformat())
        return CollectorResult(items=len(issuers))
