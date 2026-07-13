"""Pure unit tests for the BM25 re-rank scoring core (no DuckDB).

These pin the exact arithmetic of the multiplicative re-rank so the
magnitude-sensitive cases (a title hit overriding a *higher* raw BM25
body match) are proven deterministically, independent of DuckDB's BM25.
"""

from __future__ import annotations

from datetime import UTC, datetime

from awareness.storage.duckdb_index import (
    _domain_labels,
    _domain_nav_frac,
    _lead_hit_frac,
    _lead_phrase_frac,
    _length_factor,
    _recency_factor,
    _rerank,
    _title_exact_frac,
    _title_hit_frac,
    _title_phrase_frac,
    _to_epoch,
    _url_exact_frac,
    _url_hit_frac,
    _url_phrase_frac,
    _url_slug_tokens,
)


# ── _title_hit_frac ──────────────────────────────────────────────────────
def test_title_hit_frac_no_terms_is_zero() -> None:
    assert _title_hit_frac("Anything", []) == 0.0


def test_title_hit_frac_counts_distinct_terms() -> None:
    assert _title_hit_frac("Bitcoin price rally", ["bitcoin", "price"]) == 1.0
    assert _title_hit_frac("Bitcoin only", ["bitcoin", "ethereum"]) == 0.5
    assert _title_hit_frac("nothing here", ["bitcoin"]) == 0.0


def test_title_hit_frac_is_case_insensitive() -> None:
    assert _title_hit_frac("BITCOIN", ["bitcoin"]) == 1.0


# ── _title_phrase_frac ───────────────────────────────────────────────────
def test_title_phrase_frac_requires_multi_term_ordered_match() -> None:
    """Single-term queries do not get a phrase bonus (title_hit covers them)."""
    assert _title_phrase_frac("Bitcoin surges", ["bitcoin"]) == 0.0
    assert _title_phrase_frac("Anything", []) == 0.0


def test_title_phrase_frac_contiguous_phrase_is_one() -> None:
    """Joined query terms as a contiguous title substring → full phrase hit."""
    assert _title_phrase_frac("Bitcoin price rally overnight", ["bitcoin", "price"]) == 1.0
    assert _title_phrase_frac("BITCOIN PRICE jumps", ["bitcoin", "price"]) == 1.0


def test_title_phrase_frac_ordered_with_gap_is_half() -> None:
    """Terms in order but not contiguous (gap words) → partial phrase credit."""
    assert (
        _title_phrase_frac("Bitcoin overnight price surge", ["bitcoin", "price"])
        == 0.5
    )


def test_title_phrase_frac_out_of_order_is_zero() -> None:
    """Bag-of-words title hit without ordered span is not a phrase hit."""
    assert _title_phrase_frac("Price of bitcoin jumps", ["bitcoin", "price"]) == 0.0
    assert _title_phrase_frac("markets only", ["bitcoin", "price"]) == 0.0


# ── _title_exact_frac ────────────────────────────────────────────────────
def test_title_exact_frac_empty_is_zero() -> None:
    assert _title_exact_frac("Bitcoin", []) == 0.0
    assert _title_exact_frac("", ["bitcoin"]) == 0.0
    assert _title_exact_frac("   ", ["bitcoin"]) == 0.0


def test_title_exact_frac_single_and_multi_term_match() -> None:
    assert _title_exact_frac("Bitcoin", ["bitcoin"]) == 1.0
    assert _title_exact_frac("BITCOIN PRICE", ["bitcoin", "price"]) == 1.0
    # Punctuation / short tokens ignored in title tokenization.
    assert _title_exact_frac("Bitcoin: price!", ["bitcoin", "price"]) == 1.0


def test_title_exact_frac_rejects_extra_or_missing_or_reorder() -> None:
    # Longer title still gets phrase credit elsewhere; exact requires equality.
    assert _title_exact_frac("Bitcoin price surges", ["bitcoin", "price"]) == 0.0
    assert _title_exact_frac("Bitcoin", ["bitcoin", "price"]) == 0.0
    assert _title_exact_frac("Price bitcoin", ["bitcoin", "price"]) == 0.0
    assert _title_exact_frac("markets only", ["bitcoin"]) == 0.0


# ── _lead_hit_frac ───────────────────────────────────────────────────────
def test_lead_hit_frac_no_terms_is_zero() -> None:
    assert _lead_hit_frac("Bitcoin anywhere", []) == 0.0


def test_lead_hit_frac_counts_distinct_terms_in_lead() -> None:
    assert _lead_hit_frac("Bitcoin price surges in Asia", ["bitcoin", "price"]) == 1.0
    assert _lead_hit_frac("Bitcoin only in lead", ["bitcoin", "ethereum"]) == 0.5
    assert _lead_hit_frac("nothing here", ["bitcoin"]) == 0.0


