"""EntityLinker tests — realistic English and Turkish headlines, no network."""

from __future__ import annotations

import json
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from awareness.markets.config import MarketsSettings
from awareness.markets.models import EntityRef
from awareness.markets.nlp.entities import EntityLinker
from awareness.markets.ref.sec_tickers import SecIssuer, parse_company_tickers

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def issuers() -> list[SecIssuer]:
    payload = json.loads((FIXTURES / "sec_company_tickers.json").read_text(encoding="utf-8"))
    return parse_company_tickers(payload)


@pytest.fixture(scope="module")
def linker(issuers: list[SecIssuer]) -> EntityLinker:
    return EntityLinker(issuers)


def _find(refs: list[EntityRef], etype: str, eid: str) -> EntityRef | None:
    for ref in refs:
        if ref.type == etype and ref.id == eid:
            return ref
    return None


def _ids(refs: list[EntityRef]) -> set[tuple[str, str]]:
    return {(r.type, r.id) for r in refs}


# ── cashtags, exchange-qualified and bare tickers ────────────────────────


def test_cashtag_known_equity(linker: EntityLinker) -> None:
    refs = linker.link("$AAPL jumps after supplier update")
    ref = _find(refs, "equity", "AAPL")
    assert ref is not None
    assert ref.confidence == pytest.approx(0.99)


def test_cashtag_crypto_symbol(linker: EntityLinker) -> None:
    refs = linker.link("$BTC rallies to new monthly high")
    ref = _find(refs, "crypto", "BTC")
    assert ref is not None
    assert ref.confidence == pytest.approx(0.99)


def test_cashtag_unknown_symbol_is_dropped(linker: EntityLinker) -> None:
    assert _find(linker.link("$QWXZV jumps on rumour"), "equity", "QWXZV") is None


def test_exchange_qualified_ticker_outside_sec_list(linker: EntityLinker) -> None:
    refs = linker.link("Startup (NYSE: ZZZQ) prices IPO above range")
    ref = _find(refs, "equity", "ZZZQ")
    assert ref is not None
    assert ref.confidence == pytest.approx(0.99)


def test_bist_qualified_ticker(linker: EntityLinker) -> None:
    refs = linker.link("Shares of (BIST: THYAO) climb after traffic data")
    assert _find(refs, "equity", "THYAO.IS") is not None


def test_nvidia_earnings_headline_salience_and_ids(linker: EntityLinker) -> None:
    refs = linker.link("Nvidia shares jump after earnings beat; (NASDAQ: NVDA) guidance raised")
    ref = _find(refs, "equity", "NVDA")
    assert ref is not None
    assert ref.salience == pytest.approx(1.0)
    assert ref.confidence >= 0.99
    assert refs[0].id == "NVDA"  # title entities rank first


def test_bare_ticker_needs_context(linker: EntityLinker) -> None:
    assert _find(linker.link("NVDA is listed in a long table of names"), "equity", "NVDA") is None
    assert _find(linker.link("NVDA shares rallied 5% in early trade"), "equity", "NVDA") is not None


def test_stoplisted_words_are_not_tickers(linker: EntityLinker) -> None:
    assert linker.link("I think it is all on the table") == []


# ── curated company names, ambiguity ─────────────────────────────────────


def test_target_without_context_is_not_tgt(linker: EntityLinker) -> None:
    assert _find(linker.link("Target cuts outlook"), "equity", "TGT") is None


def test_target_with_context_is_tgt(linker: EntityLinker) -> None:
    refs = linker.link("Target shares fall as retailer cuts outlook")
    assert _find(refs, "equity", "TGT") is not None


def test_lowercase_gap_and_gold_medal_are_not_entities(linker: EntityLinker) -> None:
    assert _find(linker.link("There is a gap in the market after the gold medal ceremony"),
                 "equity", "GAP") is None
    assert _find(linker.link("There is a gap in the market after the gold medal ceremony"),
                 "commodity", "XAU") is None


def test_lowercase_fed_is_not_the_fed(linker: EntityLinker) -> None:
    assert _find(linker.link("Officials were fed up with delays"), "central_bank", "FED") is None


def test_berkshire_hathaway_name(linker: EntityLinker) -> None:
    assert _find(linker.link("Berkshire Hathaway adds to its stake in insurers"), "equity", "BRK-B")


def test_auto_alias_from_sec_title_resolves(issuers: list[SecIssuer]) -> None:
    refs = EntityLinker(issuers).link("SK hynix posts record quarterly profit")
    assert _find(refs, "equity", "SKHY") is not None


