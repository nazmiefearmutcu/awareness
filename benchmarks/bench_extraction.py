"""Suite: HTML → main-text extraction (quality + throughput).

Awareness extracts article text with **trafilatura** (see
``awareness.normalize.html.html_to_text``), the library that tops the
standard extraction benchmark. Two views:

* **Measured, same-corpus quality** — word-level F1 of each extractor's
  output against the known article body, on our synthetic pages that wrap
  the article in realistic boilerplate (nav / ads / "related" / footer).
  This tests boilerplate rejection (precision) directly.
* **Measured throughput** — pages/sec through each extractor.
* **Published external benchmark** — the Barbaresi (trafilatura author)
  750-document leaderboard, cited for external validity, so the synthetic
  numbers aren't the only evidence.

Higher F1 / higher docs-per-sec is better.
"""

from __future__ import annotations

import re
from collections import Counter

from awareness.normalize.html import html_to_text

from .corpus import make_html_pages
from .harness import Entry, Suite, throughput, time_callable

_WORD = re.compile(r"[0-9a-z]+")


def _word_f1(pred: str, gold: str) -> float:
    pw = Counter(_WORD.findall((pred or "").lower()))
    gw = Counter(_WORD.findall((gold or "").lower()))
    if not pw or not gw:
        return 0.0
    overlap = sum((pw & gw).values())
    precision = overlap / sum(pw.values())
    recall = overlap / sum(gw.values())
    return (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0


# ── extractor adapters (return plain text or "") ─────────────────────────────
def _awareness_extract(html: str) -> str:
    ex = html_to_text(html, min_chars=1)
    return ex.text.text if ex else ""


def _readability_extract():
    try:
        from lxml import html as lhtml
        from readability import Document  # readability-lxml

        def fn(html: str) -> str:
            try:
                summary = Document(html).summary()
                return lhtml.fromstring(summary).text_content()
            except Exception:
                return ""

        return fn
    except Exception:
        return None


def _html2text_extract():
    try:
        import html2text

        h = html2text.HTML2Text()
        h.ignore_links = True
        h.ignore_images = True

        def fn(html: str) -> str:
            try:
                return h.handle(html)
            except Exception:
                return ""

        return fn
    except Exception:
        return None


def _inscriptis_extract():
    try:
        from inscriptis import get_text

        def fn(html: str) -> str:
            try:
                return get_text(html)
            except Exception:
                return ""

        return fn
    except Exception:
        return None


def _raw_lxml_extract():
    try:
        from lxml import html as lhtml

        def fn(html: str) -> str:
            try:
                return lhtml.fromstring(html).text_content()
            except Exception:
                return ""

        return fn
    except Exception:
        return None


def run(repeats: int = 3) -> list[Suite]:
    pages = make_html_pages(400, seed=99)
    htmls = [p.html for p in pages]
    golds = [p.gold_text for p in pages]
    suites: list[Suite] = []

    competitors: list[tuple[str, object, bool, str]] = [
        ("trafilatura (Awareness)", _awareness_extract, True, "the extractor Awareness uses"),
        ("readability-lxml", _readability_extract(), False, ""),
        ("inscriptis", _inscriptis_extract(), False, ""),
        ("html2text", _html2text_extract(), False, ""),
        ("raw lxml text()", _raw_lxml_extract(), False, "no boilerplate removal"),
    ]
    competitors = [(n, f, a, note) for (n, f, a, note) in competitors if f is not None]

    # ── measured same-corpus quality (word F1) ───────────────────────────
    quality = Suite(
        key="extraction_quality",
        title="HTML→text extraction quality (measured)",
        metric="Word-level F1 (0–1)",
        higher_is_better=True,
        subtitle=f"{len(pages)} synthetic pages with nav/ads/footer boilerplate; F1 vs known article body",
    )
    tput = Suite(
        key="extraction_throughput",
        title="HTML→text extraction throughput",
        metric="Throughput (pages/s)",
        higher_is_better=True,
        subtitle=f"{len(pages)} pages through each extractor",
    )
    for name, fn, is_aw, note in competitors:
        f1s = [_word_f1(fn(h), g) for h, g in zip(htmls, golds, strict=True)]
        avg_f1 = sum(f1s) / len(f1s)
        quality.add(Entry(name, avg_f1, "F1", is_awareness=is_aw, note=note))
        secs = time_callable(lambda fn=fn: [fn(h) for h in htmls], repeats=repeats)
        tput.add(Entry(name, throughput(len(htmls), secs), "pages/s", is_awareness=is_aw, note=note))
    suites.append(quality)
    suites.append(tput)

    # ── published external benchmark (cited, NOT re-measured) ────────────
    pub = Suite(
        key="extraction_published",
        title="Extraction F1 — published benchmark",
        metric="F1 score (0–1)",
        higher_is_better=True,
        subtitle="Barbaresi 2022 leaderboard, 750-doc gold corpus (external validity)",
    )
    # Source: https://trafilatura.readthedocs.io/en/latest/evaluation.html
    # 2022-05-18 corpus, 750 docs (2236 text + 2250 boilerplate segments).
    # Verified F-scores transcribed verbatim from that table.
    for name, f1, is_aw in [
        ("trafilatura (Awareness)", 0.909, True),
        ("readabilipy", 0.874, False),
        ("news-please", 0.808, False),
        ("readability-lxml", 0.801, False),
        ("goose3", 0.793, False),
        ("justext", 0.742, False),
        ("inscriptis", 0.686, False),
        ("html2text", 0.577, False),
    ]:
        pub.add(Entry(name, f1, "F1", is_awareness=is_aw, note="published (Barbaresi 2022)"))
    suites.append(pub)

    return suites


if __name__ == "__main__":
    for s in run():
        print(f"\n== {s.title} — {s.metric} ==")
        for e in sorted(s.entries, key=lambda x: -x.value):
            tag = " *" if e.is_awareness else "  "
            print(f"{tag} {e.name:28s} {e.value:8.3f} {e.unit:8s} {e.note}")
