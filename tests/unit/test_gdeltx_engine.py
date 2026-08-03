"""Unit tests for the GDELT analytics bridge engine.

All GDELT HTTP traffic is mocked — either a scripted ``httpx.AsyncClient``
or a patched ``GdeltBridge._gdelt_counts``. No test touches the network.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from awareness.gdeltx.engine import GdeltBridge
from awareness.gdeltx.models import GapReport, GdeltComparison, GdeltWindow
from awareness.storage.duckdb_index import DuckDbIndex
from awareness.util.timeutil import floor_to_day

_FULL_KEYS = (
    "doc_id", "capture_id", "parent_doc_or_dup_group", "source_type",
    "source_name", "source_locator", "source_shard",
    "source_offset_or_record_id", "discovery_channel", "job_id", "batch_id",
    "ingest_version", "url", "canonical_url", "domain", "fetch_ts",
    "observed_ts", "published_ts", "last_modified", "content_type",
    "http_status", "etag", "title", "text", "language", "content_hash",
    "near_dup_hash", "robots_decision", "terms_note_if_relevant",
)

_FIXED_NOW = datetime(2026, 6, 14, 10, 0, 0, tzinfo=UTC)


def _write_doc(
    root: Path,
    idx: int,
    *,
    ts: datetime,
    title: str = "",
    text: str = "",
    domain: str = "example.com",
    language: str | None = None,
) -> None:
    day = root / "captures" / f"{ts:%Y}" / f"{ts:%m}" / f"{ts:%d}"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}",
        capture_id=f"cap-{idx:04d}",
        source_type="rss",
        domain=domain,
        url=f"https://{domain}/{idx}",
        fetch_ts=ts.isoformat(),
        observed_ts=ts.isoformat(),
        title=title,
        text=text,
        language=language,
    )
    (day / f"chunk-{idx:04d}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _index(tmp_path: Path) -> DuckDbIndex:
    return DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )


class _FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        articles: int | None = None,
        json_error: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self._articles = articles
        self._json_error = json_error

    def json(self) -> dict:
        if self._json_error is not None:
            raise self._json_error
        if self._articles is None:
            return {}
        return {"articles": [{"url": f"https://example.com/{i}"} for i in range(self._articles)]}


class _FakeAsyncClient:
    """Scripted AsyncClient: per-(term, day) article counts, call log."""

    def __init__(
        self,
        counts: dict[tuple[str, str], int] | None = None,
        status_code: int = 200,
        json_error: Exception | None = None,
        raise_on_get: Exception | None = None,
    ) -> None:
        self._counts = counts or {}
        self._status_code = status_code
        self._json_error = json_error
        self._raise_on_get = raise_on_get
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, url: str, params: dict[str, str] | None = None, **kwargs: object) -> _FakeResponse:
        params = dict(params or {})
        self.calls.append((url, params))
        if self._raise_on_get is not None:
            raise self._raise_on_get
        term = str(params.get("query"))
        day = str(params.get("startdatetime"))[:8]
        return _FakeResponse(
            status_code=self._status_code,
            articles=self._counts.get((term, day), 0),
            json_error=self._json_error,
        )


def _patch_client(monkeypatch: pytest.MonkeyPatch, client: _FakeAsyncClient) -> None:
    monkeypatch.setattr(
        "awareness.gdeltx.engine.httpx.AsyncClient", lambda *args, **kwargs: client
    )


def _bridge(tmp_path: Path) -> GdeltBridge:
    return GdeltBridge(_index(tmp_path), cache_dir=tmp_path / "cache")


def _freeze_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("awareness.gdeltx.engine.utcnow", lambda: _FIXED_NOW)


# ── gdelt_query: per-day counts, aggregation, validation ─────────────────────


def test_gdelt_query_counts_articles_per_day(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _FakeAsyncClient(
        counts={
            ("bitcoin", "20260601"): 2,
            ("bitcoin", "20260602"): 0,
            ("bitcoin", "20260603"): 5,
        }
    )
    _patch_client(monkeypatch, client)
    windows = _bridge(tmp_path).gdelt_query(
        "bitcoin",
        datetime(2026, 6, 1, tzinfo=UTC),
        datetime(2026, 6, 3, 23, 59, 59, tzinfo=UTC),
    )
    assert [w.count for w in windows] == [2, 0, 5]
    assert [w.ts for w in windows] == [
        datetime(2026, 6, 1, tzinfo=UTC),
        datetime(2026, 6, 2, tzinfo=UTC),
        datetime(2026, 6, 3, tzinfo=UTC),
    ]
    assert all(w.term == "bitcoin" for w in windows)
    # One GDELT call per day with the expected DOC 2.0 parameters.
    assert len(client.calls) == 3
    url, params = client.calls[0]
    assert url.endswith("/api/v2/doc/doc")
    assert params["mode"] == "artlist"
    assert params["format"] == "json"
    assert params["startdatetime"] == "20260601000000"
    assert params["enddatetime"] == "20260601235959"


def test_gdelt_query_week_granularity_aggregates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _FakeAsyncClient(
        counts={("bitcoin", day): i + 1 for i, day in enumerate(["20260601", "20260602", "20260603"])}
    )
    _patch_client(monkeypatch, client)
    windows = _bridge(tmp_path).gdelt_query(
        "bitcoin",
        datetime(2026, 6, 1, tzinfo=UTC),  # Monday
        datetime(2026, 6, 3, 23, 59, 59, tzinfo=UTC),
        granularity="week",
    )
    assert len(windows) == 1
    assert windows[0].ts == datetime(2026, 6, 1, tzinfo=UTC)
    assert windows[0].count == 6  # 1 + 2 + 3


def test_gdelt_query_rejects_bad_input(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path)
    start = datetime(2026, 6, 1, tzinfo=UTC)
    end = datetime(2026, 6, 1, 23, 59, 59, tzinfo=UTC)
    with pytest.raises(ValueError):
        bridge.gdelt_query("", start, end)
    with pytest.raises(ValueError):
        bridge.gdelt_query("x" * 81, start, end)
    with pytest.raises(ValueError):
        bridge.gdelt_query("bitcoin\ninjected", start, end)
    with pytest.raises(ValueError):
        bridge.gdelt_query("bitcoin", end, start)  # reversed window
    with pytest.raises(ValueError):
        bridge.gdelt_query("bitcoin", start, start + timedelta(days=90))  # > 60 days
    with pytest.raises(ValueError):
        bridge.gdelt_query("bitcoin", start, end, granularity="hourly")


# ── cache behavior ───────────────────────────────────────────────────────────


def test_cache_hit_avoids_http(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _FakeAsyncClient(
        counts={("bitcoin", "20260601"): 2, ("bitcoin", "20260602"): 1}
    )
    _patch_client(monkeypatch, client)
    bridge = _bridge(tmp_path)
    start = datetime(2026, 6, 1, tzinfo=UTC)
    end = datetime(2026, 6, 2, 23, 59, 59, tzinfo=UTC)
    first = bridge.gdelt_query("bitcoin", start, end)
    second = bridge.gdelt_query("bitcoin", start, end)
    assert first == second
    assert len(first) == 2
    assert len(client.calls) == 2  # first call only
    cache_files = list((tmp_path / "cache").glob("gdeltx_*.json"))
    assert len(cache_files) == 1


def test_cache_ttl_expiry_refetches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _FakeAsyncClient(counts={("bitcoin", "20260601"): 2})
    _patch_client(monkeypatch, client)
    bridge = _bridge(tmp_path)
    start = datetime(2026, 6, 1, tzinfo=UTC)
    end = datetime(2026, 6, 1, 23, 59, 59, tzinfo=UTC)
    bridge.gdelt_query("bitcoin", start, end)
    assert len(client.calls) == 1
    cache_files = list((tmp_path / "cache").glob("gdeltx_*.json"))
    assert len(cache_files) == 1
    stale = time.time() - 7 * 3600  # older than the 6h TTL
    os.utime(cache_files[0], (stale, stale))
    bridge.gdelt_query("bitcoin", start, end)
    assert len(client.calls) == 2


def test_corrupt_cache_falls_back_to_http(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _FakeAsyncClient(counts={("bitcoin", "20260601"): 2})
    _patch_client(monkeypatch, client)
    bridge = _bridge(tmp_path)
    start = datetime(2026, 6, 1, tzinfo=UTC)
    end = datetime(2026, 6, 1, 23, 59, 59, tzinfo=UTC)
    cache_path = tmp_path / "cache" / "gdeltx_deadbeef.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text("not json {", encoding="utf-8")
    windows = bridge.gdelt_query("bitcoin", start, end)
    assert len(windows) == 1
    assert windows[0].count == 2


# ── API failures degrade to [] (never raise) ────────────────────────────────


def test_api_http_error_retries_then_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _FakeAsyncClient(status_code=500)
    _patch_client(monkeypatch, client)
    windows = _bridge(tmp_path).gdelt_query(
        "bitcoin",
        datetime(2026, 6, 1, tzinfo=UTC),
        datetime(2026, 6, 1, 23, 59, 59, tzinfo=UTC),
    )
    assert windows == []
    assert len(client.calls) == 2  # initial + one retry


def test_api_transport_error_retries_then_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _FakeAsyncClient(raise_on_get=httpx.ConnectError("offline"))
    _patch_client(monkeypatch, client)
    windows = _bridge(tmp_path).gdelt_query(
        "bitcoin",
        datetime(2026, 6, 1, tzinfo=UTC),
        datetime(2026, 6, 2, 23, 59, 59, tzinfo=UTC),
    )
    assert windows == []
    assert len(client.calls) == 2  # only day 1 was attempted (both tries)


def test_api_bad_json_retries_then_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _FakeAsyncClient(json_error=json.JSONDecodeError("no", "doc", 0))
    _patch_client(monkeypatch, client)
    windows = _bridge(tmp_path).gdelt_query(
        "bitcoin",
        datetime(2026, 6, 1, tzinfo=UTC),
        datetime(2026, 6, 1, 23, 59, 59, tzinfo=UTC),
    )
    assert windows == []
    assert len(client.calls) == 2


# ── compare_with_local ───────────────────────────────────────────────────────


def _fake_counts_for(pattern: list[int]) -> object:
    async def fake_counts(self, term: str, start: object, end: object) -> list[GdeltWindow]:
        first = floor_to_day(start)
        last = floor_to_day(end)
        days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
        return [
            GdeltWindow(term=term, ts=day, count=pattern[(day - first).days])
            for day in days
        ]

    return fake_counts


def test_compare_with_local_correlation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _freeze_now(monkeypatch)
    pattern = [3, 0, 5, 2, 0, 7, 1]
    start = datetime(2026, 6, 8, tzinfo=UTC)
    idx = 0
    for day_i, count in enumerate(pattern):
        for _ in range(count):
            idx += 1
            _write_doc(
                tmp_path / "jsonl",
                idx,
                ts=start + timedelta(days=day_i, hours=1),
                title="Bitcoin rally",
                text="market moves",
            )
    monkeypatch.setattr(GdeltBridge, "_gdelt_counts", _fake_counts_for(pattern))

    comp = GdeltBridge(_index(tmp_path), cache_dir=tmp_path / "cache").compare_with_local(
        "bitcoin", window_days=7
    )
    assert isinstance(comp, GdeltComparison)
    assert comp.n_days == 7
    assert comp.local_count == sum(pattern)
    assert comp.gdelt_count == sum(pattern)
    assert len(comp.local_series) == 7
    assert len(comp.gdelt_series) == 7
    assert comp.correlation_r == pytest.approx(1.0, abs=1e-9)
    assert comp.note == ""


def test_compare_zero_variance_correlation_is_zero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _freeze_now(monkeypatch)
    monkeypatch.setattr(GdeltBridge, "_gdelt_counts", _fake_counts_for([0, 0, 0, 0, 0, 0, 0]))
    comp = _bridge(tmp_path).compare_with_local("bitcoin", window_days=7)
    assert comp.correlation_r == 0.0
    assert comp.local_count == 0
    assert comp.gdelt_count == 0
    assert len(comp.gdelt_series) == 7  # API ok, all-zero series
    assert "no local captures match the term" in comp.note
    assert "zero variance" in comp.note


def test_compare_gdelt_failure_empty_series_with_note(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _freeze_now(monkeypatch)

    async def fake_counts(self, term: str, start: object, end: object) -> list[GdeltWindow]:
        return []

    monkeypatch.setattr(GdeltBridge, "_gdelt_counts", fake_counts)
    comp = _bridge(tmp_path).compare_with_local("bitcoin", window_days=7)
    assert comp.gdelt_series == []
    assert comp.gdelt_count == 0
    assert comp.correlation_r == 0.0
    assert len(comp.local_series) == 7  # local side still aligned (zeros)
    assert "gdelt API unavailable" in comp.note


# ── coverage_gap ─────────────────────────────────────────────────────────────


def test_coverage_gap_ratio_logic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _freeze_now(monkeypatch)
    start = datetime(2026, 6, 8, tzinfo=UTC)
    for i in range(5):
        _write_doc(
            tmp_path / "jsonl",
            i + 1,
            ts=start + timedelta(days=i, hours=1),
            title="Covered story",
            text="covered",
        )

    per_term: dict[str, int] = {
        "bigstory": 300,  # gdelt huge, local 0        → gap
        "covered": 50,  # local 5 / gdelt 50 = 0.1     → not gap (0.1 is not < 0.1)
        "quiet": 4,  # gdelt below the "big story" bar → not gap
    }

    async def fake_counts(self, term: str, start: object, end: object) -> list[GdeltWindow]:
        first = floor_to_day(start)
        last = floor_to_day(end)
        days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
        volume = per_term.get(term, 0)
        return [
            GdeltWindow(term=term, ts=day, count=volume if day == first else 0)
            for day in days
        ]

    monkeypatch.setattr(GdeltBridge, "_gdelt_counts", fake_counts)
    reports = _bridge(tmp_path).coverage_gap(["bigstory", "covered", "quiet"], window_days=7)

    assert all(isinstance(r, GapReport) for r in reports)
    by_term = {r.term: r for r in reports}
    assert by_term["bigstory"].gap is True
    assert by_term["bigstory"].gdelt_count == 300
    assert by_term["bigstory"].local_count == 0
    assert by_term["bigstory"].ratio == 0.0
    assert by_term["covered"].gap is False
    assert by_term["covered"].local_count == 5
    assert by_term["covered"].gdelt_count == 50
    assert by_term["covered"].ratio == pytest.approx(0.1, abs=1e-9)
    assert by_term["quiet"].gap is False
    assert by_term["quiet"].gdelt_count == 4
    # Gaps first, then by descending gdelt volume.
    assert [r.term for r in reports] == ["bigstory", "covered", "quiet"]


def test_coverage_gap_validates_and_dedupes_terms(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path)
    with pytest.raises(ValueError):
        bridge.coverage_gap(["ok", "bad\tterm"])
    with pytest.raises(ValueError):
        bridge.coverage_gap([""])
    assert bridge.coverage_gap([]) == []


def test_coverage_gap_window_validation(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path)
    with pytest.raises(ValueError):
        bridge.coverage_gap(["bitcoin"], window_days=0)
    with pytest.raises(ValueError):
        bridge.coverage_gap(["bitcoin"], window_days=61)