def test_curated_eli_lilly_alias_resolves(issuers: list[SecIssuer]) -> None:
    refs = EntityLinker(issuers).link("Eli Lilly raises 2027 revenue view")
    assert _find(refs, "equity", "LLY") is not None


def test_musk_implies_tsla_at_065(linker: EntityLinker) -> None:
    refs = linker.link("Musk says the next model will ship in spring")
    assert _find(refs, "person", "MUSK") is not None
    tsla = _find(refs, "equity", "TSLA")
    assert tsla is not None and tsla.confidence >= 0.65


# ── FX, currencies, commodities, indices, crypto ─────────────────────────


def test_fx_pair_and_currency_word(linker: EntityLinker) -> None:
    refs = linker.link("EUR/USD slips; dollar firms")
    assert _find(refs, "fx", "EURUSD") is not None
    assert _find(refs, "fx", "USD") is not None


def test_turkish_gold_and_usdtry(linker: EntityLinker) -> None:
    refs = linker.link("Alt\u0131n ons baş\u0131na rekor k\u0131rd\u0131, dolar/TL yükseldi")
    assert _find(refs, "commodity", "XAU") is not None
    assert _find(refs, "fx", "USDTRY") is not None


def test_brent_crude_is_brent_not_wti(linker: EntityLinker) -> None:
    refs = linker.link("Brent crude rises on OPEC+ cut")
    assert _find(refs, "commodity", "BRENT") is not None
    assert _find(refs, "commodity", "WTI") is None


def test_sol_cashtag_versus_sol_price_group(linker: EntityLinker) -> None:
    assert _find(linker.link("$SOL rallies as Solana DeFi activity grows"), "crypto", "SOL")
    assert _find(linker.link("Sol Price Group reports fiscal results"), "crypto", "SOL") is None


def test_crypto_full_name(linker: EntityLinker) -> None:
    assert _find(linker.link("Ethereum staking inflows hit a record"), "crypto", "ETH") is not None


def test_index_names(linker: EntityLinker) -> None:
    refs = linker.link("S&P 500 closes higher as Nasdaq Composite gains")
    assert _find(refs, "index", "SPX") is not None
    assert _find(refs, "index", "IXIC") is not None


def test_nasdaq_exchange_is_not_an_index(linker: EntityLinker) -> None:
    refs = linker.link("The company plans to list shares on Nasdaq next year")
    assert _find(refs, "index", "IXIC") is None


# ── countries, central banks, people, Turkish names ──────────────────────


def test_powell_fed_headline(linker: EntityLinker) -> None:
    refs = linker.link("Fed's Powell signals rate cut as inflation cools")
    assert _find(refs, "central_bank", "FED") is not None
    assert _find(refs, "person", "POWELL") is not None
    us = _find(refs, "country", "US")
    assert us is not None
    assert us.salience <= 0.5  # implied country of a central bank


def test_turkish_central_bank_and_bist_headline(linker: EntityLinker) -> None:
    refs = linker.link("Merkez Bankas\u0131 faizi sabit tuttu, BIST 100 yükseldi; THYAO hisseleri %3 artt\u0131")
    assert _find(refs, "central_bank", "TCMB") is not None
    assert _find(refs, "country", "TR") is not None
    assert _find(refs, "index", "XU100") is not None
    assert _find(refs, "equity", "THYAO.IS") is not None


def test_bist_bare_code_needs_following_context(linker: EntityLinker) -> None:
    assert _find(linker.link("THYAO is one of many names"), "equity", "THYAO.IS") is None


def test_turkish_demonyms_and_names(linker: EntityLinker) -> None:
    refs = linker.link("ABD ve Çin ticaret gerilimi büyüyor")
    assert _find(refs, "country", "US") is not None
    assert _find(refs, "country", "CN") is not None


def test_country_demonym_in_title(linker: EntityLinker) -> None:
    refs = linker.link("Chinese stocks rally as Beijing eases property rules")
    cn = _find(refs, "country", "CN")
    assert cn is not None and cn.confidence == pytest.approx(0.95)


def test_latin_america_is_not_the_us(linker: EntityLinker) -> None:
    assert _find(linker.link("Latin America bonds attract inflows"), "country", "US") is None


# ── contract: dedupe, threshold, ordering ────────────────────────────────


def test_output_is_deduped_thresholded_and_sorted(linker: EntityLinker) -> None:
    refs = linker.link(
        "Apple shares rise; Apple earnings beat. $AAPL and (NASDAQ: AAPL) higher.",
        "Apple said Apple revenue grew. Apple Inc. shares gained.",
    )
    keys = [(r.type, r.id) for r in refs]
    assert len(keys) == len(set(keys))
    assert all(r.confidence >= 0.6 for r in refs)
    saliences = [r.salience for r in refs]
    assert saliences == sorted(saliences, reverse=True)
    aapl = _find(refs, "equity", "AAPL")
    assert aapl is not None and aapl.mentions >= 3