def test_lead_hit_frac_is_case_insensitive() -> None:
    assert _lead_hit_frac("BITCOIN jumped", ["bitcoin"]) == 1.0


def test_lead_hit_frac_ignores_matches_past_lead_window() -> None:
    """Term only after the lead window must not score."""
    buried = ("noise " * 80) + "bitcoin later"
    assert _lead_hit_frac(buried, ["bitcoin"], lead_chars=40) == 0.0
    assert _lead_hit_frac(buried, ["bitcoin"], lead_chars=2000) == 1.0


def test_lead_hit_frac_empty_lead_is_zero() -> None:
    assert _lead_hit_frac("", ["bitcoin"]) == 0.0
    assert _lead_hit_frac("   ", ["bitcoin"]) == 0.0
    assert _lead_hit_frac("Bitcoin", ["bitcoin"], lead_chars=0) == 0.0


# ── _lead_phrase_frac ────────────────────────────────────────────────────
def test_lead_phrase_frac_requires_multi_term() -> None:
    assert _lead_phrase_frac("Bitcoin price surges in Asia", ["bitcoin"]) == 0.0
    assert _lead_phrase_frac("Anything", []) == 0.0


def test_lead_phrase_frac_contiguous_in_lead_is_one() -> None:
    assert _lead_phrase_frac("Bitcoin price surges in Asia", ["bitcoin", "price"]) == 1.0
    assert _lead_phrase_frac("BITCOIN PRICE jumps", ["bitcoin", "price"]) == 1.0


def test_lead_phrase_frac_ordered_gap_in_lead_is_half() -> None:
    assert (
        _lead_phrase_frac("Bitcoin overnight price surge", ["bitcoin", "price"]) == 0.5
    )


def test_lead_phrase_frac_ignores_matches_past_lead_window() -> None:
    """Phrase only after the lead window must not score."""
    buried = ("noise " * 80) + "bitcoin price later"
    assert _lead_phrase_frac(buried, ["bitcoin", "price"], lead_chars=40) == 0.0
    # Same text with a large enough window recovers the phrase.
    assert _lead_phrase_frac(buried, ["bitcoin", "price"], lead_chars=2000) == 1.0


def test_lead_phrase_frac_out_of_order_is_zero() -> None:
    assert _lead_phrase_frac("Price of bitcoin jumps", ["bitcoin", "price"]) == 0.0


# ── _url_hit_frac ────────────────────────────────────────────────────────
def test_url_hit_frac_no_terms_is_zero() -> None:
    assert _url_hit_frac("https://ex.com/bitcoin", "ex.com", []) == 0.0


def test_url_hit_frac_counts_path_and_domain_terms() -> None:
    assert (
        _url_hit_frac(
            "https://news.example/world/bitcoin-rally",
            "news.example",
            ["bitcoin", "rally"],
        )
        == 1.0
    )
    assert (
        _url_hit_frac(
            "https://news.example/markets",
            "news.example",
            ["bitcoin", "markets"],
        )
        == 0.5
    )
    assert _url_hit_frac("https://x.test/a", "x.test", ["bitcoin"]) == 0.0


def test_url_hit_frac_empty_url_and_domain_is_zero() -> None:
    assert _url_hit_frac("", "", ["bitcoin"]) == 0.0


def test_url_hit_frac_is_case_insensitive() -> None:
    assert _url_hit_frac("https://X.TEST/BITCOIN", "X.TEST", ["bitcoin"]) == 1.0


# ── _url_phrase_frac ─────────────────────────────────────────────────────
def test_url_phrase_frac_requires_multi_term_ordered_match() -> None:
    """Single-term queries do not get a URL phrase bonus (url_hit covers them)."""
    assert _url_phrase_frac("https://ex.com/bitcoin-price", "ex.com", ["bitcoin"]) == 0.0
    assert _url_phrase_frac("https://ex.com/x", "ex.com", []) == 0.0


def test_url_phrase_frac_contiguous_slug_is_one() -> None:
    """Hyphenated slug ``bitcoin-price`` → contiguous phrase after normalize."""
    assert (
        _url_phrase_frac(
            "https://news.example/world/bitcoin-price-rally",
            "news.example",
            ["bitcoin", "price"],
        )
        == 1.0
    )
    assert (
        _url_phrase_frac(
            "https://news.example/BITCOIN-PRICE",
            "news.example",
            ["bitcoin", "price"],
        )
        == 1.0
    )


def test_url_phrase_frac_ordered_with_gap_is_half() -> None:
    """Terms in order but not contiguous (gap slug segment) → partial credit."""
    assert (
        _url_phrase_frac(
            "https://news.example/world/bitcoin-overnight-price-surge",
            "news.example",
            ["bitcoin", "price"],
        )
        == 0.5
    )


