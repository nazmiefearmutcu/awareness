"""Deterministic synthetic corpus generators for the benchmark suite.

Three products:

* :func:`make_documents`      — flat list of prose docs (throughput tests).
* :func:`make_near_dup_dataset` — clustered docs + ground-truth near-dup
  pairs (accuracy tests: precision / recall / F1).
* :func:`make_html_pages`     — article text wrapped in realistic page
  boilerplate (extraction tests).

Design principles (so the accuracy benchmark is *fair*, not tuned):

* **High lexical diversity.** Sentences are assembled from large word banks
  via templates, so two unrelated documents share few tokens — mirroring
  real web text where distinct articles are genuinely distinct. (An earlier
  draft drew from a 40-sentence pool; unrelated docs then overlapped ~13%,
  artificially depressing *every* method's precision.)
* **Realistic near-dup edits.** A near-duplicate of a document applies a
  small, bounded fraction of word-level edits (synonym swap / typo / token
  duplication), spread across a realistic intensity range (≈2–9%), matching
  the standard near-dup definition (small edits) used by CORE / NEWS-COPY —
  not heavy structural rewrites.

Everything is seeded; the same seed always yields byte-identical output, so
anyone cloning the repo reproduces the numbers (modulo hardware).
"""

from __future__ import annotations

import random
from dataclasses import dataclass

# ── word banks for high-diversity sentence assembly ──────────────────────────
_ADJ = (
    "ancient", "brittle", "coastal", "distant", "eager", "frozen", "gilded", "hollow",
    "idle", "jagged", "luminous", "muted", "narrow", "opaque", "porous", "quiet",
    "restless", "saline", "tidal", "umber", "velvet", "weathered", "amber", "boreal",
    "crimson", "dormant", "ember", "feral", "granular", "humid", "inland", "kelp",
    "lichen", "marsh", "northern", "obsidian", "pelagic", "rugged", "shale", "temperate",
)
_SUBJ = (
    "committee", "glacier", "parser", "analyst", "estuary", "protocol", "farmer",
    "scheduler", "archivist", "turbine", "senator", "volunteer", "cache", "ornithologist",
    "founder", "harbor", "reservoir", "orchestra", "engineer", "festival", "tokenizer",
    "expedition", "regulator", "fisherman", "gardener", "pipeline", "mayor", "beacon",
    "auditor", "surveyor", "botanist", "ledger", "courier", "lighthouse", "mason",
    "cartographer", "vintner", "geologist", "weaver", "shipwright",
)
_VERB = (
    "charted", "buffered", "eroded", "negotiated", "delayed", "compressed", "restored",
    "flagged", "returned", "raised", "grounded", "traced", "praised", "audited",
    "accumulated", "rebuilt", "urged", "promised", "mapped", "doubled", "normalized",
    "switched", "opened", "deduplicated", "described", "flooded", "scaled", "repaved",
    "repeated", "tolerated", "drew", "ground", "dropped", "surveyed", "ferried",
    "anchored", "pressed", "measured", "wove", "forged",
)
_OBJ = (
    "framework", "sediment", "interface", "volatility", "secret", "season", "manuscript",
    "ratio", "society", "round", "corridor", "saturation", "pacing", "controls",
    "passes", "queue", "reservoir", "movement", "outage", "crowd", "whitespace",
    "system", "consultation", "dataset", "memory", "seedlings", "bottleneck", "avenue",
    "signal", "input", "lectures", "flour", "round-trips", "ridge", "alloy",
    "frames", "harvest", "channel", "lattice", "ballast",
)
_PLACE = (
    "the northern coast", "the valley", "the high passes", "the old mill", "the ridge",
    "the eastern corridor", "the riverbank", "the reservoir", "the harbor", "the archive",
    "the highlands", "the delta", "the foothills", "the basin", "the promenade",
    "the quarry", "the wetlands", "the terminal", "the orchard", "the boatyard",
)
_TIME = (
    "on Tuesday", "over the past decade", "this quarter", "last spring", "every morning",
    "after the thaw", "before the frost", "throughout the season", "by midweek",
    "within the hour", "since the merger", "until nightfall", "across the year",
)
_TEMPLATES = (
    "The {adj} {subj} {verb} the {adj2} {obj} near {place} {time}.",
    "{time}, the {subj} {verb} a {adj} {obj} across {place}.",
    "Near {place}, a {adj} {subj} {verb} the {obj} {time}.",
    "Analysts noted that the {subj} {verb} the {adj} {obj} {time}.",
    "The {adj} {obj} was {verb} by the {subj} near {place}.",
    "A {adj} {subj} {verb} {obj} and {adj2} {obj2} {time}.",
)

