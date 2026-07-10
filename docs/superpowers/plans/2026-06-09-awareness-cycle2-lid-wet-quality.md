# Awareness Cycle 2 — Plan 4: Confidence-Aware LID + Gopher/C4 WET Quality Filter (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** (1) Make language detection *confidence-aware* — return a probability and suppress low-confidence guesses to `None` — and (2) add a Gopher/C4-style content-quality filter that drops boilerplate / link-farm / symbol-spam records from the noisy Common Crawl **WET** stream before they are stored.

**Architecture & key decision — no new dependency.** `normalize/text.py::detect_language` already uses **langdetect** (a core dep). langdetect exposes `detect_langs()` (ranked languages + probabilities), so "confidence-aware LID" is reachable *with the dep already present* — a heavier swap (fastText's ~126 MB model, CLD3's native build) would be marginal accuracy for real cost in this uv/venv. Detection stays behind the single `detect_language*` surface, so a stronger backend remains a future one-function swap. The WET quality filter is new, **pure-Python**, language-agnostic, and implemented as Gopher (Rae et al. 2021) + C4 (Raffel et al. 2020) heuristics — and is wired in as an extracted pure helper (`_record_passes_quality`) per this codebase's testing convention (cf. `_record_passes_domain_filter`).

**Tech Stack:** Python 3.13 stdlib + existing `langdetect`. No new deps. pytest.

**Standard test command:** `PYTHONPATH=src .venv/bin/python -m pytest -q -p no:cacheprovider`
**Full-suite gate:** `PYTHONPATH=src .venv/bin/python -m pytest -q -m "not slow and not smoke"`
**Baseline at plan start:** 265 passing. *(Note: the full suite can take minutes offline because `DuckDbIndex.connect()` runs network `INSTALL iceberg/fts`; unrelated to this plan.)*

**Spec:** `docs/superpowers/specs/2026-06-08-awareness-cycle2-math-systems-design.md` (Plan 4 — Language detection & quality).

**Files touched:**
- Modify: `src/awareness/normalize/text.py` — `detect_language_conf()` + confidence-gated `detect_language()`.
- Create: `src/awareness/normalize/quality.py` — pure `gopher_quality()`.
- Modify: `src/awareness/config/settings.py` — `wet_quality_filter: bool = True`.
- Modify: `src/awareness/sources/commoncrawl_wet.py` — `_record_passes_quality()` helper + wire it into `_parse_wet_to_captures`.
- Tests: append to `tests/unit/test_normalize.py`; create `tests/unit/test_quality.py`, `tests/unit/test_cc_wet_quality.py`.

---

### Task 1: Confidence-aware language detection

**Files:**
- Modify: `src/awareness/normalize/text.py` (replace `detect_language`, add `detect_language_conf` + two module constants)
- Test: `tests/unit/test_normalize.py` (append tests; extend the import line)

- [ ] **Step 1: Write the failing test** — append to `tests/unit/test_normalize.py` and update line 3's import:

Change the import at the top:
```python
from awareness.normalize.text import (
    detect_language,
    detect_language_conf,
    normalize_text,
    safe_title,
)
```

Append these tests:
```python
def test_detect_language_conf_returns_language_and_confidence() -> None:
    text = "The quick brown fox jumps over the lazy dog. " * 30
    lang, conf = detect_language_conf(text)
    assert lang is not None
    assert 0.0 < conf <= 1.0


def test_detect_language_conf_short_text_is_none_zero() -> None:
    assert detect_language_conf("hi") == (None, 0.0)


def test_detect_language_conf_gates_below_min_confidence() -> None:
    # An impossible threshold suppresses the language but still reports the
    # measured confidence — proving the GATE, not the detector, made the call.
    text = "The quick brown fox jumps over the lazy dog. " * 30
    lang, conf = detect_language_conf(text, min_confidence=1.01)
    assert lang is None
    assert conf > 0.0


def test_detect_language_conf_is_deterministic() -> None:
    text = "Bonjour tout le monde, ceci est un court texte en francais. " * 10
    assert detect_language_conf(text) == detect_language_conf(text)


def test_detect_language_applies_confidence_gate() -> None:
    text = "The quick brown fox jumps over the lazy dog. " * 30
    assert detect_language(text, min_confidence=1.01) is None   # gated
    assert detect_language(text) is not None                    # default keeps clear English
```

