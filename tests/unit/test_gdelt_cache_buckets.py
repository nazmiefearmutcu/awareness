"""W38 M-3: the GDELT disk cache is keyed by the DAY RANGE, not raw
timestamps or the caller's window length.

A rotating window selector (or attacker) must not mint DISTINCT cache files
for the same underlying floored day range — that would bypass the 6h disk
cache and re-hit the GDELT API ~60x per request. Conversely, genuinely
different day ranges must NEVER share an entry: a 14-day request must not be
served from a 7-day cache.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from awareness.gdeltx.engine import GdeltBridge
from awareness.storage.duckdb_index import DuckDbIndex
from awareness.util.timeutil import floor_to_day

_FIXED_NOW = datetime(2026, 6, 14, 10, 0, 0, tzinfo=UTC)

_DAYS = [
    "20260608",
    "20260609",
    "20260610",
    "20260611",
    "20260612",
    "20260613",
    "20260614",
]


class _FakeResponse:
    def __init__(self, status_code: int = 200, articles: int = 0) -> None:
        self.status_code = status_code
        self._articles = articles

    def json(self) -> dict:
        return {"articles": [{"url": f"https://example.com/{i}"} for i in range(self._articles)]}


class _FakeAsyncClient:
    """Scripted AsyncClient: per-(term, day) article counts, call log."""

    def __init__(self, counts: dict[tuple[str, str], int] | None = None) -> None:
        self._counts = counts or {}
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, url: str, params: dict[str, str] | None = None, **kwargs: object) -> _FakeResponse:
        params = dict(params or {})
        self.calls.append((url, params))
        term = str(params.get("query"))
        day = str(params.get("startdatetime"))[:8]
        return _FakeResponse(status_code=200, articles=self._counts.get((term, day), 0))


def _patch_client(monkeypatch: pytest.MonkeyPatch, client: _FakeAsyncClient) -> None:
    monkeypatch.setattr(
        "awareness.gdeltx.engine.httpx.AsyncClient", lambda *args, **kwargs: client
    )


def _bridge(tmp_path: Path) -> GdeltBridge:
    idx = DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )
    return GdeltBridge(idx, cache_dir=tmp_path / "cache")


def _freeze_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("awareness.gdeltx.engine.utcnow", lambda: _FIXED_NOW)


def _counts_for(days: list[str], per_day: int = 1) -> dict[tuple[str, str], int]:
    return {("bitcoin", day): per_day for day in days}


def _cache_files(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "cache").glob("gdeltx_*.json"))


# ── same day range ⇒ one cache entry ───────────────────────────────────────


def test_same_day_range_different_window_variants_share_one_cache_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Intra-day start/end ticks with the same floored days → one cache file.

    This is the cache-variant abuse: two "windows" that differ only in raw
    timestamps (e.g. the UI recomputing end=utcnow() minutes apart, or a
    window_days value that shifts the raw start within the same day) must
    resolve to the SAME cache file — the second call is a pure disk hit.
    """
    client = _FakeAsyncClient(_counts_for(_DAYS))
    _patch_client(monkeypatch, client)
    bridge = _bridge(tmp_path)

    end = _FIXED_NOW
    # "window_days=7": start = end - 6 days (floored, compare_with_local math).
    start7 = floor_to_day(end - timedelta(days=6))
    first = bridge.gdelt_query("bitcoin", start7, end)

    # "window_days=8" variant: same floored day range, raw edges ticked.
    start8 = start7 + timedelta(hours=4)
    end8 = end + timedelta(hours=9)
    assert floor_to_day(start8) == floor_to_day(start7)
    assert floor_to_day(end8) == floor_to_day(end)
    second = bridge.gdelt_query("bitcoin", start8, end8)

    assert first == second
    assert len(first) == 7  # one entry per floored day
    # Exactly one HTTP pass (7 per-day GDELT calls); the second call was a hit.
    assert len(client.calls) == 7
    assert len(_cache_files(tmp_path)) == 1