_SYNONYMS: dict[str, str] = {
    "ancient": "aging", "distant": "remote", "quiet": "subdued", "narrow": "slim",
    "charted": "mapped", "delayed": "postponed", "raised": "lifted", "praised": "lauded",
    "doubled": "redoubled", "described": "depicted", "promised": "pledged",
    "framework": "scaffold", "society": "guild", "outage": "blackout", "crowd": "throng",
    "season": "stretch", "signal": "beacon", "system": "apparatus", "harvest": "yield",
}


def _sentence(rng: random.Random) -> str:
    return rng.choice(_TEMPLATES).format(
        adj=rng.choice(_ADJ), adj2=rng.choice(_ADJ),
        subj=rng.choice(_SUBJ), verb=rng.choice(_VERB),
        obj=rng.choice(_OBJ), obj2=rng.choice(_OBJ),
        place=rng.choice(_PLACE), time=rng.choice(_TIME),
    )


def _make_doc(rng: random.Random, n_sentences: int) -> str:
    return " ".join(_sentence(rng) for _ in range(n_sentences))


def make_documents(
    n_docs: int = 5_000, *, min_sentences: int = 6, max_sentences: int = 24, seed: int = 1234
) -> list[str]:
    """A flat list of independent prose documents (for throughput tests)."""
    rng = random.Random(seed)
    return [_make_doc(rng, rng.randint(min_sentences, max_sentences)) for _ in range(n_docs)]


def _perturb(rng: random.Random, text: str, *, intensity: float) -> str:
    """Return a NEAR-duplicate of ``text`` via a small fraction of word edits.

    ``intensity`` (~0.02–0.09) is the fraction of words edited: a synonym
    swap, a realistic adjacent-character typo, or a duplicated token. Edits
    are word-level only (no whole-sentence add/drop) so the result is a
    genuine near-dup in the standard sense, not a structural rewrite.
    """
    words = text.split(" ")
    n_edits = max(1, round(len(words) * intensity))
    idxs = rng.sample(range(len(words)), min(n_edits, len(words)))
    for i in idxs:
        w = words[i]
        lw = w.lower().strip(".,")
        roll = rng.random()
        if lw in _SYNONYMS and roll < 0.5:
            words[i] = _SYNONYMS[lw] + w[len(w.rstrip(".,")):]  # keep trailing punctuation
        elif len(lw) > 4 and roll < 0.75:
            j = rng.randrange(len(w) - 1)
            words[i] = w[:j] + w[j + 1] + w[j] + w[j + 2:]  # adjacent-char transpose typo
        else:
            words[i] = w + " " + w  # token duplication (common copy artifact)
    return " ".join(words)


@dataclass(slots=True)
class NearDupDataset:
    docs: list[str]
    cluster_of: list[int]              # cluster_of[i] == cluster id of docs[i]
    true_pairs: set[tuple[int, int]]   # canonical (i<j) pairs that are near-dups

    @property
    def n_docs(self) -> int:
        return len(self.docs)


# A realistic spread of near-dup edit intensities (light → moderate).
_INTENSITY_SPREAD = (0.02, 0.03, 0.05, 0.07, 0.09)