def test_entity_ref_fields_are_valid(linker: EntityLinker) -> None:
    for ref in linker.link("Nvidia rallies as Fed holds rates; EUR/USD steady"):
        assert 0.0 <= ref.salience <= 1.0
        assert 0.0 <= ref.confidence <= 1.0
        assert ref.mentions >= 1
        assert ref.name


def test_celebrity_headline_has_no_finance_entities(linker: EntityLinker) -> None:
    refs = linker.link("Taylor Swift steps out on red carpet at the Oscars")
    assert not [r for r in refs if r.type in {"equity", "crypto", "fx", "commodity", "index"}]


def test_performance_200_headlines_under_one_second(linker: EntityLinker) -> None:
    samples = [
        "Nvidia shares jump after earnings beat; (NASDAQ: NVDA) guidance raised",
        "Fed's Powell signals rate cut as inflation cools across the economy this week",
        "Target shares fall as retailer cuts outlook amid weak consumer demand in the US",
        "Merkez Bankas\u0131 faizi sabit tuttu, BIST 100 yükseldi; THYAO hisseleri %3 artt\u0131",
        "Brent crude rises on OPEC+ cut while the dollar softens against the euro",
        "$BTC rallies as Ethereum ETF inflows accelerate and Solana DeFi grows",
        "Chinese stocks rally as Beijing eases property rules and the yuan firms",
        "Apple, Microsoft and Alphabet lead megacap gains as the Nasdaq Composite rises",
    ] * 25
    assert len(samples) == 200
    start = time.perf_counter()
    for headline in samples:
        linker.link(headline, None)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"200 headlines took {elapsed:.3f}s"


def test_linker_builds_without_issuers() -> None:
    refs = EntityLinker().link("Apple shares rise after earnings")
    assert _find(refs, "equity", "AAPL") is not None


def test_capitalised_fed_up_is_not_the_fed(linker: EntityLinker) -> None:
    assert _find(linker.link("Fed up with delays on the bridge"), "central_bank", "FED") is None


# ── fix round 1: precision regression suite ──────────────────────────────

FALSE_POSITIVES: list[tuple[str, set[tuple[str, str]]]] = [
    # I-1 / I-2 crypto words and symbol collisions
    ("Consumer optimism rises as jobs data beats expectations", {("crypto", "OP")}),
    ("Stacks of cash pile up in Treasury vaults", {("crypto", "STX")}),
    ("Traders say the two sides are in harmony on tariffs", {("crypto", "ONE")}),
    ("Did Apple Just Get a Boost From Democrats? Could Benefit the iPhone Maker",
     {("crypto", "MKR")}),
    ("The DASH pricing plan shares rise", {("crypto", "DASH")}),
    ("HYPE cycle fades as shares fall", {("crypto", "HYPE")}),
    ("Optimism grows as shares of Walmart rise", {("crypto", "OP")}),
    # I-3 time zones and abbreviations
    ("Stocks fell at 9 AM ET as yields jumped", {("equity", "ET")}),
    ("STOCKS RISE AS ET SHARES JUMP", {("equity", "ET")}),
    ("Stocks rally at 4 PM ET as shares of banks rise", {("equity", "PM"), ("equity", "ET")}),
    # I-4 single-word automatic aliases
    ("Southern Europe shares slide as energy costs rise", {("equity", "SO")}),
    ("Booking revenue jumps at Expedia", {("equity", "BKNG")}),
    ("Waters rose along the coast as shares fall", {("equity", "WAT")}),
    ("Progressive lawmakers push new tax plan; shares fall", {("equity", "PGR")}),
    # I-5 Target price and sentence-wide context
    ("Analyst raises Target price on Apple shares", {("equity", "TGT")}),
    ("Target sets $200 price target for Nvidia shares", {("equity", "TGT")}),
    ("Strategy shifts as shares slide for banks", {("equity", "MSTR")}),
    # I-6 currency words that are company names
    ("Sterling Infrastructure shares surge on data center deal", {("fx", "GBP")}),
    ("Sterling infrastructure STRL", {("fx", "GBP")}),
    # I-7 S&P sub-indices and bare CAC
    ("James Hardie Industries Set to Join S&P MidCap 400", {("index", "SPX")}),
    ("S&P SmallCap 600 names new members", {("index", "SPX")}),
    ("Stocks stuck in a rut as CAC payback worsens", {("index", "CAC")}),
    # I-8 Turkish M\u0131s\u0131r (Egypt vs corn)
    ("M\u0131s\u0131r, bölgesel gerilimlerin ekonomik etkilerine karş\u0131 kamu harcamalar\u0131n\u0131 k\u0131s\u0131tlamaya haz\u0131rlan\u0131yor",
     {("country", "EG"), ("commodity", "CORN")}),
    ("M\u0131s\u0131r fiyatlar\u0131 yükseldi, ticaret sürüyor", {("country", "EG")}),
    # I-9 exchange qualifiers
    ("Nasdaq: U.S. stocks climb as yields ease", {("equity", "U-S")}),
    ("NYSE: TODAY rises", {("equity", "TODAY")}),
    # M-1 / M-4 neighbour exclusions
    ("Apple pie prices rise as shares of food firms fall", {("equity", "AAPL")}),
    ("Olive oil prices rise on harvest worries", {("commodity", "WTI")}),
    # M-9 "Fed up"
    ("Fed up with delays: shares fall", {("central_bank", "FED")}),
]