def test_same_floored_end_different_utcnow_ticks_share_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two direct engine calls with utcnow ticking inside one floored day share."""
    client = _FakeAsyncClient(_counts_for(_DAYS))
    _patch_client(monkeypatch, client)
    bridge = _bridge(tmp_path)

    nows = iter([_FIXED_NOW, _FIXED_NOW.replace(hour=23, minute=59)])
    monkeypatch.setattr("awareness.gdeltx.engine.utcnow", lambda: next(nows))

    first = bridge.gdelt_query(
        "bitcoin",
        floor_to_day(_FIXED_NOW - timedelta(days=6)),
        _FIXED_NOW,
    )
    second = bridge.gdelt_query(
        "bitcoin",
        floor_to_day(_FIXED_NOW - timedelta(days=6)),
        _FIXED_NOW.replace(hour=23, minute=59),
    )
    assert first == second
    assert len(client.calls) == 7
    assert len(_cache_files(tmp_path)) == 1


# ── different day ranges ⇒ distinct keys (no false cache hit) ──────────────


def test_different_window_days_get_distinct_cache_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """window_days=7 (start = end-6) vs window_days=8 (start = end-7) differ.

    The day-range key must NOT collapse ranges of different lengths: the
    second call covers a genuinely different day range, so it refetches and
    lands in its own cache file.
    """
    client = _FakeAsyncClient(_counts_for(_DAYS))
    _patch_client(monkeypatch, client)
    _freeze_now(monkeypatch)
    bridge = _bridge(tmp_path)

    seven = bridge.gdelt_query(
        "bitcoin", floor_to_day(_FIXED_NOW - timedelta(days=6)), _FIXED_NOW
    )
    eight = bridge.gdelt_query(
        "bitcoin", floor_to_day(_FIXED_NOW - timedelta(days=7)), _FIXED_NOW
    )

    assert len(seven) == 7
    assert len(eight) == 8  # 8 days fetched — NOT served from the 7-day cache
    assert len(client.calls) == 15
    assert len(_cache_files(tmp_path)) == 2


def test_different_terms_never_share_cache_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _FakeAsyncClient(_counts_for(_DAYS))
    _patch_client(monkeypatch, client)
    bridge = _bridge(tmp_path)
    start = datetime(2026, 6, 8, tzinfo=UTC)
    end = datetime(2026, 6, 14, 23, 59, 59, tzinfo=UTC)

    bridge.gdelt_query("bitcoin", start, end)
    bridge.gdelt_query("ethereum", start, end)
    assert len(client.calls) == 14
    assert len(_cache_files(tmp_path)) == 2


# ── legacy (pre-W38) key format ────────────────────────────────────────────


def test_legacy_raw_start_key_misses_and_refetches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cache files written by the old key (raw start ISO) must not crash or
    be reused — the new day-range key is a different digest → miss → refetch.
    """
    client = _FakeAsyncClient(_counts_for(["20260608", "20260609", "20260610"]))
    _patch_client(monkeypatch, client)
    bridge = _bridge(tmp_path)
    start = datetime(2026, 6, 8, 7, 30, 0, tzinfo=UTC)
    end = datetime(2026, 6, 10, 23, 59, 59, tzinfo=UTC)

    # Replicate the pre-W38 key exactly: raw start ISO, floored end ISO.
    legacy = json.dumps(
        {
            "term": "bitcoin",
            "start": start.isoformat(),
            "end": floor_to_day(end).isoformat(),
            "granularity": "day",
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(legacy.encode("utf-8")).hexdigest()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"gdeltx_{digest}.json").write_text(
        json.dumps(
            {
                "cached_at": "2026-06-08T00:00:00+00:00",
                "windows": [
                    {"term": "bitcoin", "ts": "2026-06-08T00:00:00+00:00", "count": 999, "truncated": False}
                ],
            }
        ),
        encoding="utf-8",
    )

    windows = bridge.gdelt_query("bitcoin", start, end)
    assert [w.count for w in windows] == [1, 1, 1]  # fresh fetch, not the 999
    assert len(client.calls) == 3
    assert len(_cache_files(tmp_path)) == 2  # legacy file + new key file
