# Awareness Cycle 2 — Plan 3: BM25F Field-Boost Re-Ranking (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make search ranking field-aware — a query term in a document's **title** should outrank the same term buried in a long body — by re-ranking the top-`max_results` BM25 candidates with bounded, independent multiplicative factors (title boost · length damping · optional recency), instead of trusting DuckDB's single-blob BM25 order directly.

**Architecture:** DuckDB's FTS (`fts_main_captures_idx.match_bm25`) indexes `title` + `text` as **one field**, so it cannot field-boost. We keep BM25 as the *retrieval* score but change the *ordering*: fetch the top-`max_results` candidates by raw BM25 (`ORDER BY score DESC LIMIT window`), re-rank them in a **pure** `_rerank(...)` function, then slice `[offset:offset+limit]`. The re-rank multiplies the min-max-normalized BM25 score by three bounded factors — all-neutral collapses to identity (pure BM25 order preserved). `total` stays the SQL `COUNT(*)`; the row schema is unchanged (no new keys); `mode`/`ranked` semantics are unchanged.

**The formula (user-selected: multiplicative boosts):**
```
final = bm25_norm × (1 + Wt·title_hit_frac) × length_factor × recency_factor
bm25_norm    = raw_score / max(raw_score in the candidate window)
title_hit_frac = (# distinct query terms present in title) / (# query terms)
length_factor  ∈ [floor, 1.0]  — 1.0 up to a pivot length, decaying for longer docs
recency_factor ∈ [1.0, 1+Wr]   — newest≈1+Wr, old→1.0; DISABLED by default (Wr=0)
```
Stable tie-break: equal `final` preserves the incoming BM25 order (sort key `(-final, original_index)`).

**Tech Stack:** Python 3.13 stdlib (`datetime`), DuckDB FTS, pytest. No new deps.

**Standard test command:** `PYTHONPATH=src .venv/bin/python -m pytest -q -p no:cacheprovider`
**Full-suite gate:** `PYTHONPATH=src .venv/bin/python -m pytest -q -m "not slow and not smoke"`
**Baseline at plan start:** 240 passing (Cycle 1 P1–P4+P3b, Cycle 2 P1/P2/P5a).

**Spec:** `docs/superpowers/specs/2026-06-08-awareness-cycle2-math-systems-design.md` (Plan 3 — Ranking).

**Files touched:**
- Modify: `src/awareness/storage/duckdb_index.py` — add re-rank tuning constants (near the search-config block ~line 43), a `datetime` import, the pure helpers + `_rerank` (in the snippet-helpers region after `_tokenize_query`, ~line 598), and the FTS-path wiring in `DuckDbIndex.search` (~lines 489–503).
- Test (create): `tests/unit/test_search_rerank.py` — pure scoring/ordering unit tests (no DB).
- Test (create): `tests/unit/test_search_rerank_integration.py` — real-index wiring/invariant tests.

**Why two test files / two tasks:** exact, magnitude-sensitive scoring (e.g. "a title hit overrides a *higher* BM25 body match") is proven at the **pure** level where we control the numbers; the integration tests prove the **wiring** (window→rerank→slice, pagination, `max_results` cap, `mode`/`ranked` preserved) without depending on DuckDB's exact BM25 magnitudes (which would be flaky).

---

### Task 1: Pure re-rank scoring core (`_rerank` + helpers)

**Files:**
- Modify: `src/awareness/storage/duckdb_index.py`
- Test (create): `tests/unit/test_search_rerank.py`

- [ ] **Step 1: Write the failing test** — create `tests/unit/test_search_rerank.py`:

```python
"""Pure unit tests for the BM25 re-rank scoring core (no DuckDB).

These pin the exact arithmetic of the multiplicative re-rank so the
magnitude-sensitive cases (a title hit overriding a *higher* raw BM25
body match) are proven deterministically, independent of DuckDB's BM25.
"""

from __future__ import annotations

from datetime import datetime, timezone

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
    dt = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
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
```