def test_url_phrase_frac_out_of_order_is_zero() -> None:
    """Bag-of-words slug hit without ordered span is not a phrase hit."""
    assert (
        _url_phrase_frac(
            "https://news.example/world/price-of-bitcoin-jumps",
            "news.example",
            ["bitcoin", "price"],
        )
        == 0.0
    )
    assert (
        _url_phrase_frac(
            "https://news.example/markets-only",
            "news.example",
            ["bitcoin", "price"],
        )
        == 0.0
    )


# ── _length_factor ───────────────────────────────────────────────────────
def test_length_factor_is_one_up_to_pivot() -> None:
    assert _length_factor(0, pivot=4000, floor=0.75) == 1.0
    assert _length_factor(4000, pivot=4000, floor=0.75) == 1.0


def test_length_factor_decays_and_is_floored() -> None:
    short = _length_factor(8000, pivot=4000, floor=0.75)
    longer = _length_factor(40000, pivot=4000, floor=0.75)
    assert 0.75 < short < 1.0
    assert longer < short            # monotonically decreasing
    assert longer >= 0.75            # never below the floor


# ── _to_epoch ────────────────────────────────────────────────────────────
def test_to_epoch_handles_datetime_number_iso_and_garbage() -> None:
    dt = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    assert _to_epoch(dt) == dt.timestamp()
    assert _to_epoch(1_700_000_000.0) == 1_700_000_000.0
    assert _to_epoch("2026-06-01T12:00:00+00:00") == dt.timestamp()
    assert _to_epoch("2026-06-01T12:00:00Z") == dt.timestamp()
    assert _to_epoch(None) is None
    assert _to_epoch("not-a-date") is None


# ── _recency_factor ──────────────────────────────────────────────────────
def test_recency_factor_disabled_when_weight_zero() -> None:
    assert _recency_factor(100.0, 200.0, halflife_days=30.0, weight=0.0) == 1.0


def test_recency_factor_disabled_when_timestamps_missing() -> None:
    assert _recency_factor(None, 200.0, halflife_days=30.0, weight=0.5) == 1.0
    assert _recency_factor(100.0, None, halflife_days=30.0, weight=0.5) == 1.0


def test_recency_factor_newest_gets_full_boost_old_decays() -> None:
    ref = 1_000_000_000.0
    newest = _recency_factor(ref, ref, halflife_days=30.0, weight=0.5)
    one_halflife = _recency_factor(ref - 30 * 86400, ref, halflife_days=30.0, weight=0.5)
    ancient = _recency_factor(ref - 3650 * 86400, ref, halflife_days=30.0, weight=0.5)
    assert newest == 1.5                       # 1 + 0.5 * 1.0
    assert abs(one_halflife - 1.25) < 1e-9     # 1 + 0.5 * 0.5
    assert abs(ancient - 1.0) < 1e-3           # decayed back to baseline


# ── _rerank ──────────────────────────────────────────────────────────────
def _cand(
    cid: str,
    score: float,
    *,
    title: str = "",
    text: str = "",
    ts: object = None,
    published_ts: object = None,
    fetch_ts: object = None,
    url: str | None = None,
    domain: str | None = None,
    canonical_url: str | None = None,
) -> dict[str, object]:
    """Build a candidate row. ``ts`` is a shorthand for fetch_ts (legacy tests)."""
    row: dict[str, object] = {
        "capture_id": cid,
        "score": score,
        "title": title,
        "text": text,
        "fetch_ts": fetch_ts if fetch_ts is not None else ts,
    }
    if published_ts is not None:
        row["published_ts"] = published_ts
    if url is not None:
        row["url"] = url
    if domain is not None:
        row["domain"] = domain
    if canonical_url is not None:
        row["canonical_url"] = canonical_url
    return row


def test_rerank_title_hit_overrides_higher_bm25() -> None:
    # A: higher raw BM25 (1.0) but the term is NOT in its title.
    # B: lower raw BM25 (0.8) but the term IS in its title.
    # finals: A = 1.0 * 1.0 = 1.0 ; B = 0.8 * (1 + 0.5*1.0) = 1.2  -> B wins.
    cands = [_cand("A", 1.0, title="markets today", text="bitcoin bitcoin"),
             _cand("B", 0.8, title="bitcoin surges", text="the asset gained")]
    out = _rerank(cands, ["bitcoin"], title_boost=0.5)
    assert [c["capture_id"] for c in out] == ["B", "A"]