@pytest.mark.parametrize(("headline", "forbidden"), FALSE_POSITIVES)
def test_false_positive_class_is_not_linked(
    linker: EntityLinker, headline: str, forbidden: set[tuple[str, str]],
) -> None:
    got = _ids(linker.link(headline))
    assert not (got & forbidden), f"unexpected {sorted(got & forbidden)} in {headline!r}"


POSITIVE_CONTROLS: list[tuple[str, tuple[str, str]]] = [
    ("Sterling falls against the dollar as BoE holds", ("fx", "GBP")),
    ("M\u0131s\u0131r fiyatlar\u0131 yükseldi, ticaret sürüyor", ("commodity", "CORN")),
    ("Türkiye'nin M\u0131s\u0131r ihracat\u0131 artt\u0131; m\u0131s\u0131r hasad\u0131 rekor", ("commodity", "CORN")),
    ("Berkshire Hathaway adds to its stake in insurers", ("equity", "BRK-B")),
    ("Southern Company raises dividend for shareholders", ("equity", "SO")),
    ("Dividend Roundup: Apple, 3M raise payouts", ("equity", "MMM")),
    ("Ethereum staking inflows hit a record", ("crypto", "ETH")),
    ("S&P 500 closes higher as tech rallies", ("index", "SPX")),
    ("Nasdaq: NVDA rises after earnings beat", ("equity", "NVDA")),
    ("Optimism token rallies as crypto traders return", ("crypto", "OP")),
]


@pytest.mark.parametrize(("headline", "expected"), POSITIVE_CONTROLS)
def test_true_positive_controls(
    linker: EntityLinker, headline: str, expected: tuple[str, str],
) -> None:
    assert expected in _ids(linker.link(headline)), headline


def test_fixture_headlines_have_no_wrong_title_entities(linker: EntityLinker) -> None:
    """Real recorded headlines from the fixtures: the known wrong salience-1.0 hits stay gone."""
    titles: list[str] = []
    for name in ("rss_aa_ekonomi.xml", "rss_prnewswire_fin.xml", "rss_yahoo_aapl.xml"):
        root = ET.parse(FIXTURES / name).getroot()  # noqa: S314 - recorded local fixture
        titles.extend(el.text or "" for el in root.iter("title"))
    atom = ET.parse(FIXTURES / "reddit_stocks_new.atom").getroot()  # noqa: S314 - local fixture
    ns = "{http://www.w3.org/2005/Atom}"
    titles.extend(el.text or "" for el in atom.iter(f"{ns}title"))
    forbidden = {
        "iPhone Maker": ("crypto", "MKR"),
        "S&P MidCap 400": ("index", "SPX"),
        "Sterling infrastructure": ("fx", "GBP"),
    }
    checked = 0
    for title in titles:
        for marker, (etype, eid) in forbidden.items():
            if marker in title:
                checked += 1
                refs = linker.link(title)
                assert not any(r.type == etype and r.id == eid and r.salience >= 1.0 for r in refs), title
    assert checked >= 3


def test_eth_and_brk_cross_check(linker: EntityLinker) -> None:
    assert _find(linker.link("Berkshire Hathaway earnings beat"), "equity", "BRK-B") is not None


def test_from_settings_reads_sec_cache(tmp_path: Path, issuers: list[SecIssuer]) -> None:
    ref = tmp_path / "markets" / "ref"
    ref.mkdir(parents=True)
    (ref / "company_tickers.json").write_text(
        (FIXTURES / "sec_company_tickers.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    settings = MarketsSettings(data_dir=tmp_path / "markets", autostart_runner=False)
    linker = EntityLinker.from_settings(settings)
    assert _find(linker.link("SK hynix posts record quarterly profit"), "equity", "SKHY") is not None