- [ ] **Step 2: Run, confirm FAIL** (`ImportError: cannot import name '_rerank'`):
`PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_search_rerank.py -q`

- [ ] **Step 3: Implement** in `src/awareness/storage/duckdb_index.py`:

(a) Add a `datetime` import next to the existing `threading` import (top of file, after `import threading`):
```python
from datetime import datetime
```

(b) Add the re-rank tuning constants right after `DEFAULT_SEARCH_MAX_RESULTS = 200` (~line 43):
```python

# ── re-rank (BM25F-style) tuning ──────────────────────────────────────────────
# DuckDB FTS scores title+text as one blob, so it cannot field-boost. We re-rank
# the top-`max_results` BM25 candidates with independent, bounded multiplicative
# factors; all-neutral collapses to identity (pure BM25 order preserved).
_RERANK_TITLE_BOOST = 0.5        # Wt: full title-term coverage multiplies score by 1+Wt
_RERANK_LEN_PIVOT = 4000         # chars; docs up to here are not length-damped
_RERANK_LEN_FLOOR = 0.75         # the most a very long doc can be damped to
_RERANK_RECENCY_WEIGHT = 0.0     # Wr: 0 disables the recency prior (off by default)
_RERANK_RECENCY_HALFLIFE_DAYS = 30.0
```

(c) Add the pure helpers + `_rerank` in the snippet-helpers region, immediately **after** the `_tokenize_query` function (~line 598, before `_snippet_for`):
```python
def _title_hit_frac(title: str, terms: list[str]) -> float:
    """Fraction of distinct query terms that occur in the title (case-insensitive)."""
    if not terms:
        return 0.0
    low = (title or "").lower()
    hits = sum(1 for t in terms if t and t in low)
    return hits / len(terms)


def _length_factor(text_len: int, *, pivot: int = _RERANK_LEN_PIVOT, floor: float = _RERANK_LEN_FLOOR) -> float:
    """1.0 for docs up to `pivot` chars, decaying toward `floor` for longer ones.

    Tames over-long blobs the single-field BM25 length-norm under-penalizes,
    while never zeroing a document out (bounded in [floor, 1.0]).
    """
    if pivot <= 0 or text_len <= pivot:
        return 1.0
    over = text_len - pivot
    return floor + (1.0 - floor) * (pivot / (pivot + over))


def _to_epoch(ts: Any) -> float | None:
    """Best-effort epoch-seconds from a datetime / number / ISO-8601 string."""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.timestamp()
    try:
        return float(ts)  # already epoch seconds
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _recency_factor(
    doc_epoch: float | None,
    ref_epoch: float | None,
    *,
    halflife_days: float = _RERANK_RECENCY_HALFLIFE_DAYS,
    weight: float = _RERANK_RECENCY_WEIGHT,
) -> float:
    """Bounded recency boost in [1.0, 1.0+weight]: newest≈1+weight, old→1.0.

    Disabled (returns 1.0) when weight<=0, the half-life is non-positive, or a
    timestamp is unavailable.
    """
    if weight <= 0 or halflife_days <= 0 or doc_epoch is None or ref_epoch is None:
        return 1.0
    age_days = max(0.0, (ref_epoch - doc_epoch) / 86400.0)
    decay = 0.5 ** (age_days / halflife_days)
    return 1.0 + weight * decay


def _rerank(
    candidates: list[dict[str, Any]],
    terms: list[str],
    *,
    title_boost: float = _RERANK_TITLE_BOOST,
    len_pivot: int = _RERANK_LEN_PIVOT,
    len_floor: float = _RERANK_LEN_FLOOR,
    recency_weight: float = _RERANK_RECENCY_WEIGHT,
    recency_halflife_days: float = _RERANK_RECENCY_HALFLIFE_DAYS,
    ref_epoch: float | None = None,
) -> list[dict[str, Any]]:
    """Re-rank BM25 candidates (already in raw-score DESC order) by multiplying
    the min-max-normalized BM25 score with independent title / length / recency
    factors. Returns a NEW ordered list; input row dicts are not mutated. Stable:
    equal final scores keep the incoming BM25 order.

    Each candidate dict carries ``score`` (raw BM25), ``title``, ``text`` and a
    timestamp (``published_ts`` or ``fetch_ts``).
    """
    if len(candidates) <= 1:
        return list(candidates)

    scores = [float(c.get("score") or 0.0) for c in candidates]
    max_score = max(scores) if scores else 0.0

    if recency_weight > 0 and ref_epoch is None:
        epochs = [
            e
            for c in candidates
            for e in (_to_epoch(c.get("published_ts") or c.get("fetch_ts")),)
            if e is not None
        ]
        ref_epoch = max(epochs) if epochs else None

    def final_score(c: dict[str, Any], raw: float) -> float:
        norm = (raw / max_score) if max_score > 0 else 0.0
        title_f = 1.0 + title_boost * _title_hit_frac(c.get("title") or "", terms)
        len_f = _length_factor(len(c.get("text") or ""), pivot=len_pivot, floor=len_floor)
        rec_f = _recency_factor(
            _to_epoch(c.get("published_ts") or c.get("fetch_ts")),
            ref_epoch,
            halflife_days=recency_halflife_days,
            weight=recency_weight,
        )
        return norm * title_f * len_f * rec_f

    finals = [final_score(c, raw) for c, raw in zip(candidates, scores, strict=True)]
    # Sort by descending final score; `(-final, i)` keeps the original BM25 order
    # for ties (lower original index = higher BM25 = ranked first).
    order = sorted(range(len(candidates)), key=lambda i: (-finals[i], i))
    return [candidates[i] for i in order]
```

