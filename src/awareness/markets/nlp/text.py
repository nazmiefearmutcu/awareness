"""Tokenisation and folding shared by the NLP units (English and Turkish).

Folding maps a surface token to a lookup key: Unicode accents are dropped
(``Altin`` -> ``altin``, ``Turkiye`` -> ``turkiye``), dotless i / dotted capital I
become ``i``, case is lowered, a possessive or case suffix after an apostrophe
is cut (``Fed's`` -> ``fed``, ``Istanbul'da`` -> ``istanbul``) and trailing dots
are removed (``Inc.`` -> ``inc``) while internal dots survive (``U.S.`` -> ``u.s``).
"""

from __future__ import annotations

import bisect
import re
import unicodedata
from dataclasses import dataclass

# A token is a word with optional internal connectors (``BRK.B``, ``EUR/USD``,
# ``Nasdaq-100``, ``S&P``, ``U.S.``, ``Fed's``); a leading ``$`` marks a cashtag.
TOKEN_RE = re.compile(r"\$?[^\W_]+(?:[.'\u2019&/\-][^\W_]+)*\.?")
# Sentence boundaries: hard punctuation, newlines, and periods after a 3+ letter
# word or a number (so ``U.S. Steel`` does not split on the inner dots).
BOUNDARY_RE = re.compile(r"[!?;\n]|(?<=[^\W\d_]{3})\.(?=\s)|(?<=\d)\.(?=\s)")
_APOSTROPHE_RE = re.compile(r"['\u2019]")
_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class Token:
    """One surface token with its folded lookup key and sentence index."""

    raw: str
    key: str
    start: int
    end: int
    sentence: int


def fold(word: str) -> str:
    """Return the folded lookup key for a single word or phrase fragment."""
    base = _APOSTROPHE_RE.split(word.lstrip("$"), maxsplit=1)[0].rstrip(".")
    decomposed = unicodedata.normalize("NFKD", base)
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return plain.replace("\u0131", "i").replace("\u0130", "I").lower()


def fold_text(text: str) -> str:
    """Fold a whole string for word-boundary regex matching (taxonomy)."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    plain = plain.replace("\u0131", "i").replace("\u0130", "I").replace("\u2019", "'").lower()
    return _SPACE_RE.sub(" ", plain)


def tokenize(text: str) -> list[Token]:
    """Split text into :class:`Token` records with folded keys and sentence ids."""
    if not text:
        return []
    bounds = [m.start() for m in BOUNDARY_RE.finditer(text)]
    out: list[Token] = []
    for match in TOKEN_RE.finditer(text):
        raw = match.group(0)
        key = fold(raw)
        if not key:
            continue
        sentence = bisect.bisect_left(bounds, match.start())
        out.append(Token(raw, key, match.start(), match.end(), sentence))
    return out


def phrase_key(phrase: str) -> str:
    """Canonical dictionary key for a (possibly multi-word) alias."""
    return " ".join(tok.key for tok in tokenize(phrase))
