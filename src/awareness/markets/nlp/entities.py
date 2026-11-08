"""Entity linker: maps headline and body text to canonical market entities.

Precision beats recall: a false ticker on a headline is worse than a missed one, so
bare words only count with supporting context. Recognisers, in scan order per segment:

1. Exchange-qualified tickers ``(NASDAQ: NVDA)``, ``NYSE American: XYZ``, ``(BIST: THYAO)``.
   Unparenthesised forms must also be known SEC or BIST issuers.
2. Cashtags ``$AAPL`` (known SEC/BIST issuers) and ``$BTC`` (known crypto symbols).
3. FX pairs ``EUR/USD``, ``EURUSD``, ``dolar/TL``.
4. Dictionary aliases, longest match first (up to five tokens), from ``data/entities/*.yaml``.
5. Bare upper-case SEC tickers and BIST codes, only in parentheses or near context words.

Each alias candidate is gated by case (capitalised or upper-case surfaces), neighbour words,
optional context words within a token window (or the whole sentence), and exclusion words.
Confidence is attached per hit; entities are de-duplicated per (type, id) and kept at or
above :data:`MIN_CONFIDENCE`. Salience is 1.0 when in the title, otherwise
``min(0.9, 0.3 + 0.15 * mentions)``; implied country entities of central banks are halved.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from awareness.markets.config import PACKAGE_DATA_DIR, MarketsSettings
from awareness.markets.models import EntityRef, EntityType
from awareness.markets.nlp.text import Token, phrase_key, tokenize
from awareness.markets.ref.sec_tickers import SecIssuer, load_issuers, ticker_to_issuer

log = logging.getLogger(__name__)

MIN_CONFIDENCE = 0.6
MAX_NGRAM = 5
DEFAULT_TOP_ISSUERS = 1500
ENTITY_DIR = "entities"
PERSON_LINK_CAP = 0.5  # person-implied company links never outrank an explicit mention
COMPANY_WINDOW = 4

CASHTAG_CONF = 0.99
QUALIFIED_CONF = 0.99
BARE_TICKER_CONF = 0.85
SEC_ALIAS_CONF = 0.8
DEFAULT_ALIAS_CONF = 0.9
AMBIGUOUS_ALIAS_CONF = 0.8
TITLE_COUNTRY_CONF = 0.95
PAIR_CONF = 0.95

# Tickers that are ordinary words or time-zone, finance and org abbreviations.
STOP_TICKERS = frozenset(
    """A I AN ALL ARE BE CAN CEO CFO COO IT ON ONE OR SO AT BY FOR GO HAS NOW OUT SEE TWO KEY
    CAT CAR FAST LOW HUGE PLAY REAL MAIN GOOD FUN TRUE OPEN EDIT BIG NEXT LIFE USA AI EV ETF
    IPO GDP CPI PMI PPI PCE FOMC ECB SEC FDA US UK EU UN NATO IMF OPEC NYSE NASDAQ AMEX BIST
    EPS WTI FED BOE BOJ PBOC TCMB ABD AB BUY SELL HOLD ALSO NEW OLD TOP BEST UP DOWN USD EUR
    GBP JPY TRY CNY IN OF TO AS IS IF NO YES MY ME WE HE SHE HIM HER HIS OUR YOU ANY NOT BUT
    WAS HAD DAY PAY TAX YEAR ONLY MORE MOST SOME EACH SAYS SAID
    ET EST EDT PT PST PDT CT CST CDT MT MST MDT GMT UTC CET CEST BST JST IST AM PM PE
    YOY QOQ MOM ATH ATL ROE ROI EBITDA FCF DCF IRS LLC AUM NAV OTC FX ER TV ESG ADR SA NA OK
    PR HQ VP MD DEX NFT""".split()
)

# Turkish agency abbreviation: "AA" followed by these words is Anadolu Ajansı, never American Airlines.
AGENCY_NEXT_WORDS = frozenset({"anadolu", "muhabiri", "muhabir"})
ENGLISH_CODES = frozenset({"en", ""})

BARE_CONTEXT = frozenset(
    {"stock", "stocks", "shares", "share", "ticker", "nyse", "nasdaq", "earnings", "eps",
     "guidance", "rallied", "slumped", "fell", "rose", "jumped", "plunged", "soared"}
)
BIST_CONTEXT = frozenset({"hisse", "hisseleri", "hisselerinde", "pay", "paylari", "bist", "borsa"})
CRYPTO_CONTEXT = frozenset(
    {"crypto", "token", "tokens", "coin", "coins", "blockchain", "defi", "etf", "stablecoin",
     "altcoin", "btc", "eth", "bitcoin", "ethereum", "cryptocurrency", "kripto"}
)
DEFAULT_COMPANY_CONTEXT = frozenset(
    {"shares", "stock", "stocks", "inc", "corp", "ceo", "earnings", "company", "retailer",
     "bank", "brand"}
)

_QUALIFIED_RE = re.compile(
    r"\(\s*(?P<ex1>NASDAQ|Nasdaq|NYSE American|NYSE Arca|NYSE|AMEX|BIST)\s*:\s*"
    r"(?P<t1>[A-Z]{1,5}(?:[.\-][A-Z]{1,2})?)\s*\)"
    r"|\b(?P<ex2>NASDAQ|Nasdaq|NYSE American|NYSE Arca|NYSE|AMEX|BIST)\s*:\s*"
    r"(?P<t2>[A-Z]{1,5}(?:[.\-][A-Z]{1,2})?)\b"
)
_TICKER_RE = re.compile(r"[A-Z][A-Z0-9]*(?:[.\-][A-Z0-9]+)?")
_SUFFIX_WORDS = frozenset(
    {"inc", "corp", "corporation", "co", "company", "ltd", "limited", "plc", "holdings",
     "holding", "group", "n.v", "nv", "s.a", "sa", "ag", "se", "llc", "lp", "&", "de", "new"}
)


def is_foreign_language(language: str | None) -> bool:
    """True when a language hint is given and is not English (e.g. ``tr``)."""
    if not language:
        return False
    return language.strip().lower().replace("_", "-").split("-")[0] not in ENGLISH_CODES


@dataclass(frozen=True, slots=True)
class _Cand:
    """One alias candidate and the gates that decide whether a token span matches it."""

    etype: EntityType
    eid: str
    name: str
    conf: float
    capital: bool = False
    upper: bool = False
    context: frozenset[str] | None = None
    require_context: bool = False
    start_needs_context: bool = False
    window: int | None = None  # None: whole sentence; N: N tokens either side
    require_next: frozenset[str] = frozenset()
    exclude_next: frozenset[str] = frozenset()
    exclude_prev: frozenset[str] = frozenset()
    exclude_context: frozenset[str] = frozenset()
    next_not_capital: bool = False
    dampen: bool = False
    cap: float | None = None  # salience ceiling (person-implied links)
    person_implied: bool = False
    unless: frozenset[str] = frozenset()  # title words that cancel a person-implied link
    links: tuple[_Cand, ...] = ()
    auto: bool = False  # generated from an SEC issuer title (disabled for non-English text)


@dataclass(slots=True)
class _Acc:
    """Accumulated hits for one (type, id) entity."""

    etype: EntityType
    eid: str
    name: str
    conf: float
    dampen: bool
    title: bool = False
    mentions: int = 0
    text_mentions: int = 0
    cap: float | None = None
    person_only: bool = True


class _Sink:
    def __init__(self) -> None:
        self.acc: dict[tuple[str, str], _Acc] = {}
        self.item = ""  # folded title+body token phrase, space-padded
        self.foreign = False  # non-English text: bare tickers and SEC auto-aliases are off

    def add(
        self, etype: EntityType, eid: str, name: str, conf: float, *, in_title: bool,
        dampen: bool = False, cap: float | None = None, person_implied: bool = False,
    ) -> None:
        key = (etype, eid)
        acc = self.acc.get(key)
        if acc is None:
            acc = _Acc(etype, eid, name, conf, dampen, person_only=person_implied)
            self.acc[key] = acc
        else:
            acc.conf = max(acc.conf, conf)
            acc.dampen = acc.dampen and dampen
            acc.person_only = acc.person_only and person_implied
        if cap is not None:
            acc.cap = cap if acc.cap is None else min(acc.cap, cap)
        acc.mentions += 1
        if in_title:
            acc.title = True
        else:
            acc.text_mentions += 1


def _load_yaml(config_dir: Path, name: str) -> dict[str, Any]:
    """Read an entity dictionary from ``config_dir/entities`` or the package data."""
    for root in (Path(config_dir), PACKAGE_DATA_DIR):
        path = root / ENTITY_DIR / name
        if path.exists():
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raise FileNotFoundError(f"entity dictionary {name} not found")


def _alias_specs(entries: list[Any] | None) -> list[dict[str, Any]]:
    """Normalise alias entries (strings or mappings) into mappings with ``text``."""
    out: list[dict[str, Any]] = []
    for item in entries or []:
        if isinstance(item, str):
            out.append({"text": item})
        elif isinstance(item, dict) and item.get("text"):
            out.append(item)
    return out


def _frozen(values: list[Any] | None) -> frozenset[str]:
    return frozenset(phrase_key(str(v)) for v in values or [])


def _make_cand(
    etype: EntityType,
    eid: str,
    name: str,
    spec: dict[str, Any],
    *,
    default_conf: float,
    default_capital: bool,
    default_context: frozenset[str] | None = None,
    default_window: int | None = None,
) -> tuple[str, _Cand]:
    """Build the lookup key and candidate for one alias spec from a YAML dictionary."""
    text = str(spec["text"])
    ambiguous = bool(spec.get("ambiguous", False))
    context = _frozen(spec["context"]) if "context" in spec else default_context
    if ambiguous and context is None:
        context = DEFAULT_COMPANY_CONTEXT
    letters = [ch for ch in text if ch.isalpha()]
    cand = _Cand(
        etype=etype,
        eid=eid,
        name=name,
        conf=float(spec.get("conf", AMBIGUOUS_ALIAS_CONF if ambiguous else default_conf)),
        capital=bool(spec.get("capital", default_capital)),
        upper=len(letters) >= 2 and text == text.upper(),
        context=context,
        require_context=ambiguous,
        start_needs_context=bool(spec.get("start_needs_context", False)),
        window=spec.get("window", default_window),
        require_next=_frozen(spec.get("require_next")),
        exclude_next=_frozen(spec.get("exclude_next")),
        exclude_prev=_frozen(spec.get("exclude_prev")),
        exclude_context=_frozen(spec.get("exclude_context")),
        next_not_capital=bool(spec.get("next_not_capital", False)),
        dampen=bool(spec.get("dampen", False)),
    )
    return phrase_key(text), cand


def _item_phrase(title: str, text: str | None) -> str:
    """Space-padded folded token phrase of the whole item, for whole-word checks."""
    keys = [tok.key for tok in tokenize(title)]
    if text:
        keys.extend(tok.key for tok in tokenize(text))
    return " " + " ".join(keys) + " "


def _clean_title(title: str) -> list[str]:
    """Drop share-class and state markers, then trailing corporate suffixes."""
    cleaned = re.sub(r"/[A-Za-z]{2,4}/", " ", title)
    cleaned = re.sub(r"\bClass\s+[A-Za-z]\b", " ", cleaned, flags=re.IGNORECASE)
    words = [w.strip(".,") for w in cleaned.split()]
    words = [w for w in words if w]
    while words and phrase_key(words[-1]) in _SUFFIX_WORDS:
        words.pop()
    if words and words[0].lower() == "the":
        words.pop(0)
    return words


class EntityLinker:
    """Links text to :class:`EntityRef` records. Build once; :meth:`link` is pure."""

    def __init__(
        self,
        issuers: list[SecIssuer] | None = None,
        *,
        config_dir: Path | None = None,
        top_issuers_by_name: int = DEFAULT_TOP_ISSUERS,
    ) -> None:
        base = Path(config_dir) if config_dir is not None else PACKAGE_DATA_DIR
        self._aliases: dict[str, list[_Cand]] = {}
        self._names: dict[tuple[str, str], str] = {}
        self._sec: dict[str, SecIssuer] = ticker_to_issuer(issuers or [])
        self._bist: dict[str, str] = {}
        self._crypto: dict[str, str] = {}
        self._currency_codes: set[str] = set()
        self._currency_names: dict[str, str] = {}
        self._max_len = 1
        self._load_dictionaries(base)
        self._add_auto_aliases(issuers or [], top_issuers_by_name)

    @classmethod
    def from_settings(cls, settings: MarketsSettings) -> EntityLinker:
        """Build from the SEC cache under the settings' ref dir and its config dir."""
        return cls(load_issuers(settings.ref_dir), config_dir=settings.config_dir)

    # ── index construction ────────────────────────────────────────────────

    def _put(self, key: str, cand: _Cand) -> None:
        if not key:
            return
        self._aliases.setdefault(key, []).append(cand)
        self._max_len = max(self._max_len, len(key.split(" ")))

    def _load_dictionaries(self, base: Path) -> None:
        self._load_companies(base)
        self._load_crypto(base)
        self._load_fx(base)
        self._load_commodities(base)
        self._load_indices(base)
        self._load_countries(base)
        self._load_central_banks(base)
        self._load_people(base)
        self._load_bist(base)

    def _load_companies(self, base: Path) -> None:
        for row in _load_yaml(base, "company_aliases.yaml").get("companies", []):
            eid, name = str(row["id"]), str(row.get("name", row["id"]))
            self._names[("equity", eid)] = name
            row_context = _frozen(row.get("context")) or None
            for spec in _alias_specs(row.get("aliases")):
                key, cand = _make_cand(
                    "equity", eid, name, spec, default_conf=DEFAULT_ALIAS_CONF,
                    default_capital=True, default_context=row_context,
                    default_window=COMPANY_WINDOW,
                )
                self._put(key, cand)

    def _load_crypto(self, base: Path) -> None:
        for coin in _load_yaml(base, "crypto.yaml").get("coins", []):
            symbol = str(coin["symbol"]).upper()
            names = [str(n) for n in coin.get("names", [])] or [symbol]
            self._crypto[symbol] = names[0]
            self._names[("crypto", symbol)] = names[0]
            sym_context = CRYPTO_CONTEXT if coin.get("ambiguous_symbol") else None
            self._put(
                phrase_key(symbol),
                _Cand("crypto", symbol, names[0], 0.85, upper=True,
                      context=sym_context, require_context=bool(coin.get("ambiguous_symbol"))),
            )
            for name in names:
                key, cand = _make_cand(
                    "crypto", symbol, names[0],
                    {"text": name, "ambiguous": bool(coin.get("ambiguous_names", False))},
                    default_conf=DEFAULT_ALIAS_CONF, default_capital=True,
                    default_context=CRYPTO_CONTEXT,
                )
                self._put(key, cand)

    def _load_fx(self, base: Path) -> None:
        currencies = _load_yaml(base, "fx.yaml").get("currencies", {})
        self._currency_codes = {str(code).upper() for code in currencies}
        for raw_code, body in currencies.items():
            code = str(raw_code).upper()
            self._put(phrase_key(code), _Cand("fx", code, code, 0.9, upper=True))
            self._currency_names[phrase_key(code)] = code
            for spec in _alias_specs((body or {}).get("aliases")):
                key, cand = _make_cand(
                    "fx", code, code, spec, default_conf=0.85, default_capital=False,
                )
                self._put(key, cand)
                self._currency_names[key] = code

    def _load_commodities(self, base: Path) -> None:
        for row in _load_yaml(base, "commodities.yaml").get("commodities", []):
            eid, name = str(row["id"]), str(row.get("name", row["id"]))
            self._names[("commodity", eid)] = name
            for spec in _alias_specs(row.get("aliases")):
                key, cand = _make_cand(
                    "commodity", eid, name, spec, default_conf=0.85, default_capital=False,
                )
                self._put(key, cand)

    def _load_indices(self, base: Path) -> None:
        for row in _load_yaml(base, "indices.yaml").get("indices", []):
            eid, name = str(row["id"]), str(row.get("name", row["id"]))
            self._names[("index", eid)] = name
            for spec in _alias_specs(row.get("aliases")):
                key, cand = _make_cand(
                    "index", eid, name, spec, default_conf=0.9, default_capital=False,
                )
                self._put(key, cand)

    def _load_countries(self, base: Path) -> None:
        for row in _load_yaml(base, "countries.yaml").get("countries", []):
            eid = str(row["id"])
            self._names[("country", eid)] = eid
            for spec in _alias_specs(row.get("aliases")):
                key, cand = _make_cand(
                    "country", eid, eid, spec, default_conf=0.85, default_capital=True,
                )
                self._put(key, cand)

    def _load_central_banks(self, base: Path) -> None:
        for row in _load_yaml(base, "central_banks.yaml").get("banks", []):
            eid = str(row["id"])
            country = str(row["country"]).upper()
            self._names[("central_bank", eid)] = eid
            link = _Cand("country", country, country, 0.7, dampen=True)
            for spec in _alias_specs(row.get("aliases")):
                key, cand = _make_cand(
                    "central_bank", eid, eid, spec, default_conf=0.95, default_capital=True,
                )
                self._put(key, replace(cand, links=(link,)))

    def _load_people(self, base: Path) -> None:
        for row in _load_yaml(base, "people.yaml").get("people", []):
            eid, name = str(row["id"]), str(row.get("name", row["id"]))
            self._names[("person", eid)] = name
            unless = _frozen(row.get("unless_mention"))
            links = tuple(
                _Cand(str(link["type"]), str(link["id"]), str(link["id"]),  # type: ignore[arg-type]
                      float(link["conf"]),
                      cap=PERSON_LINK_CAP if link["type"] == "equity" else None,
                      person_implied=link["type"] == "equity", unless=unless)
                for link in row.get("links", [])
            )
            for spec in _alias_specs(row.get("aliases")):
                key, cand = _make_cand(
                    "person", eid, name, spec, default_conf=0.85, default_capital=True,
                )
                self._put(key, replace(cand, links=links))

    def _load_bist(self, base: Path) -> None:
        for row in _load_yaml(base, "bist.yaml").get("issuers", []):
            code = str(row["code"]).upper()
            eid = f"{code}.IS"
            name = str(row.get("name", code))
            self._bist[code] = eid
            self._names[("equity", eid)] = name
            for spec in _alias_specs([name, *(row.get("aliases") or [])]):
                key, cand = _make_cand(
                    "equity", eid, name, spec, default_conf=DEFAULT_ALIAS_CONF,
                    default_capital=True,
                )
                self._put(key, cand)

    def _add_auto_aliases(self, issuers: list[SecIssuer], top: int) -> None:
        """Alias the largest SEC issuers by cleaned multi-word title (curated keys win).

        Single-word titles are never auto-aliased: they are overwhelmingly ordinary English
        words (Southern, Booking, Waters, Progressive), and precision comes first.
        """
        for issuer in sorted(issuers, key=lambda i: i.rank)[:top]:
            words = _clean_title(issuer.title)
            if len(words) < 2:
                continue
            key = phrase_key(" ".join(words))
            if not key or key in self._aliases:
                continue
            display = self._names.get(("equity", issuer.ticker), issuer.title)
            self._put(key, _Cand("equity", issuer.ticker, display, SEC_ALIAS_CONF, capital=True,
                                 auto=True))

    # ── public API ────────────────────────────────────────────────────────

    def link(self, title: str, text: str | None = None, *, language: str | None = None) -> list[EntityRef]:
        """Return de-duplicated entities for a headline and optional body.

        ``language`` is a hint: both English and Turkish dictionaries are always scanned, but for
        non-English text only cashtags, exchange-qualified tickers, BIST codes and curated aliases
        count. Bare upper-case US tickers and SEC auto-aliases are off, so "AA" (Anadolu Ajansı),
        "SSB", "MW" or "KDP" in Turkish copy are not American issuers.
        """
        sink = _Sink()
        sink.foreign = is_foreign_language(language)
        sink.item = _item_phrase(title or "", text)
        self._scan(title or "", True, sink)
        if text:
            self._scan(text, False, sink)
        explicit_equities = {
            acc.eid for acc in sink.acc.values() if acc.etype == "equity" and not acc.person_only
        }
        out: list[EntityRef] = []
        for acc in sink.acc.values():
            if acc.conf < MIN_CONFIDENCE:
                continue
            if acc.person_only and explicit_equities - {acc.eid}:
                continue  # the item names a different company explicitly
            base = 1.0 if acc.title else min(0.9, 0.3 + 0.15 * acc.text_mentions)
            if acc.cap is not None and acc.person_only:
                base = min(base, acc.cap)
            salience = base * 0.5 if acc.dampen else base
            out.append(
                EntityRef(
                    type=acc.etype,
                    id=acc.eid,
                    name=self._names.get((acc.etype, acc.eid), acc.name),
                    confidence=round(acc.conf, 3),
                    mentions=acc.mentions,
                    salience=round(salience, 3),
                )
            )
        out.sort(key=lambda r: (-r.salience, -r.confidence, r.id))
        return out

    # ── scanning ──────────────────────────────────────────────────────────

    def _scan(self, text: str, in_title: bool, sink: _Sink) -> None:
        if not text:
            return
        claimed = self._scan_qualified(text, in_title, sink)
        tokens = tokenize(text)
        blocked = {i for i, tok in enumerate(tokens) if _inside(tok, claimed)}
        sent_ranges = _sentence_ranges(tokens)
        i = 0
        n = len(tokens)
        while i < n:
            if i in blocked:
                i += 1
                continue
            tok = tokens[i]
            if tok.raw.startswith("$"):
                self._cashtag(tok, in_title, sink)
                i += 1
                continue
            if self._pair(tok, in_title, sink):
                i += 1
                continue
            consumed = self._match_alias(tokens, i, blocked, sent_ranges, in_title, sink)
            if consumed:
                i += consumed
                continue
            self._bare_ticker(tokens, i, text, in_title, sink)
            i += 1

    def _scan_qualified(self, text: str, in_title: bool, sink: _Sink) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        for match in _QUALIFIED_RE.finditer(text):
            parenthesised = match.group("t1") is not None
            exchange = match.group("ex1") or match.group("ex2")
            raw_ticker = match.group("t1") or match.group("t2")
            if raw_ticker.endswith(".") or (
                "." in raw_ticker and text[match.end(): match.end() + 1] == "."
            ):
                continue
            ticker = raw_ticker.replace(".", "-")
            if ticker in STOP_TICKERS:
                continue
            if exchange == "BIST":
                eid = self._bist.get(ticker, f"{ticker}.IS")
            else:
                eid = ticker
                if not parenthesised and ticker not in self._sec:
                    continue
            name = self._names.get(("equity", eid), eid)
            sink.add("equity", eid, name, QUALIFIED_CONF, in_title=in_title)
            spans.append(match.span())
        return spans

    def _cashtag(self, tok: Token, in_title: bool, sink: _Sink) -> None:
        symbol = tok.raw.rstrip(".")[1:].upper().replace(".", "-")
        if not symbol or not symbol[0].isalpha():
            return
        if symbol in self._sec:
            name = self._names.get(("equity", symbol), self._sec[symbol].title)
            sink.add("equity", symbol, name, CASHTAG_CONF, in_title=in_title)
        elif symbol in self._bist:
            eid = self._bist[symbol]
            sink.add("equity", eid, self._names.get(("equity", eid), eid), CASHTAG_CONF,
                     in_title=in_title)
        elif symbol in self._crypto:
            sink.add("crypto", symbol, self._crypto[symbol], CASHTAG_CONF, in_title=in_title)

    def _pair(self, tok: Token, in_title: bool, sink: _Sink) -> bool:
        raw = tok.raw.rstrip(".")
        if "/" in raw:
            parts = raw.split("/")
            if len(parts) != 2:
                return False
            left, right = self._currency(parts[0]), self._currency(parts[1])
            if left and right and left != right:
                sink.add("fx", f"{left}{right}", f"{left}/{right}", PAIR_CONF, in_title=in_title)
                return True
            return False
        if len(raw) == 6 and raw.isupper():
            left, right = raw[:3], raw[3:]
            if left in self._currency_codes and right in self._currency_codes and left != right:
                sink.add("fx", f"{left}{right}", f"{left}/{right}", PAIR_CONF, in_title=in_title)
                return True
        return False

    def _currency(self, word: str) -> str | None:
        if word.isupper() and word in self._currency_codes:
            return word
        return self._currency_names.get(phrase_key(word))

    def _match_alias(
        self, tokens: list[Token], i: int, blocked: set[int],
        sent_ranges: list[tuple[int, int]], in_title: bool, sink: _Sink,
    ) -> int:
        n = len(tokens)
        for width in range(min(MAX_NGRAM, self._max_len, n - i), 0, -1):
            span = range(i, i + width)
            if any(j in blocked for j in span):
                continue
            key = " ".join(tokens[j].key for j in span)
            cands = self._aliases.get(key)
            if not cands:
                continue
            accepted = [
                c for c in cands
                if _accept(c, tokens, i, i + width - 1, sent_ranges)
                and not (c.etype == "crypto" and c.upper and c.eid in self._sec)
                and not (sink.foreign and c.auto)
            ]
            if not accepted:
                continue
            for cand in accepted:
                self._emit(cand, in_title, sink)
            return width
        return 0

    def _emit(self, cand: _Cand, in_title: bool, sink: _Sink) -> None:
        conf = cand.conf
        if in_title and cand.etype == "country":
            conf = max(conf, TITLE_COUNTRY_CONF)
        sink.add(cand.etype, cand.eid, cand.name, conf, in_title=in_title, dampen=cand.dampen)
        for link in cand.links:
            if link.unless and any(f" {word} " in sink.item for word in link.unless):
                continue
            sink.add(link.etype, link.eid, self._names.get((link.etype, link.eid), link.eid),
                     link.conf, in_title=in_title, dampen=link.dampen, cap=link.cap,
                     person_implied=link.person_implied)

    def _bare_ticker(
        self, tokens: list[Token], i: int, text: str, in_title: bool, sink: _Sink,
    ) -> None:
        tok = tokens[i]
        raw = tok.raw.rstrip(".")
        if not _TICKER_RE.fullmatch(raw) or sum(ch.isalpha() for ch in raw) < 2:
            return
        symbol = raw.replace(".", "-")
        if symbol in STOP_TICKERS:
            return
        if symbol == "AA" and i + 1 < len(tokens) and tokens[i + 1].key in AGENCY_NEXT_WORDS:
            return  # Anadolu Ajansı (AA muhabiri, AA haberi)
        if symbol in self._sec:
            if sink.foreign:
                return  # bare US tickers are not read in non-English copy
            if _in_parens(text, tok) or _near(tokens, i, BARE_CONTEXT, 3):
                name = self._names.get(("equity", symbol), self._sec[symbol].title)
                sink.add("equity", symbol, name, BARE_TICKER_CONF, in_title=in_title)
            return
        if raw in self._bist and _near(tokens, i, BIST_CONTEXT, 3, both_sides=False):
            eid = self._bist[raw]
            sink.add("equity", eid, self._names.get(("equity", eid), eid), BARE_TICKER_CONF,
                     in_title=in_title)