- [ ] **Step 4: Confirm PASS:** `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_search_rerank.py -q`
- [ ] **Step 5: Ruff the new code (no NEW errors):** `.venv/bin/python -m ruff check src/awareness/storage/duckdb_index.py tests/unit/test_search_rerank.py`. If it reports an error on a line you added, fix it to match the file's convention (the helpers contain no SQL, so no S608; `0.5 ** x` and `_to_epoch`'s try/except are intentional). Pre-existing PLC0415/S608 elsewhere are out of scope.
- [ ] **Step 6: Commit:**
```bash
git add src/awareness/storage/duckdb_index.py tests/unit/test_search_rerank.py
git commit -m "feat(search): add pure BM25 re-rank core (title boost + length + recency)"
```

---

### Task 2: Wire `_rerank` into the FTS search path

**Files:**
- Modify: `src/awareness/storage/duckdb_index.py` (`DuckDbIndex.search`, the FTS-ranked block ~lines 489–503)
- Test (create): `tests/unit/test_search_rerank_integration.py`

- [ ] **Step 1: Write the failing test** — create `tests/unit/test_search_rerank_integration.py`:

```python
"""Integration tests: the FTS path retrieves top-K by BM25, re-ranks, then
slices the page. These assert wiring + invariants (not DuckDB's exact BM25
magnitudes — exact scoring is covered by tests/unit/test_search_rerank.py)."""

from __future__ import annotations

import json
from pathlib import Path

from awareness.storage.duckdb_index import DuckDbIndex

_FULL_KEYS = (
    "doc_id", "capture_id", "parent_doc_or_dup_group", "source_type",
    "source_name", "source_locator", "source_shard",
    "source_offset_or_record_id", "discovery_channel", "job_id", "batch_id",
    "ingest_version", "url", "canonical_url", "domain", "fetch_ts",
    "observed_ts", "published_ts", "last_modified", "content_type",
    "http_status", "etag", "title", "text", "language", "content_hash",
    "near_dup_hash", "robots_decision", "terms_note_if_relevant",
)


def _write_doc(root: Path, idx: int, *, title: str, text: str, domain: str = "example.com") -> None:
    day = root / "captures" / "2026" / "06" / "01"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}", capture_id=f"cap-{idx}", source_type="rss",
        domain=domain, url=f"https://{domain}/{idx}",
        fetch_ts="2026-06-01T12:00:00+00:00", title=title, text=text,
    )
    (day / f"chunk-{idx}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _index(tmp_path: Path) -> DuckDbIndex:
    return DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )


def test_title_match_ranks_first_on_fts_path(tmp_path: Path) -> None:
    jsonl = tmp_path / "jsonl"
    # cap-0: "bitcoin" in BOTH title and body. cap-1: body-only mention.
    # cap-2: no match. The title doc must lead and the no-match doc be absent.
    _write_doc(jsonl, 0, title="Bitcoin rally", text="bitcoin is a cryptocurrency")
    _write_doc(jsonl, 1, title="Market roundup", text="the market moved on bitcoin news")
    _write_doc(jsonl, 2, title="Sports", text="a football match ended in a draw")
    res = _index(tmp_path).search("bitcoin", mode="auto")
    assert res["mode"] == "fts" and res["ranked"] is True
    assert res["total"] == 2
    ids = [r["capture_id"] for r in res["rows"]]
    assert ids[0] == "cap-0"          # title hit surfaces first
    assert set(ids) == {"cap-0", "cap-1"}  # the non-matching doc is excluded


def test_rerank_pagination_slices_after_reorder(tmp_path: Path) -> None:
    jsonl = tmp_path / "jsonl"
    _write_doc(jsonl, 0, title="Bitcoin guide", text="bitcoin explained simply")
    _write_doc(jsonl, 1, title="News", text="bitcoin mentioned once here")
    idx = _index(tmp_path)
    page0 = idx.search("bitcoin", mode="auto", limit=1, offset=0)
    page1 = idx.search("bitcoin", mode="auto", limit=1, offset=1)
    assert page0["total"] == 2 and page1["total"] == 2
    assert len(page0["rows"]) == 1 and len(page1["rows"]) == 1
    assert page0["rows"][0]["capture_id"] == "cap-0"          # title doc first
    assert page0["rows"][0]["capture_id"] != page1["rows"][0]["capture_id"]


def test_rerank_still_respects_max_results_cap(tmp_path: Path) -> None:
    jsonl = tmp_path / "jsonl"
    for i in range(8):
        _write_doc(jsonl, i, title=f"Financial report {i}", text="financial financial")
    res = _index(tmp_path).search("financial", mode="auto", limit=100, max_results=3)
    assert len(res["rows"]) <= 3
```