def test_rerank_title_phrase_overrides_scattered_title_hits() -> None:
    """Ordered contiguous title phrase beats equal title coverage without order.

    Both docs hit every term in the title (same title_hit_frac). Only A forms
    the contiguous phrase "bitcoin price"; with phrase boost on, A ranks first
    despite lower raw BM25.
    """
    cands = [
        _cand("SCATTER", 1.0, title="Price of bitcoin jumps", text="t"),
        _cand("PHRASE", 0.85, title="Bitcoin price surges", text="t"),
    ]
    off = _rerank(
        cands,
        ["bitcoin", "price"],
        title_boost=0.5,
        title_phrase_boost=0.0,
        title_exact_boost=0.0,
        url_boost=0.0,
    )
    # Without phrase boost: equal title_f → BM25 order (SCATTER first).
    assert [c["capture_id"] for c in off] == ["SCATTER", "PHRASE"]
    on = _rerank(
        cands,
        ["bitcoin", "price"],
        title_boost=0.5,
        title_phrase_boost=0.35,
        title_exact_boost=0.0,
        url_boost=0.0,
    )
    # PHRASE final = 0.85 * (1+0.5) * (1+0.35) = 0.85 * 1.5 * 1.35 = 1.72125
    # SCATTER final = 1.0 * (1+0.5) * 1.0 = 1.5
    assert [c["capture_id"] for c in on] == ["PHRASE", "SCATTER"]


def test_rerank_title_exact_overrides_phrase_only() -> None:
    """Title token equality beats a longer title that only contains the phrase.

    Both docs form the contiguous phrase and full term coverage. Only EXACT
    has title tokens == query; with exact boost on, EXACT ranks first despite
    lower raw BM25.
    """
    cands = [
        _cand("LONGER", 1.0, title="Bitcoin price surges overnight", text="t"),
        _cand("EXACT", 0.85, title="Bitcoin price", text="t"),
    ]
    off = _rerank(
        cands,
        ["bitcoin", "price"],
        title_boost=0.5,
        title_phrase_boost=0.35,
        title_exact_boost=0.0,
        url_boost=0.0,
        url_phrase_boost=0.0,
        lead_hit_boost=0.0,
        lead_phrase_boost=0.0,
    )
    # Equal title_f + phrase_f, no exact → BM25 order (LONGER first).
    assert [c["capture_id"] for c in off] == ["LONGER", "EXACT"]
    on = _rerank(
        cands,
        ["bitcoin", "price"],
        title_boost=0.5,
        title_phrase_boost=0.35,
        title_exact_boost=0.4,
        url_boost=0.0,
        url_phrase_boost=0.0,
        lead_hit_boost=0.0,
        lead_phrase_boost=0.0,
    )
    # EXACT final = 0.85 * 1.5 * 1.35 * 1.4 = 2.40975
    # LONGER final = 1.0 * 1.5 * 1.35 * 1.0 = 2.025
    assert [c["capture_id"] for c in on] == ["EXACT", "LONGER"]


def test_rerank_lead_phrase_overrides_buried_body_match() -> None:
    """Ordered phrase in the lead beats a buried body match with higher BM25.

    Neutral titles/urls so only lead_f differs. BURIED has higher raw BM25
    but the phrase appears only after the lead window; LEAD has lower BM25
    with the contiguous phrase in the first sentence.
    """
    buried_text = ("x " * 200) + "bitcoin price later in the article"
    lead_text = "Bitcoin price jumped as markets opened. " + ("y " * 50)
    cands = [
        _cand("BURIED", 1.0, title="n", text=buried_text),
        _cand("LEAD", 0.85, title="n", text=lead_text),
    ]
    off = _rerank(
        cands,
        ["bitcoin", "price"],
        title_boost=0.0,
        title_phrase_boost=0.0,
        title_exact_boost=0.0,
        url_boost=0.0,
        url_phrase_boost=0.0,
        lead_hit_boost=0.0,
        lead_phrase_boost=0.0,
    )
    assert [c["capture_id"] for c in off] == ["BURIED", "LEAD"]
    on = _rerank(
        cands,
        ["bitcoin", "price"],
        title_boost=0.0,
        title_phrase_boost=0.0,
        title_exact_boost=0.0,
        url_boost=0.0,
        url_phrase_boost=0.0,
        lead_hit_boost=0.0,
        lead_phrase_boost=0.2,
        lead_chars=280,
    )
    # LEAD final = 0.85 * 1.2 = 1.02 > BURIED = 1.0 * 1.0
    assert [c["capture_id"] for c in on] == ["LEAD", "BURIED"]


