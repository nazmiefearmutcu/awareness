"""Tests for the digest's optional GDELT context (digest.py gdelt_note).

``generate_digest`` never touches the network in this suite: the GDELT bridge
is either skipped (``include_gdelt=False``) or replaced by a monkeypatched
``GdeltBridge.compare_with_local``. The fake comparison is constructed from
the real models so the summary formatting is exercised end-to-end.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from awareness.analytics.models import TimeBucket
from awareness.consume.digest import generate_digest, render_digest_markdown
from awareness.gdeltx.engine import GdeltBridge
from awareness.gdeltx.models import GdeltComparison
from tests.unit.test_consume_digest import _empty_index, _index


def _fake_comparison(
    *,
    term: str = "brand",
    local_count: int = 4,
    gdelt_count: int = 88,
    r: float = 0.42,
    note: str = "",
    with_series: bool = True,
) -> GdeltComparison:
    buckets = [
        TimeBucket(ts=datetime(2026, 6, 7, tzinfo=UTC), count=1),
        TimeBucket(ts=datetime(2026, 6, 8, tzinfo=UTC), count=3),
    ]
    return GdeltComparison(
        term=term,
        local_count=local_count,
        gdelt_count=gdelt_count,
        local_series=buckets if with_series else [],
        gdelt_series=buckets if with_series else [],
        correlation_r=r,
        n_days=7,
        note=note,
    )


def test_include_gdelt_false_no_note_no_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """include_gdelt=False must never construct the bridge / touch httpx."""
    index = _index(tmp_path, now=datetime.now(tz=UTC))

    def _no_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network must not be used with include_gdelt=False")

    monkeypatch.setattr("httpx.AsyncClient", _no_network)
    monkeypatch.setattr("awareness.gdeltx.engine.GdeltBridge", _no_network)

    digest = generate_digest(index, days=7, include_gdelt=False)

    assert digest.top_terms  # corpus has terms; GDELT would have run if enabled
    assert digest.gdelt_note is None


def test_include_gdelt_true_uses_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """include_gdelt=True calls the bridge and formats the one-line note."""
    index = _index(tmp_path, now=datetime.now(tz=UTC))
    top_term = generate_digest(index, days=7, include_gdelt=False).top_terms[0].term
    fake = _fake_comparison(term=top_term, local_count=4, gdelt_count=88, r=0.42)
    calls: list[tuple[str, int]] = []

    def _fake_compare(self: object, term: str, window_days: int = 14) -> GdeltComparison:
        calls.append((term, window_days))
        return fake

    monkeypatch.setattr(GdeltBridge, "compare_with_local", _fake_compare)

    digest = generate_digest(index, days=7, include_gdelt=True)

    assert calls == [(top_term, 7)]  # top term, digest window
    assert digest.gdelt_note == f"GDELT: {top_term} local 4 vs external 88 (r=0.42)"
    assert "0.42" in digest.gdelt_note
    assert top_term in digest.gdelt_note


def test_include_gdelt_true_clamps_window_to_bridge_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Digest days beyond the bridge's 60-day cap are clamped, not fatal."""
    index = _index(tmp_path, now=datetime.now(tz=UTC))
    calls: list[int] = []

    def _fake_compare(self: object, term: str, window_days: int = 14) -> GdeltComparison:
        calls.append(window_days)
        return _fake_comparison(term=term)

    monkeypatch.setattr(GdeltBridge, "compare_with_local", _fake_compare)

    digest = generate_digest(index, days=90, include_gdelt=True)

    assert calls == [60]
    assert digest.gdelt_note is not None


def test_bridge_raising_never_fails_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A raising bridge leaves gdelt_note=None and the digest intact."""
    index = _index(tmp_path, now=datetime.now(tz=UTC))

    def _raise(self: object, term: str, window_days: int = 14) -> GdeltComparison:
        raise RuntimeError("gdelt offline")

    monkeypatch.setattr(GdeltBridge, "compare_with_local", _raise)

    digest = generate_digest(index, days=7, include_gdelt=True)

    assert digest.gdelt_note is None
    assert digest.total_captures == 4
    assert digest.growth_rate == pytest.approx(1.0)


def test_empty_corpus_skips_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No top terms → no bridge call even with include_gdelt=True."""
    index = _empty_index(tmp_path)

    def _no_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("bridge must not be constructed for an empty corpus")

    monkeypatch.setattr("awareness.gdeltx.engine.GdeltBridge", _no_network)

    digest = generate_digest(index, days=7, include_gdelt=True)

    assert digest.top_terms == []
    assert digest.gdelt_note is None


def test_render_markdown_includes_gdelt_note_under_growth(tmp_path: Path) -> None:
    index = _index(tmp_path, now=datetime.now(tz=UTC))
    digest = generate_digest(index, days=7, include_gdelt=False)
    digest.gdelt_note = "GDELT: brand local 4 vs external 88 (r=0.42)"

    md = render_digest_markdown(digest)

    assert "## Notes on growth" in md
    assert md.index("## Notes on growth") < md.index("GDELT: brand local 4 vs external 88 (r=0.42)")
    assert "GDELT: brand local 4 vs external 88 (r=0.42)" in md


def test_render_markdown_omits_gdelt_note_when_unset(tmp_path: Path) -> None:
    index = _index(tmp_path, now=datetime.now(tz=UTC))
    md = render_digest_markdown(generate_digest(index, days=7, include_gdelt=False))
    assert "GDELT:" not in md