- [ ] **Step 2: Run, confirm FAIL** (`ImportError: cannot import name 'detect_language_conf'`):
`PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_normalize.py -q`

- [ ] **Step 3: Implement** — in `src/awareness/normalize/text.py`, REPLACE the existing `detect_language` function (currently the `def detect_language(text: str) -> str | None:` block, ~lines 88-99) with:

```python
DEFAULT_LID_MIN_CHARS = 80
DEFAULT_LID_MIN_CONFIDENCE = 0.50


def detect_language_conf(
    text: str,
    *,
    min_chars: int = DEFAULT_LID_MIN_CHARS,
    min_confidence: float = 0.0,
) -> tuple[str | None, float]:
    """Best-effort ``(language, confidence)`` for ``text`` (ISO-639-1).

    Returns ``(None, 0.0)`` when the text is shorter than ``min_chars`` or the
    detector finds no language. When the top language's probability is below
    ``min_confidence`` the language is suppressed to ``None`` but the measured
    confidence is still returned, so callers can log or re-threshold it.
    """
    if not text or len(text) < min_chars:
        return (None, 0.0)
    try:
        from langdetect import DetectorFactory, detect_langs  # noqa: PLC0415

        DetectorFactory.seed = 0  # deterministic
        ranked = detect_langs(text[:5000])
    except (ImportError, Exception):  # langdetect raises LangDetectException
        return (None, 0.0)
    if not ranked:
        return (None, 0.0)
    top = ranked[0]
    conf = float(top.prob)
    if conf < min_confidence:
        return (None, conf)
    return (str(top.lang), conf)


def detect_language(text: str, *, min_confidence: float = DEFAULT_LID_MIN_CONFIDENCE) -> str | None:
    """Best-effort language code (ISO-639-1), confidence-gated. ``None`` on
    failure or when detection is below ``min_confidence``.

    Confidence-aware upgrade of the old top-1 ``langdetect.detect`` call: every
    source that calls ``detect_language(text)`` now stores a language only when
    the detector is at least ``DEFAULT_LID_MIN_CONFIDENCE`` sure, otherwise
    ``None`` (ambiguous text no longer gets a confidently-wrong label).
    """
    lang, _conf = detect_language_conf(
        text, min_chars=DEFAULT_LID_MIN_CHARS, min_confidence=min_confidence
    )
    return lang
```

(The five callers — `commoncrawl_wet.py`, `fineweb.py`, `warc_repair.py`, `tail_recrawl.py`, `local_fixture.py` — all call `detect_language(text)` positionally and are unchanged; they inherit the confidence gate for free. `fineweb`/`local_fixture` already trust row metadata first, so the "trust FineWeb metadata" requirement is already satisfied.)