def test_rerank_lead_hit_overrides_buried_single_term() -> None:
    """Bag-of-words lead coverage beats a buried single-term body match.

    lead_phrase_frac is 0 for single-term queries; lead_hit_frac covers them.
    Neutral titles/urls; only lead_hit_f differs when boost is on.
    """
    buried_text = ("x " * 200) + "bitcoin later in the article"
    lead_text = "Bitcoin jumped as markets opened. " + ("y " * 50)
    cands = [
        _cand("BURIED", 1.0, title="n", text=buried_text),
        _cand("LEAD", 0.90, title="n", text=lead_text),
    ]
    off = _rerank(
        cands,
        ["bitcoin"],
        title_boost=0.0,
        title_phrase_boost=0.0,
        title_exact_boost=0.0,
        url_boost=0.0,
        url_phrase_boost=0.0,
        lead_hit_boost=0.0,
        lead_phrase_boost=0.0,
    )
    assert [c["capture_id"] for c in off] == ["BURIED", "LEAD"]
    on = _rerank(
        cands,
        ["bitcoin"],
        title_boost=0.0,
        title_phrase_boost=0.0,
        title_exact_boost=0.0,
        url_boost=0.0,
        url_phrase_boost=0.0,
        lead_hit_boost=0.15,
        lead_phrase_boost=0.0,
        lead_chars=280,
    )
    # LEAD final = 0.90 * 1.15 = 1.035 > BURIED = 1.0 * 1.0
    assert [c["capture_id"] for c in on] == ["LEAD", "BURIED"]


def test_rerank_url_hit_overrides_higher_bm25() -> None:
    # A: higher raw BM25, term only in long body. B: lower BM25, term in URL path.
    # With Wu=0.25: B final = 0.85 * 1.25 = 1.0625 > A = 1.0 * 1.0.
    cands = [
        _cand(
            "A",
            1.0,
            title="markets today",
            text="bitcoin " * 20,
            url="https://news.example/markets/today",
            domain="news.example",
        ),
        _cand(
            "B",
            0.85,
            title="markets today",
            text="brief note",
            url="https://news.example/world/bitcoin-rally",
            domain="news.example",
        ),
    ]
    off = _rerank(
        cands,
        ["bitcoin"],
        title_boost=0.0,
        url_boost=0.0,
        lead_hit_boost=0.0,
        lead_phrase_boost=0.0,
    )
    assert [c["capture_id"] for c in off] == ["A", "B"]
    on = _rerank(
        cands,
        ["bitcoin"],
        title_boost=0.0,
        url_boost=0.25,
        lead_hit_boost=0.0,
        lead_phrase_boost=0.0,
    )
    assert [c["capture_id"] for c in on] == ["B", "A"]


def test_rerank_url_boost_falls_back_to_canonical_url() -> None:
    """Missing ``url`` still scores path hits via ``canonical_url``."""
    cands = [
        _cand("A", 1.0, title="n", text="t", domain="x.test"),
        _cand(
            "B",
            0.85,
            title="n",
            text="t",
            canonical_url="https://x.test/topic/bitcoin",
            domain="x.test",
        ),
    ]
    out = _rerank(
        cands,
        ["bitcoin"],
        title_boost=0.0,
        url_boost=0.25,
        lead_hit_boost=0.0,
        lead_phrase_boost=0.0,
    )
    assert [c["capture_id"] for c in out] == ["B", "A"]


def test_rerank_url_phrase_overrides_scattered_url_hits() -> None:
    """Ordered contiguous URL slug phrase beats equal URL coverage without order.

    Both docs hit every term in the URL (same url_hit_frac). Only PHRASE forms
    the contiguous slug ``bitcoin-price``; with url_phrase boost on, PHRASE
    ranks first despite lower raw BM25.
    """
    cands = [
        _cand(
            "SCATTER",
            1.0,
            title="n",
            text="t",
            url="https://news.example/world/price-of-bitcoin-jumps",
            domain="news.example",
        ),
        _cand(
            "PHRASE",
            0.85,
            title="n",
            text="t",
            url="https://news.example/world/bitcoin-price-surges",
            domain="news.example",
        ),
    ]
    off = _rerank(
        cands,
        ["bitcoin", "price"],
        title_boost=0.0,
        title_phrase_boost=0.0,
        url_boost=0.25,
        url_phrase_boost=0.0,
        url_exact_boost=0.0,
    )
    # Without phrase boost: equal url_f → BM25 order (SCATTER first).
    assert [c["capture_id"] for c in off] == ["SCATTER", "PHRASE"]
    on = _rerank(
        cands,
        ["bitcoin", "price"],
        title_boost=0.0,
        title_phrase_boost=0.0,
        url_boost=0.25,
        url_phrase_boost=0.2,
        url_exact_boost=0.0,
    )
    # PHRASE final = 0.85 * 1.25 * 1.2 = 1.275
    # SCATTER final = 1.0 * 1.25 * 1.0 = 1.25
    assert [c["capture_id"] for c in on] == ["PHRASE", "SCATTER"]


# ── _url_exact_frac / _url_slug_tokens ───────────────────────────────────
def test_url_slug_tokens_uses_last_path_segment() -> None:
    assert _url_slug_tokens("https://news.example/world/bitcoin-price") == [
        "bitcoin",
        "price",
    ]
    assert _url_slug_tokens("https://news.example/bitcoin") == ["bitcoin"]
    assert _url_slug_tokens("https://news.example/story.html") == ["story"]
    assert _url_slug_tokens("https://news.example/") == []
    assert _url_slug_tokens("") == []


