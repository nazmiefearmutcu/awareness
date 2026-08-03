"""Heuristic entity extraction for captured text.

Lightweight, dependency-free named-entity extraction tuned for financial and
general news text. No spaCy, no model weights: ORG / PERSON / PLACE / TICKER
are found from title-case sequences, known suffix dictionaries, a built-in
geo/ticker lexicon, and a handful of conservative stopword guards.

The extractor is deterministic — same input, same output — so aggregations
over the corpus are stable across runs.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

Kind = str
# Kind = Literal["ORG", "PERSON", "PLACE", "TICKER"]

# --- Dictionaries -----------------------------------------------------------

_TICKERS: frozenset[str] = frozenset(
    {
        # crypto
        "BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "DOT", "AVAX", "LINK",
        "UNI", "AAVE", "APT", "SUI", "INJ", "ARB", "OP", "SEI", "PEPE",
        "BONK", "WIF", "TON", "LTC", "BCH", "SHIB", "ATOM", "NEAR", "FIL",
        "ICP", "HBAR", "VET", "ALGO", "ETC", "EOS", "XTZ", "XLM", "TRX",
        # equities / indices / funds
        "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "NVDA", "AMD",
        "INTC", "NFLX", "SPY", "QQQ", "VIX", "GLD", "SLV", "USO", "JPM",
        "BAC", "GS", "XOM", "CVX", "KO", "PEP", "WMT", "DIS", "BA", "GE",
    }
)

_GEO_TERMS: frozenset[str] = frozenset(
    {
        # cities
        "Istanbul", "Ankara", "Izmir", "Tokyo", "Osaka", "London", "New York",
        "Los Angeles", "Chicago", "Houston", "San Francisco", "Boston",
        "Seattle", "Miami", "Berlin", "Munich", "Frankfurt", "Paris", "Lyon",
        "Marseille", "Beijing", "Shanghai", "Shenzhen", "Moscow", "Saint Petersburg",
        "Dubai", "Abu Dhabi", "Singapore", "Zurich", "Geneva", "Washington",
        "Seoul", "Sydney", "Melbourne", "Toronto", "Vancouver", "Montreal",
        "Mumbai", "Delhi", "Bangalore", "Sao Paulo", "Rio de Janeiro",
        "Mexico City", "Johannesburg", "Cape Town", "Cairo", "Nairobi",
        "Lagos", "Amsterdam", "Madrid", "Barcelona", "Rome", "Milan", "Vienna",
        "Stockholm", "Oslo", "Helsinki", "Warsaw", "Prague", "Budapest",
        "Athens", "Lisbon", "Dublin", "Brussels", "Copenhagen", "Buenos Aires",
        "Santiago", "Lima", "Bogota", "Karachi", "Dhaka", "Jakarta", "Bangkok",
        "Manila", "Kuala Lumpur", "Ho Chi Minh City", "Taipei", "Hong Kong",
        "Riyadh", "Doha", "Tel Aviv", "Kyiv",
        # countries
        "Turkey", "Turkkiye", "Germany", "France", "United Kingdom", "China",
        "Russia", "Japan", "India", "Brazil", "Canada", "Australia",
        "Switzerland", "Netherlands", "Spain", "Italy", "Poland", "Sweden",
        "Norway", "Ukraine", "Iran", "Saudi Arabia", "United Arab Emirates",
        "Egypt", "South Africa", "Nigeria", "Mexico", "Argentina", "Chile",
        "Colombia", "Indonesia", "Vietnam", "Thailand", "South Korea",
        "United States", "Portugal", "Belgium", "Austria", "Denmark",
        "Finland", "Ireland", "Greece", "Czech Republic", "Hungary",
        "Romania", "Bulgaria", "Croatia", "Serbia", "Israel", "Jordan",
        "Qatar", "Kuwait", "Oman", "Morocco", "Tunisia", "Algeria",
        "Ethiopia", "Kenya", "Ghana", "Pakistan", "Bangladesh", "Sri Lanka",
        "Malaysia", "Philippines", "New Zealand",
    }
)

_ORG_SUFFIXES: frozenset[str] = frozenset(
    {
        "Inc", "Corp", "Corporation", "Ltd", "LLC", "Bank", "Group",
        "Exchange", "University", "Fund", "ETF", "Agency", "Association",
        "Institute", "Commission", "Council", "Union", "Holdings", "Labs",
        "Technologies", "Systems", "Media", "News", "Times", "Post", "Journal",
        "Herald", "Tribune", "Reserve", "Fed", "Parliament", "Senate",
        "Congress", "Ministry", "Department", "Authority", "Board",
        "Committee", "Foundation", "Organization", "Organisation",
        "Partnership", "Company", "Co", "Club", "Center", "Centre", "School",
        "College", "Hospital", "Trust", "Airways", "Airlines", "Energy",
        "Petroleum", "Pharma", "Therapeutics", "Biosciences", "Capital",
        "Ventures", "Partners", "Advisors", "Consulting", "Research",
    }
)

_PERSON_STOP: frozenset[str] = frozenset(
    {
        "The", "A", "An", "On", "In", "At", "For", "To", "Of", "And", "Or",
        "With", "By", "From", "Into", "Onto", "Upon", "About", "After",
        "Before", "During", "Under", "Over", "Via", "Vs", "This", "That",
        "These", "Those", "His", "Her", "Their", "Our", "Your", "Its", "It",
        "He", "She", "They", "We", "You", "I", "Not", "But", "As", "If",
        "When", "Where", "Who", "Whom", "Which", "What", "How", "All", "Any",
        "Some", "More", "Most", "Other", "New", "Old", "High", "Low", "Big",
        "Small", "Large", "First", "Last", "Next", "Every", "Each", "Both",
    }
)

_PLACE_SUFFIXES: tuple[str, ...] = (
    "City", "Country", "State", "Province", "Island", "Bay", "Lake", "River",
    "Mountain", "County", "Region", "District", "Valley", "Coast", "Desert",
    "Peninsula", "Gulf",
)

# Common-noun tokens that make a title-case span a headline fragment
# ("Bitcoin Rally", "Oil Price", "Market Rally Today") rather than a person.
_COMMON_HEADLINE_NOUNS: frozenset[str] = frozenset(
    {
        "Rally", "Price", "Prices", "Market", "Markets", "Stock", "Stocks",
        "Shares", "Rates", "Rate", "Deal", "Deals", "Record", "High", "Low",
        "Peak", "Drop", "Rise", "Surge", "Slump", "Gain", "Gains", "Loss",
        "Losses", "Profit", "Revenue", "Earnings", "Report", "Reports",
        "Update", "Updates", "News", "Analysis", "Outlook", "Forecast",
        "Growth", "Inflation", "Economy", "Fed", "Bond", "Bonds", "Yield",
        "Yields", "Oil", "Gold", "Dollar", "Euro", "Index", "Indices",
        "Fund", "Funds", "ETF", "ETFs", "Token", "Tokens", "Coin", "Coins",
    }
)

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'&.-]*")
_WS_RE = re.compile(r"\s+")


def _collapsed(text: str) -> str:
    return _WS_RE.sub(" ", text.replace("\n", " ").replace("\r", " ")).strip()


def _is_title_case(word: str) -> bool:
    return bool(word) and word[0].isupper() and word.isalpha()


def _ticker_matches(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for ticker in _TICKERS:
        # $BTC or bare BTC, bounded by non-word / non-$ chars.
        pat = re.compile(
            rf"(?<![A-Za-z0-9$])\$?{re.escape(ticker)}(?![A-Za-z0-9])",
            re.IGNORECASE,
        )
        if pat.search(text):
            found.append((ticker, "TICKER"))
    return found


def _span_entities(words: list[str]) -> list[tuple[str, str]]:
    """Classify title-case spans of 1-4 consecutive words.

    Tokens retain their original order (lowercase words included as
    separators). A span breaks on any non-title-case word. Within a span:
      * every ORG-suffix token forms an ORG with up to 3 preceding
        non-suffix title-case words (PERSON_STOP words excluded),
      * every run of non-suffix words is scanned for PLACE (geo terms,
        including multi-word entries) and conservative 2-word PERSONs.
    """
    results: list[tuple[str, str]] = []
    n = len(words)
    i = 0
    while i < n:
        if not _is_title_case(words[i]):
            i += 1
            continue
        j = i
        while j + 1 < n and _is_title_case(words[j + 1]):
            j += 1
        span = words[i : j + 1]
        if len(span) > 8:
            span = span[:8]

        # 1) ORG: each suffix token + up to 3 preceding non-suffix words.
        suffix_positions = {k for k, w in enumerate(span) if w in _ORG_SUFFIXES}
        for k in suffix_positions:
            prefix: list[str] = []
            for p in range(k - 1, -1, -1):
                if p in suffix_positions:
                    break
                if span[p] in _PERSON_STOP:
                    continue
                prefix.append(span[p])
                if len(prefix) >= 3:
                    break
            org = " ".join([*reversed(prefix), span[k]])
            if org:
                results.append((org, "ORG"))

        # 2) Runs of non-suffix words: PLACE (geo, incl. multi-word) + PERSON.
        run: list[str] = []
        for k, w in enumerate(span):
            if k in suffix_positions:
                run = []
                continue
            run.append(w)
            for length in (1, 2, 3, 4):
                if len(run) < length:
                    continue
                candidate = " ".join(run[-length:])
                if all(t in _GEO_TERMS for t in run[-length:]) or candidate in _GEO_TERMS:
                    results.append((candidate, "PLACE"))
                    break
        run_clean = [w for k, w in enumerate(span) if k not in suffix_positions]
        if (
            not suffix_positions
            and 2 <= len(run_clean) <= 3
            and run_clean[0] not in _PERSON_STOP
            and not any(w in _GEO_TERMS for w in run_clean)
            and not any(w in _COMMON_HEADLINE_NOUNS for w in run_clean)
        ):
            results.append((" ".join(run_clean[:2]), "PERSON"))

        i = j + 1
    return results


def normalize_entity(text: str) -> str:
    """Canonical form: collapsed whitespace, title-cased.

    Plural stripping is intentionally NOT applied: it corrupts known
    entities ("Los Angeles" -> "Los Angele", "United States" -> "United
    State") and breaks query round-trips (a normalized query can never
    match corpus text). Articles/conjunctions stay lowercase unless leading.
    """
    text = _collapsed(text)
    words = text.split()
    if not words:
        return text
    small = {"and", "of", "the", "for", "in", "on", "at", "to", "vs", "vs."}
    out: list[str] = []
    for idx, w in enumerate(words):
        if idx == 0 or w.lower() not in small:
            out.append(w.capitalize() if w.islower() else w)
        else:
            out.append(w.lower())
    return " ".join(out)


def extract_entities(text: str) -> list[tuple[str, str]]:
    """Extract (normalized_text, kind) entities from a single text.

    Returns a de-duplicated list, deterministic order: TICKER first, then
    ORG / PLACE / PERSON in first-appearance order.
    """
    text = _collapsed(text)
    if not text:
        return []
    results: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for ticker in _TICKERS:
        pat = re.compile(
            rf"(?<![A-Za-z0-9$])\$?{re.escape(ticker)}(?![A-Za-z0-9])",
            re.IGNORECASE,
        )
        if pat.search(text):
            key = (ticker, "TICKER")
            if key not in seen:
                seen.add(key)
                results.append(key)

    tokens = _WORD_RE.findall(text)
    for entity, kind in _span_entities(tokens):
        norm = normalize_entity(entity)
        if len(norm) < 2 or len(norm) > 80:
            continue
        if norm.lower() in {"the", "a", "an", "this", "that", "these", "new", "old", "big", "high", "low"}:
            continue
        key = (norm, kind)
        if key not in seen:
            seen.add(key)
            results.append(key)

    return results


def extract_entities_batch(texts: Iterable[str]) -> dict[tuple[str, str], int]:
    """Aggregate extraction over many texts into a Counter-like dict."""
    counts: dict[tuple[str, str], int] = {}
    for text in texts:
        for entity, kind in extract_entities(text):
            key = (entity, kind)
            counts[key] = counts.get(key, 0) + 1
    return counts