def _inside(tok: Token, spans: list[tuple[int, int]]) -> bool:
    return any(start <= tok.start < end for start, end in spans)


def _sentence_ranges(tokens: list[Token]) -> list[tuple[int, int]]:
    """Inclusive token index range of each token's sentence."""
    ranges: list[tuple[int, int]] = [(0, 0)] * len(tokens)
    start = 0
    for idx in range(1, len(tokens) + 1):
        if idx == len(tokens) or tokens[idx].sentence != tokens[start].sentence:
            for j in range(start, idx):
                ranges[j] = (start, idx - 1)
            start = idx
    return ranges


def _first_letter(raw: str) -> str:
    return next((ch for ch in raw if ch.isalpha()), "")


def _accept(
    cand: _Cand, tokens: list[Token], a: int, b: int, sent_ranges: list[tuple[int, int]],
) -> bool:
    """Apply the case, neighbour, context and exclusion gates of one candidate to a span."""
    letter = _first_letter(tokens[a].raw)
    nxt = tokens[b + 1].key if b + 1 < len(tokens) else ""
    prv = tokens[a - 1].key if a > 0 else ""
    shape_ok = (not cand.capital or (bool(letter) and letter.isupper())) and (
        not cand.upper or all(tokens[j].raw.upper() == tokens[j].raw for j in range(a, b + 1))
    )
    if not shape_ok:
        return False
    neighbour_ok = (not cand.require_next or nxt in cand.require_next) and (
        nxt not in cand.exclude_next and prv not in cand.exclude_prev
    )
    if not neighbour_ok:
        return False
    if cand.next_not_capital and b + 1 < len(tokens) and _first_letter(tokens[b + 1].raw).isupper():
        return False
    lo, hi = sent_ranges[a]
    left, right = (lo, hi) if cand.window is None else (max(lo, a - cand.window), min(hi, b + cand.window))
    needs = cand.require_context or (cand.start_needs_context and a == lo)
    if not needs and not cand.exclude_context:
        return True
    words = {tokens[j].key for j in range(left, right + 1) if not a <= j <= b}
    if cand.exclude_context and words & cand.exclude_context:
        return False
    return not needs or (cand.context is not None and bool(words & cand.context))


def _near(
    tokens: list[Token], i: int, words: frozenset[str], window: int, *, both_sides: bool = True,
) -> bool:
    """True when a context word sits within ``window`` tokens before (and after) index ``i``."""
    if both_sides:
        lo, hi = max(0, i - window), min(len(tokens), i + window + 1)
    else:
        lo, hi = i + 1, min(len(tokens), i + window + 1)
    return any(tokens[j].key in words for j in range(lo, hi) if j != i)


def _in_parens(text: str, tok: Token) -> bool:
    before = text[: tok.start].rstrip()
    after = text[tok.end :].lstrip()
    return before.endswith("(") and after.startswith(")")