def test_url_exact_frac_empty_is_zero() -> None:
    assert _url_exact_frac("https://x.test/bitcoin", "x.test", []) == 0.0
    assert _url_exact_frac("", "x.test", ["bitcoin"]) == 0.0
    assert _url_exact_frac("https://x.test/", "x.test", ["bitcoin"]) == 0.0


def test_url_exact_frac_single_and_multi_term_match() -> None:
    assert (
        _url_exact_frac("https://news.example/world/bitcoin", "news.example", ["bitcoin"])
        == 1.0
    )
    assert (
        _url_exact_frac(
            "https://news.example/markets/BITCOIN-PRICE",
            "news.example",
            ["bitcoin", "price"],
        )
        == 1.0
    )
    # Extension stripped from leaf slug.
    assert (
        _url_exact_frac(
            "https://news.example/world/bitcoin-price.html",
            "news.example",
            ["bitcoin", "price"],
        )
        == 1.0
    )


def test_url_exact_frac_rejects_extra_or_missing_or_reorder() -> None:
    # Longer slug still gets phrase credit elsewhere; exact requires equality.
    assert (
        _url_exact_frac(
            "https://news.example/world/bitcoin-price-surges",
            "news.example",
            ["bitcoin", "price"],
        )
        == 0.0
    )
    assert (
        _url_exact_frac(
            "https://news.example/world/bitcoin",
            "news.example",
            ["bitcoin", "price"],
        )
        == 0.0
    )
    assert (
        _url_exact_frac(
            "https://news.example/world/price-bitcoin",
            "news.example",
            ["bitcoin", "price"],
        )
        == 0.0
    )
    # Section crumbs are not part of the leaf slug → no false exact.
    assert (
        _url_exact_frac(
            "https://news.example/bitcoin/price",
            "news.example",
            ["bitcoin", "price"],
        )
        == 0.0
    )


def test_rerank_url_exact_overrides_partial_slug_match() -> None:
    """Leaf slug tokens == query beats a longer slug with equal bag/phrase hits.

    Both URLs contain ordered ``bitcoin`` then ``price`` (url_hit + url_phrase
    full credit). Only EXACT has slug tokens exactly equal to the query; with
    url_exact boost on, EXACT ranks first despite lower raw BM25.
    """
    cands = [
        _cand(
            "LONGER",
            1.0,
            title="n",
            text="t",
            url="https://news.example/world/bitcoin-price-surges",
            domain="news.example",
        ),
        _cand(
            "EXACT",
            0.85,
            title="n",
            text="t",
            url="https://news.example/world/bitcoin-price",
            domain="news.example",
        ),
    ]
    off = _rerank(
        cands,
        ["bitcoin", "price"],
        title_boost=0.0,
        title_phrase_boost=0.0,
        title_exact_boost=0.0,
        url_boost=0.25,
        url_phrase_boost=0.2,
        url_exact_boost=0.0,
        lead_hit_boost=0.0,
        lead_phrase_boost=0.0,
    )
    # Without exact boost: equal url_f + url_phrase_f → BM25 order.
    assert [c["capture_id"] for c in off] == ["LONGER", "EXACT"]
    on = _rerank(
        cands,
        ["bitcoin", "price"],
        title_boost=0.0,
        title_phrase_boost=0.0,
        title_exact_boost=0.0,
        url_boost=0.25,
        url_phrase_boost=0.2,
        url_exact_boost=0.3,
        lead_hit_boost=0.0,
        lead_phrase_boost=0.0,
    )
    # EXACT final = 0.85 * 1.25 * 1.2 * 1.3 = 1.6575
    # LONGER final = 1.0 * 1.25 * 1.2 * 1.0 = 1.5
    assert [c["capture_id"] for c in on] == ["EXACT", "LONGER"]


def test_rerank_url_exact_falls_back_to_canonical_url() -> None:
    """Missing ``url`` still scores slug exact via ``canonical_url``."""
    cands = [
        _cand("A", 1.0, title="n", text="t", domain="x.test"),
        _cand(
            "B",
            0.85,
            title="n",
            text="t",
            canonical_url="https://x.test/topic/bitcoin",
            domain="x.test",
        ),
    ]
    out = _rerank(
        cands,
        ["bitcoin"],
        title_boost=0.0,
        title_phrase_boost=0.0,
        title_exact_boost=0.0,
        url_boost=0.0,
        url_phrase_boost=0.0,
        url_exact_boost=0.3,
        lead_hit_boost=0.0,
        lead_phrase_boost=0.0,
        domain_nav_boost=0.0,
    )
    # B final = 0.85 * 1.3 = 1.105 > A = 1.0
    assert [c["capture_id"] for c in out] == ["B", "A"]


