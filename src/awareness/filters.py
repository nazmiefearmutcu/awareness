"""Topic / keyword filtering applied at ingest time.

A :class:`TopicFilter` decides whether a captured document is "about" the
topics the user asked for. It is applied in the worker pipeline to BOTH
backfill and tail ingestion — immediately after an adapter yields a capture
and before dedup/storage — so non-matching documents are dropped before they
ever cost disk. This is what turns "ingest the whole web" into "ingest only
documents about X".

Semantics (all case-insensitive):

* **keyword mode** (default): each term matches as a **whole word/phrase**
  (word boundaries are added around the term, so ``ai`` matches "AI" but not
  "said"). Use regex mode for partial/substring matching.
* **regex mode**: each term is a Python regular expression (``re.search``);
  a term that fails to compile silently falls back to a literal substring.
* ``match_all=False`` (default): a doc passes if **any** term matches (OR).
* ``match_all=True``: a doc passes only if **all** terms match (AND).
* ``field``: which text to search — ``"title"``, ``"text"`` or ``"both"``.

An empty term list yields an *inactive* filter that passes everything, so the
worker can treat "no filter configured" and "filter that matches all" the
same way.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from awareness.obs.logging import get_logger

logger = get_logger("filters")

_FIELDS = ("title", "text", "both")


def _word_boundary(term: str) -> str:
    """Escaped term wrapped in ``\\b`` only where its edges are word chars.

    So ``ai`` → ``\\bai\\b`` (won't match "said"), ``.net`` → ``\\.net\\b``,
    ``c++`` → ``\\bc\\+\\+`` — boundaries are skipped next to punctuation,
    where ``\\b`` would never match.
    """
    esc = re.escape(term)
    pre = r"\b" if term[:1].isalnum() or term[:1] == "_" else ""
    post = r"\b" if term[-1:].isalnum() or term[-1:] == "_" else ""
    return f"{pre}{esc}{post}"


class TopicFilter:
    """Compiled, reusable predicate over a capture's title/text."""

    def __init__(
        self,
        terms: Iterable[str],
        *,
        match_all: bool = False,
        regex: bool = False,
        field: str = "both",
    ) -> None:
        self.terms: tuple[str, ...] = tuple(s for t in terms if (s := str(t).strip()))
        self.match_all = bool(match_all)
        self.regex = bool(regex)
        self.field = field if field in _FIELDS else "both"
        self._patterns: list[re.Pattern[str]] = []
        for term in self.terms:
            pattern: re.Pattern[str] | None = None
            if self.regex:
                try:
                    pattern = re.compile(term, re.IGNORECASE)
                except re.error:
                    # M-25: a bad regex silently becoming a literal substring
                    # can drop everything (or nothing) — surface it.
                    logger.warning(
                        "topic_filter_bad_regex",
                        term=term,
                        fallback="literal",
                    )
                    pattern = None
            if pattern is None:
                pattern = re.compile(_word_boundary(term), re.IGNORECASE)
            if self.regex and self.match_all and pattern.search("") is not None:
                # M-24: a pattern that matches the empty string (``.*``,
                # ``.+?``) makes an AND filter trivially true for every doc —
                # drop it so the remaining terms still constrain.
                logger.warning(
                    "topic_filter_empty_match_regex_dropped",
                    term=term,
                )
                continue
            self._patterns.append(pattern)

    @property
    def active(self) -> bool:
        """True if this filter actually constrains anything."""
        return bool(self._patterns)

    def _haystack(self, title: str, text: str) -> str:
        if self.field == "title":
            return title or ""
        if self.field == "text":
            return text or ""
        return f"{title or ''}\n{text or ''}"

    def matches(self, title: str = "", text: str = "") -> bool:
        """Whether a document with this title/text should be kept."""
        if not self._patterns:
            return True
        hay = self._haystack(title, text)
        hits = (p.search(hay) is not None for p in self._patterns)
        return all(hits) if self.match_all else any(hits)

    def describe(self) -> str:
        """Short human description for logs / status lines."""
        if not self.active:
            return "off"
        joiner = " AND " if self.match_all else " OR "
        mode = "regex" if self.regex else "keyword"
        return f"{mode}[{self.field}]: " + joiner.join(self.terms)

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None) -> TopicFilter | None:
        """Build from a job-request dict (backfill request or tail seeds).

        Returns ``None`` when no topic terms are configured, so callers can
        cheaply skip filtering entirely.
        """
        if not cfg:
            return None
        raw = cfg.get("match")
        if isinstance(raw, str):
            candidates: list[Any] = [raw]
        elif isinstance(raw, Iterable):
            candidates = list(raw)
        else:
            candidates = []
        terms = [s for t in candidates if (s := str(t).strip())]
        if not terms:
            return None
        return cls(
            terms,
            match_all=bool(cfg.get("match_all")),
            regex=bool(cfg.get("match_regex")),
            field=str(cfg.get("match_field") or "both"),
        )