- [ ] **Step 4: Confirm PASS** (incl. the two pre-existing `test_detect_language_*` tests): `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_normalize.py -q`
- [ ] **Step 5: Full-suite gate:** `PYTHONPATH=src .venv/bin/python -m pytest -q -m "not slow and not smoke"` (expect 270 = 265 + 5).
- [ ] **Step 6: Ruff:** `.venv/bin/python -m ruff check src/awareness/normalize/text.py tests/unit/test_normalize.py` — no NEW errors (the inline `langdetect` import keeps its existing `# noqa: PLC0415`; the broad `except ... Exception` matches the file's existing convention).
- [ ] **Step 7: Commit:**
```bash
git add src/awareness/normalize/text.py tests/unit/test_normalize.py
git commit -m "feat(lid): confidence-aware language detection with min-confidence gate"
```

---

### Task 2: Pure Gopher/C4 quality module

**Files:**
- Create: `src/awareness/normalize/quality.py`
- Test (create): `tests/unit/test_quality.py`

- [ ] **Step 1: Write the failing test** — create `tests/unit/test_quality.py`:

```python
"""Gopher/C4 content-quality heuristics (pure, language-agnostic)."""

from __future__ import annotations

from awareness.normalize.quality import gopher_quality


def _clean_paragraph() -> str:
    # ~64 words, stopword-rich, normal word lengths, no symbols/bullets.
    return (
        "The committee reviewed the annual report and approved the budget "
        "with broad support from the members that attended the meeting. "
    ) * 4


def test_accepts_clean_prose() -> None:
    v = gopher_quality(_clean_paragraph())
    assert v.ok is True
    assert v.reason is None


def test_rejects_too_few_words() -> None:
    v = gopher_quality("hello world")
    assert v.ok is False
    assert v.reason == "too_few_words"


def test_rejects_no_stopwords() -> None:
    # 64 content words, none in the stop set, normal lengths, all alpha.
    text = (
        "elephant giraffe mountain river forest planet harbor lantern "
        "compass blanket biscuit garden window meadow anchor saddle "
    ) * 4
    v = gopher_quality(text)
    assert v.ok is False
    assert v.reason == "no_stopwords"


def test_rejects_symbol_spam() -> None:
    # Clean prose + many '#'-bearing tokens: exceeds the symbol/word ratio
    # without tripping the word-count or mean-length gates.
    text = _clean_paragraph() + " " + " ".join(f"item#{i}" for i in range(40))
    v = gopher_quality(text)
    assert v.ok is False
    assert v.reason == "symbol_to_word_ratio"


def test_rejects_bullet_list() -> None:
    text = "\n".join(f"• item number {i} with the value and that note" for i in range(60))
    v = gopher_quality(text)
    assert v.ok is False
    assert v.reason == "bullet_lines"
```

- [ ] **Step 2: Run, confirm FAIL** (`ModuleNotFoundError: No module named 'awareness.normalize.quality'`):
`PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_quality.py -q`

- [ ] **Step 3: Implement** — create `src/awareness/normalize/quality.py`:

```python
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


def gopher_quality(text: str) -> QualityVerdict:
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
```

- [ ] **Step 4: Confirm PASS:** `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_quality.py -q`. If a crafted input trips an *earlier* gate than the one it targets (gate order is word-count → mean-length → symbol → bullets → ellipsis → alpha → stopwords), adjust that test's input minimally so the intended gate fires first, and note it under Deviations.
- [ ] **Step 5: Full-suite gate:** `PYTHONPATH=src .venv/bin/python -m pytest -q -m "not slow and not smoke"` (expect 275 = 270 + 5).
- [ ] **Step 6: Ruff:** `.venv/bin/python -m ruff check src/awareness/normalize/quality.py tests/unit/test_quality.py` — no NEW errors.
- [ ] **Step 7: Commit:**
```bash
git add src/awareness/normalize/quality.py tests/unit/test_quality.py
git commit -m "feat(quality): Gopher/C4 content-quality heuristics for WET text"
```

---

### Task 3: Wire the quality filter into the CC-WET pipeline

**Files:**
- Modify: `src/awareness/config/settings.py` (add `wet_quality_filter`)
- Modify: `src/awareness/sources/commoncrawl_wet.py` (add `_record_passes_quality` helper; call it in `_parse_wet_to_captures`)
- Test (create): `tests/unit/test_cc_wet_quality.py`

- [ ] **Step 1: Write the failing test** — create `tests/unit/test_cc_wet_quality.py`:

```python
"""WET records below Gopher/C4 quality are dropped when the filter is on.

Mirrors the codebase's WET-helper test convention (cf. test_cc_wet_domain_filter):
the per-record decision lives in a pure helper, tested directly without WARC I/O.
"""

from __future__ import annotations

from awareness.config.settings import Settings
from awareness.sources.commoncrawl_wet import _record_passes_quality


def _clean() -> str:
    return (
        "The committee reviewed the annual report and approved the budget "
        "with broad support from the members that attended the meeting. "
    ) * 4


def test_quality_filter_default_is_on() -> None:
    assert Settings().wet_quality_filter is True


def test_clean_record_passes_when_enabled() -> None:
    assert _record_passes_quality(_clean(), enabled=True) is True


def test_junk_record_is_dropped_when_enabled() -> None:
    assert _record_passes_quality("buy now buy now", enabled=True) is False


def test_disabled_filter_passes_everything() -> None:
    assert _record_passes_quality("buy now buy now", enabled=False) is True
```

- [ ] **Step 2: Run, confirm FAIL** (`ImportError: cannot import name '_record_passes_quality'` and/or `AttributeError: ... 'wet_quality_filter'`):
`PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_cc_wet_quality.py -q`

- [ ] **Step 3a: Implement the setting** — in `src/awareness/config/settings.py`, add this field directly after `cc_wet_max_shards_per_crawl` (~line 103):
```python
    wet_quality_filter: bool = True  # drop Gopher/C4-low-quality WET records before storing
```

- [ ] **Step 3b: Implement the helper** — in `src/awareness/sources/commoncrawl_wet.py`, add the import near the other `normalize` imports (the file already does `from awareness.normalize.text import detect_language, normalize_text, safe_title`):
```python
from awareness.normalize.quality import gopher_quality
```
and add this helper next to `_record_passes_domain_filter` (~line 85):
```python
def _record_passes_quality(text: str, *, enabled: bool) -> bool:
    """WET records below Gopher/C4 content quality are dropped when ``enabled``."""
    if not enabled:
        return True
    return gopher_quality(text).ok
```

- [ ] **Step 3c: Wire it into the parse loop** — in `_parse_wet_to_captures`, immediately after the existing `if norm.discarded_reason: continue` block and BEFORE `lang = detect_language(norm.text) or None`, insert:
```python
            if not _record_passes_quality(norm.text, enabled=settings.wet_quality_filter):
                get_metrics().inc("cc_wet.quality_filtered")
                continue
```
(`settings` is already in scope — the loop already reads `settings.text_min_chars`; `get_metrics` is already imported at the top of the file.)

- [ ] **Step 4: Confirm PASS:** `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_cc_wet_quality.py -q`
- [ ] **Step 5: Full-suite gate:** `PYTHONPATH=src .venv/bin/python -m pytest -q -m "not slow and not smoke"` (expect 279 = 275 + 4). If any existing CC-WET test fed short/junky fixture text through `_parse_wet_to_captures` and now sees it filtered, READ it, confirm the drop is correct, and either raise its fixture above the quality bar or pass `wet_quality_filter=False`, noting it under Deviations.
- [ ] **Step 6: Ruff:** `.venv/bin/python -m ruff check src/awareness/config/settings.py src/awareness/sources/commoncrawl_wet.py tests/unit/test_cc_wet_quality.py` — no NEW errors.
- [ ] **Step 7: Commit:**
```bash
git add src/awareness/config/settings.py src/awareness/sources/commoncrawl_wet.py tests/unit/test_cc_wet_quality.py
git commit -m "feat(cc-wet): drop Gopher/C4-low-quality records before storing"
```

---

## Plan-level self-review checklist

- [ ] Full suite green (expect 279).
- [ ] LID is confidence-aware: `detect_language_conf` returns `(lang, conf)`; `detect_language` suppresses sub-threshold guesses to `None`; both pre-existing `test_detect_language_*` tests still pass; detection is deterministic (`DetectorFactory.seed = 0`).
- [ ] No new dependency added (langdetect was already a dep); detection stays behind the `detect_language*` surface for a future backend swap.
- [ ] `gopher_quality` is pure and total (handles empty/short text via the word-count gate); gate order is documented and each reason is covered by a test.
- [ ] WET filter is opt-out via `settings.wet_quality_filter` (default on), increments `cc_wet.quality_filtered`, and is applied before language detection; the decision lives in the testable `_record_passes_quality` helper.
- [ ] No NEW ruff errors on the touched files.

## Spec coverage note

Delivers spec Plan 4: confidence-aware LID + length gating (`min_chars`) + trust-FineWeb-metadata (already in `fineweb.py`/`local_fixture.py`) and a Gopher/C4 content-quality filter for WET. Out of scope here (future): a stronger LID backend (fastText/CLD3) behind the same surface; applying the quality filter to non-WET sources; corpus-level quality benchmarking (belongs with Plan 5).