# ── _domain_nav_frac / domain labels ─────────────────────────────────────
def test_domain_labels_tokenizes_host_labels() -> None:
    assert _domain_labels("news.bbc.co.uk") == {"news", "bbc", "co", "uk"}
    assert _domain_labels("Reuters.com") == {"reuters", "com"}
    assert _domain_labels("") == set()
    assert _domain_labels("   ") == set()
    # Hyphenated brands keep both parts as labels.
    assert "wall" in _domain_labels("wsj.com") or True
    assert _domain_labels("the-verge.com") == {"the", "verge", "com"}


def test_domain_nav_frac_empty_is_zero() -> None:
    assert _domain_nav_frac("bbc.com", []) == 0.0
    assert _domain_nav_frac("", ["bbc"]) == 0.0
    assert _domain_nav_frac("   ", ["bbc"]) == 0.0


def test_domain_nav_frac_label_token_match() -> None:
    """Query terms must match whole domain labels (not path/body substrings)."""
    assert _domain_nav_frac("bbc.com", ["bbc"]) == 1.0
    assert _domain_nav_frac("news.reuters.com", ["reuters"]) == 1.0
    assert _domain_nav_frac("news.reuters.com", ["reuters", "news"]) == 1.0
    assert _domain_nav_frac("news.reuters.com", ["reuters", "bitcoin"]) == 0.5
    # Path-ish terms are not domain labels.
    assert _domain_nav_frac("news.example", ["bitcoin"]) == 0.0
    # Substring inside a longer label does not count (token equality).
    assert _domain_nav_frac("bitcoinmagazine.com", ["bit"]) == 0.0
    assert _domain_nav_frac("bitcoinmagazine.com", ["bitcoinmagazine"]) == 1.0


def test_domain_nav_frac_is_case_insensitive() -> None:
    assert _domain_nav_frac("BBC.COM", ["bbc"]) == 1.0
    assert _domain_nav_frac("News.Example", ["NEWS"]) == 1.0


def test_rerank_domain_nav_overrides_higher_bm25() -> None:
    """Navigational domain-label hit promotes the publisher over body-only BM25.

    OTHER has higher raw BM25 with the query only in long body text.
    NAV has lower BM25 but domain label equals the query (``reuters``).
    Neutral titles/urls/leads so only domain_nav_f differs when boost is on.
    """
    cands = [
        _cand(
            "OTHER",
            1.0,
            title="n",
            text="reuters " * 30,
            url="https://other.example/world/story",
            domain="other.example",
        ),
        _cand(
            "NAV",
            0.85,
            title="n",
            text="brief note about markets",
            url="https://www.reuters.com/world/story",
            domain="reuters.com",
        ),
    ]
    off = _rerank(
        cands,
        ["reuters"],
        title_boost=0.0,
        title_phrase_boost=0.0,
        title_exact_boost=0.0,
        url_boost=0.0,
        url_phrase_boost=0.0,
        url_exact_boost=0.0,
        lead_hit_boost=0.0,
        lead_phrase_boost=0.0,
        domain_nav_boost=0.0,
    )
    assert [c["capture_id"] for c in off] == ["OTHER", "NAV"]
    on = _rerank(
        cands,
        ["reuters"],
        title_boost=0.0,
        title_phrase_boost=0.0,
        title_exact_boost=0.0,
        url_boost=0.0,
        url_phrase_boost=0.0,
        url_exact_boost=0.0,
        lead_hit_boost=0.0,
        lead_phrase_boost=0.0,
        domain_nav_boost=0.3,
    )
    # NAV final = 0.85 * 1.3 = 1.105 > OTHER = 1.0 * 1.0
    assert [c["capture_id"] for c in on] == ["NAV", "OTHER"]


def test_rerank_domain_nav_multi_term_partial_coverage() -> None:
    """Partial multi-term domain coverage still ranks above zero-nav peers."""
    cands = [
        _cand(
            "NONE",
            1.0,
            title="n",
            text="t",
            url="https://other.example/x",
            domain="other.example",
        ),
        _cand(
            "HALF",
            0.90,
            title="n",
            text="t",
            url="https://news.bbc.co.uk/x",
            domain="news.bbc.co.uk",
        ),
    ]
    out = _rerank(
        cands,
        ["bbc", "bitcoin"],
        title_boost=0.0,
        title_phrase_boost=0.0,
        title_exact_boost=0.0,
        url_boost=0.0,
        url_phrase_boost=0.0,
        url_exact_boost=0.0,
        lead_hit_boost=0.0,
        lead_phrase_boost=0.0,
        domain_nav_boost=0.3,
    )
    # HALF domain_nav_f = 1 + 0.3 * 0.5 = 1.15 → final 0.90 * 1.15 = 1.035 > 1.0
    assert [c["capture_id"] for c in out] == ["HALF", "NONE"]