- [ ] **Step 2: Run, confirm FAIL** — `test_title_match_ranks_first_on_fts_path` / `test_rerank_pagination_slices_after_reorder` fail because the current SQL applies `LIMIT/OFFSET` before any re-rank (title doc is not guaranteed first, and the page-1 disjointness/ordering is BM25-only):
`PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_search_rerank_integration.py -q`

> Note: the title-first assertion *may* already pass by BM25 alone (cap-0 has the term twice). That's fine — the test still locks the wiring. The pagination test is the one that fails without window→rerank→slice. If neither fails initially, sharpen `test_rerank_pagination_slices_after_reorder` by raising `cap-1`'s body term count so BM25 would order it first, then confirm it fails pre-implementation.

- [ ] **Step 3: Implement** — in `DuckDbIndex.search`, replace the FTS ranked block (currently ~lines 489–503, from `if total > 0:` through `used_mode = "fts"`) with the window→re-rank→slice version:

```python
                    if total > 0:
                        # DuckDB FTS scores title+text as one blob, so we cannot
                        # field-boost in SQL. Retrieve the top-`window` candidates
                        # by raw BM25, re-rank them in Python (title boost / length
                        # / optional recency), then slice the requested page.
                        window = max(offset + limit, max_results or 0) or (offset + limit)
                        sql = f"""
                            SELECT
                              capture_id, doc_id, parent_doc_or_dup_group,
                              source_type, source_name, discovery_channel,
                              url, canonical_url, domain,
                              fetch_ts, observed_ts, published_ts,
                              title, text, language, content_hash,
                              fts_main_captures_idx.match_bm25(capture_id, $q) AS score
                            {base}
                            ORDER BY score DESC
                            LIMIT {int(window)}
                        """
                        candidates = self._rows(conn, sql, params)
                        ranked_rows = _rerank(candidates, _tokenize_query(query))
                        rows = ranked_rows[offset : offset + limit]
                        used_mode = "fts"
```

