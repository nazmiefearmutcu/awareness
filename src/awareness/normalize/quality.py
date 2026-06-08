"""Gopher/C4-style content-quality heuristics for noisy bulk corpora (WET).

Cheap, mostly-language-agnostic signals from the Gopher (Rae et al., 2021) and
C4 (Raffel et al., 2020) cleaning recipes, used to drop boilerplate, link
farms, and symbol spam from Common Crawl WET text before it is stored. The
gates are checked in cost order; the first failure short-circuits with a reason.
"""

from __future__ import annotations

from dataclasses import dataclass

# A handful of high-frequency English function words; "≥2 present" is Gopher's
# cheap "is this running prose" signal. (WET English-leaning; non-English text
# is gated upstream by the language filter, not dropped here.)
_STOPWORDS = frozenset({"the", "be", "to", "of", "and", "that", "have", "with"})

_MIN_WORDS = 50
_MAX_WORDS = 100_000
_MIN_MEAN_WORD_LEN = 3.0
_MAX_MEAN_WORD_LEN = 10.0
_MAX_SYMBOL_WORD_RATIO = 0.10
_MAX_BULLET_LINE_FRAC = 0.90
_MAX_ELLIPSIS_LINE_FRAC = 0.30
_MIN_ALPHA_WORD_FRAC = 0.80
_MIN_STOPWORDS = 2
_BULLETS = frozenset({"•", "-", "*", "·", "‣", "◦"})


@dataclass(frozen=True)
class QualityVerdict:
    ok: bool
    reason: str | None = None


def gopher_quality(text: str) -> QualityVerdict:  # noqa: PLR0911
    """Apply the Gopher/C4 quality gates; return ok + the first failing reason."""
    words = text.split()
    n = len(words)
    if n < _MIN_WORDS:
        return QualityVerdict(False, "too_few_words")
    if n > _MAX_WORDS:
        return QualityVerdict(False, "too_many_words")

    mean_len = sum(len(w) for w in words) / n
    if not (_MIN_MEAN_WORD_LEN <= mean_len <= _MAX_MEAN_WORD_LEN):
        return QualityVerdict(False, "mean_word_length")

    n_symbols = text.count("#") + text.count("...") + text.count("…")
    if n_symbols / n > _MAX_SYMBOL_WORD_RATIO:
        return QualityVerdict(False, "symbol_to_word_ratio")

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if lines:
        bullets = sum(1 for ln in lines if ln.lstrip()[:1] in _BULLETS)
        if bullets / len(lines) > _MAX_BULLET_LINE_FRAC:
            return QualityVerdict(False, "bullet_lines")
        ellipsis = sum(1 for ln in lines if ln.rstrip().endswith(("...", "…")))
        if ellipsis / len(lines) > _MAX_ELLIPSIS_LINE_FRAC:
            return QualityVerdict(False, "ellipsis_lines")

    alpha_words = sum(1 for w in words if any(c.isalpha() for c in w))
    if alpha_words / n < _MIN_ALPHA_WORD_FRAC:
        return QualityVerdict(False, "low_alpha_fraction")

    lowered = {w.strip(".,!?;:\"'()[]").lower() for w in words}
    if len(_STOPWORDS & lowered) < _MIN_STOPWORDS:
        return QualityVerdict(False, "no_stopwords")

    return QualityVerdict(True, None)