def make_near_dup_dataset(
    *,
    n_clusters: int = 300,
    variants_per_cluster: int = 3,
    n_singletons: int = 700,
    intensity: float | None = None,
    seed: int = 7,
) -> NearDupDataset:
    """Build a labelled near-dup dataset.

    Each *cluster* is one base document plus ``variants_per_cluster`` near-dups
    of it. ``intensity`` fixes the edit fraction for every variant; when None
    (default) it is drawn per-variant from a realistic spread. ``n_singletons``
    independent documents are added as distractors. Ground truth: a pair
    ``(i, j)`` is a near-dup iff both belong to the same multi-member cluster.
    """
    rng = random.Random(seed)
    docs: list[str] = []
    cluster_of: list[int] = []
    cid = 0
    for _ in range(n_clusters):
        base = _make_doc(rng, rng.randint(10, 22))
        docs.append(base)
        cluster_of.append(cid)
        for _ in range(variants_per_cluster):
            amt = intensity if intensity is not None else rng.choice(_INTENSITY_SPREAD)
            docs.append(_perturb(rng, base, intensity=amt))
            cluster_of.append(cid)
        cid += 1
    for _ in range(n_singletons):
        docs.append(_make_doc(rng, rng.randint(10, 22)))
        cluster_of.append(cid)
        cid += 1

    # Shuffle so cluster members are not adjacent (avoids ordering artifacts).
    order = list(range(len(docs)))
    rng.shuffle(order)
    docs = [docs[i] for i in order]
    cluster_of = [cluster_of[i] for i in order]

    from collections import defaultdict

    members_by_cluster: dict[int, list[int]] = defaultdict(list)
    for idx, c in enumerate(cluster_of):
        members_by_cluster[c].append(idx)
    true_pairs: set[tuple[int, int]] = set()
    for members in members_by_cluster.values():
        if len(members) < 2:
            continue
        for a_pos in range(len(members)):
            for b_pos in range(a_pos + 1, len(members)):
                i, j = members[a_pos], members[b_pos]
                true_pairs.add((i, j) if i < j else (j, i))
    return NearDupDataset(docs=docs, cluster_of=cluster_of, true_pairs=true_pairs)


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{title} — The Daily Ledger</title>
  <meta name="description" content="{title}">
  <link rel="stylesheet" href="/static/site.css">
  <script>window.dataLayer=window.dataLayer||[];</script>
</head>
<body>
  <header class="masthead">
    <nav><a href="/">Home</a> · <a href="/world">World</a> · <a href="/tech">Tech</a> · <a href="/markets">Markets</a></nav>
    <form class="search"><input placeholder="Search the archive"><button>Go</button></form>
  </header>
  <div class="ad-leaderboard">Advertisement — Buy now and save 20% on annual plans!</div>
  <main>
    <article>
      <h1>{title}</h1>
      <p class="byline">By A. Correspondent · Updated 3 hours ago · 4 min read</p>
      {paragraphs}
    </article>
    <aside class="related">
      <h3>Related stories</h3>
      <ul><li><a href="/a">Markets wobble</a></li><li><a href="/b">A quiet revolution</a></li></ul>
    </aside>
  </main>
  <footer>
    <p>© 2026 The Daily Ledger. All rights reserved. <a href="/privacy">Privacy</a> · <a href="/terms">Terms</a></p>
    <div class="social">Share on social media. Subscribe to our newsletter for daily updates.</div>
  </footer>
  <script src="/static/analytics.js"></script>
</body>
</html>"""


@dataclass(slots=True)
class HtmlPage:
    html: str
    gold_text: str  # the article body we expect a good extractor to recover


def make_html_pages(n: int = 400, *, seed: int = 99) -> list[HtmlPage]:
    """Article text wrapped in realistic boilerplate (nav/ads/footer)."""
    rng = random.Random(seed)
    pages: list[HtmlPage] = []
    for _ in range(n):
        title = _sentence(rng).rstrip(".")
        body_sentences = [_sentence(rng) for _ in range(rng.randint(8, 18))]
        paras: list[list[str]] = []
        cur: list[str] = []
        for s in body_sentences:
            cur.append(s)
            if len(cur) >= rng.randint(2, 4):
                paras.append(cur)
                cur = []
        if cur:
            paras.append(cur)
        gold_text = "\n\n".join(" ".join(p) for p in paras)
        html = _HTML_TEMPLATE.format(
            title=title,
            paragraphs="\n      ".join(f"<p>{' '.join(p)}</p>" for p in paras),
        )
        pages.append(HtmlPage(html=html, gold_text=gold_text))
    return pages


if __name__ == "__main__":
    docs = make_documents(10)
    ds = make_near_dup_dataset(n_clusters=5, n_singletons=5)
    pages = make_html_pages(3)
    total_bytes = sum(len(d.encode()) for d in docs)
    print(f"documents: {len(docs)}  bytes={total_bytes}")
    print(f"near-dup: {ds.n_docs} docs, {len(ds.true_pairs)} true pairs")
    print(f"html: {len(pages)} pages, first gold {len(pages[0].gold_text)} chars")
    print("sample sentence:", _sentence(__import__('random').Random(1)))