(Leave the `# NOTE: only static SQL fragments…` comment and the `total` COUNT line above untouched — `base`, `params`, `where_sql` are unchanged. Only the inner `if total > 0:` body changes: `LIMIT {int(window)}` replaces `LIMIT {int(limit)} OFFSET {int(offset)}`, and the Python re-rank + slice replace the direct `self._rows(...)` assignment.)

- [ ] **Step 4: Confirm PASS:** `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_search_rerank_integration.py -q`
- [ ] **Step 5: Full-suite gate (the critical regression check):** `PYTHONPATH=src .venv/bin/python -m pytest -q -m "not slow and not smoke"`. Expected: **259 passed** (240 baseline + 16 pure re-rank tests from Task 1 + 3 integration tests from this task). Pay special attention to:
  - `tests/unit/test_search_matching.py::test_fts_still_ranks_when_term_present` (must stay `mode=="fts"`, `ranked is True`),
  - `tests/unit/test_search_matching.py::test_max_results_caps_returned_rows` (cap preserved),
  - `tests/unit/test_search_consistency.py`, `test_duckdb_fts_freshness.py`, `test_duckdb_gz.py`, `test_cli_highlight.py` (search shape/ordering).
  If any asserts a specific FTS ordering that the re-rank legitimately changes, READ the test, confirm the new order is correct (title-aware), update the expectation, and record it under Deviations.
- [ ] **Step 6: Ruff (no NEW errors):** `.venv/bin/python -m ruff check src/awareness/storage/duckdb_index.py tests/unit/test_search_rerank_integration.py`. The rewritten f-string SQL keeps the existing safety NOTE; if ruff flags S608 on it, match the surrounding convention (the sibling FTS `COUNT` uses `# nosemgrep`, not noqa — do not add new suppressions the file doesn't already use on equivalent lines).
- [ ] **Step 7: Commit:**
```bash
git add src/awareness/storage/duckdb_index.py tests/unit/test_search_rerank_integration.py
git commit -m "feat(search): re-rank top-K BM25 candidates by title/length before paging"
```

---

## Plan-level self-review checklist

- [ ] Full suite green (`-m "not slow and not smoke"`), expected 243.
- [ ] `_rerank` is pure: returns a new list, mutates no input row, uses an injected/derived `ref_epoch` (never the wall clock) — proven by `test_rerank_does_not_mutate_input_rows` and the recency tests.
- [ ] All-neutral factors collapse to pure BM25 order — proven by `test_rerank_recency_off_by_default_keeps_bm25_order` and `test_rerank_preserves_bm25_order_on_ties`.
- [ ] `total` still equals the SQL `COUNT(*)`; row schema unchanged (no new keys); `mode`/`ranked` semantics unchanged; `max_results` cap honored.
- [ ] Recency is **off by default** (`_RERANK_RECENCY_WEIGHT = 0.0`) — wired but dormant; flip the constant (or pass `recency_weight>0`) to enable.
- [ ] `ruff check` introduces no NEW errors on the two touched/created files.

## Spec coverage note

Spec Plan 3 asks for "BM25F field-boost (title weight) + length-aware + recency prior." This plan delivers all three as bounded multiplicative factors on top of DuckDB's single-blob BM25 (true per-field BM25F is impossible without per-field term stats DuckDB's FTS does not expose — see the rank-formula decision). Corpus-level relevance re-measurement (a labeled query set) is **not** in scope here; it belongs with Cycle 2 Plan 5's benchmark-honesty work.