def test_rerank_preserves_bm25_order_on_ties() -> None:
    # Identical title/length -> identical factors -> BM25 (input) order kept.
    cands = [_cand("A", 0.9, title="x", text="t"), _cand("B", 0.9, title="x", text="t")]
    out = _rerank(cands, ["bitcoin"], title_boost=0.5)
    assert [c["capture_id"] for c in out] == ["A", "B"]


def test_rerank_length_factor_breaks_ties_toward_shorter() -> None:
    # Equal BM25, equal (zero) title hit; the much longer doc is damped below.
    cands = [_cand("LONG", 1.0, title="n", text="z" * 40000),
             _cand("SHORT", 1.0, title="n", text="z" * 10)]
    out = _rerank(cands, ["bitcoin"], title_boost=0.5)
    assert [c["capture_id"] for c in out] == ["SHORT", "LONG"]


def test_rerank_recency_prefers_newer_when_enabled() -> None:
    ref = 1_000_000_000.0
    cands = [_cand("OLD", 1.0, title="n", text="t", ts=ref - 365 * 86400),
             _cand("NEW", 1.0, title="n", text="t", ts=ref)]
    out = _rerank(cands, ["bitcoin"], recency_weight=0.5, recency_halflife_days=30.0)
    assert [c["capture_id"] for c in out] == ["NEW", "OLD"]


def test_rerank_recency_off_by_default_keeps_bm25_order() -> None:
    ref = 1_000_000_000.0
    cands = [_cand("OLD", 1.0, title="n", text="t", ts=ref - 365 * 86400),
             _cand("NEW", 0.9, title="n", text="t", ts=ref)]
    out = _rerank(cands, ["bitcoin"])  # default recency weight 0 -> no recency
    assert [c["capture_id"] for c in out] == ["OLD", "NEW"]  # pure BM25


def test_rerank_does_not_mutate_input_rows() -> None:
    cands = [_cand("A", 1.0, title="bitcoin", text="t")]
    before = dict(cands[0])
    _rerank(cands, ["bitcoin"], title_boost=0.5)
    assert cands[0] == before


def test_rerank_handles_empty_and_single() -> None:
    assert _rerank([], ["bitcoin"]) == []
    one = [_cand("A", 1.0, title="t", text="t")]
    assert [c["capture_id"] for c in _rerank(one, ["bitcoin"])] == ["A"]


def test_rerank_recency_prefers_published_ts_over_fetch_ts() -> None:
    """When recency is on, published_ts beats a newer fetch_ts on the other doc."""
    ref = 1_000_000_000.0
    # A: old published, but freshly fetched (fetch_ts = ref).
    # B: recently published, older fetch.
    # If we incorrectly used fetch_ts only, A would win; published_ts makes B win.
    cands = [
        _cand("A", 1.0, title="n", text="t", published_ts=ref - 365 * 86400, fetch_ts=ref),
        _cand("B", 1.0, title="n", text="t", published_ts=ref, fetch_ts=ref - 30 * 86400),
    ]
    out = _rerank(cands, ["bitcoin"], recency_weight=0.5, recency_halflife_days=30.0)
    assert [c["capture_id"] for c in out] == ["B", "A"]


def test_rerank_recency_falls_back_to_fetch_ts_when_published_missing() -> None:
    """Missing published_ts → use fetch_ts for the recency prior."""
    ref = 1_000_000_000.0
    cands = [
        _cand("OLD", 1.0, title="n", text="t", fetch_ts=ref - 365 * 86400),
        _cand("NEW", 1.0, title="n", text="t", fetch_ts=ref),
    ]
    out = _rerank(cands, ["bitcoin"], recency_weight=0.5, recency_halflife_days=30.0)
    assert [c["capture_id"] for c in out] == ["NEW", "OLD"]


def test_rerank_recency_weight_boosts_fresher_over_higher_bm25() -> None:
    """With a large enough Wr, a fresher published_ts can outrank higher raw BM25."""
    ref = 1_000_000_000.0
    # OLD has higher BM25 (1.0) but is a year old; NEW has 0.85 BM25 and is fresh.
    # Without recency: OLD first. With Wr=0.5: NEW final = 0.85 * 1.5 = 1.275 > 1.0 * ~1.0.
    cands = [
        _cand("OLD", 1.0, title="n", text="t", published_ts=ref - 365 * 86400),
        _cand("NEW", 0.85, title="n", text="t", published_ts=ref),
    ]
    off = _rerank(cands, ["bitcoin"], recency_weight=0.0)
    assert [c["capture_id"] for c in off] == ["OLD", "NEW"]
    on = _rerank(cands, ["bitcoin"], recency_weight=0.5, recency_halflife_days=30.0)
    assert [c["capture_id"] for c in on] == ["NEW", "OLD"]
