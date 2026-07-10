"""Pure unit tests for the BM25 re-rank scoring core (no DuckDB).

These pin the exact arithmetic of the multiplicative re-rank so the
magnitude-sensitive cases (a title hit overriding a *higher* raw BM25
body match) are proven deterministically, independent of DuckDB's BM25.
"""

from __future__ import annotations

from datetime import UTC, datetime

from awareness.storage.duckdb_index import (
    _length_factor,
    _recency_factor,
    _rerank,
    _title_hit_frac,
    _to_epoch,
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
def _cand(cid: str, score: float, *, title: str = "", text: str = "", ts: object = None) -> dict[str, object]:
    return {"capture_id": cid, "score": score, "title": title, "text": text, "fetch_ts": ts}


def test_rerank_title_hit_overrides_higher_bm25() -> None:
    # A: higher raw BM25 (1.0) but the term is NOT in its title.
    # B: lower raw BM25 (0.8) but the term IS in its title.
    # finals: A = 1.0 * 1.0 = 1.0 ; B = 0.8 * (1 + 0.5*1.0) = 1.2  -> B wins.
    cands = [_cand("A", 1.0, title="markets today", text="bitcoin bitcoin"),
             _cand("B", 0.8, title="bitcoin surges", text="the asset gained")]
    out = _rerank(cands, ["bitcoin"], title_boost=0.5)
    assert [c["capture_id"] for c in out] == ["B", "A"]


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
