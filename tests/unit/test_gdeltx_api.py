"""API tests for the /gdelt router (200/400/503 contract).

Mounts :func:`~awareness.gdeltx.router.create_gdeltx_router` on a bare
FastAPI app and drives it with FastAPI's TestClient. The GDELT API is never
touched: the router is pointed at a bridge whose ``_gdelt_counts`` is
patched (scripted per-term counts), so the whole request path — validation,
index-readiness, engine, serialization — is exercised with zero network.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from awareness.gdeltx.engine import GdeltBridge
from awareness.gdeltx.models import GdeltWindow
from awareness.gdeltx.router import create_gdeltx_router
from awareness.util.timeutil import floor_to_day


def _fake_index(ready: bool = True) -> MagicMock:
    index = MagicMock()
    index.health_snapshot.return_value = {"ready": ready}
    index.execute.return_value = []
    return index


def _bridge_factory_factory(tmp_path: Path):
    def _bridge_factory(index: object) -> GdeltBridge:
        return GdeltBridge(index, cache_dir=tmp_path / "cache")

    return _bridge_factory


def _client(
    monkeypatch,
    tmp_path: Path,
    index: MagicMock | None = None,
    per_day: dict[str, int] | None = None,
) -> TestClient:
    index = index or _fake_index()
    monkeypatch.setattr(
        "awareness.gdeltx.router.GdeltBridge", _bridge_factory_factory(tmp_path)
    )
    per_day = per_day or {}

    async def _fake_counts(self, term: str, start: object, end: object) -> list[GdeltWindow]:
        first = floor_to_day(start)
        last = floor_to_day(end)
        days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
        volume = per_day.get(term, 3)
        return [
            GdeltWindow(term=term, ts=day, count=volume if day == first else 0)
            for day in days
        ]

    monkeypatch.setattr(GdeltBridge, "_gdelt_counts", _fake_counts)
    app = FastAPI()
    app.include_router(create_gdeltx_router(lambda: index))
    return TestClient(app)


def test_compare_returns_full_shape(monkeypatch, tmp_path: Path) -> None:
    with _client(monkeypatch, tmp_path) as client:
        res = client.get("/gdelt/compare", params={"term": "bitcoin", "window_days": 7})
    assert res.status_code == 200
    data = res.json()
    assert set(data) == {
        "term", "local_count", "gdelt_count", "local_series", "gdelt_series",
        "correlation_r", "n_days", "note",
    }
    assert data["term"] == "bitcoin"
    assert data["n_days"] == 7
    assert data["gdelt_count"] == 3
    assert data["local_count"] == 0
    assert len(data["local_series"]) == 7
    assert len(data["gdelt_series"]) == 7
    assert data["correlation_r"] == 0.0
    assert "zero variance" in data["note"]


def test_compare_bad_term_is_400(monkeypatch, tmp_path: Path) -> None:
    with _client(monkeypatch, tmp_path) as client:
        empty = client.get("/gdelt/compare", params={"term": ""})
        too_long = client.get("/gdelt/compare", params={"term": "x" * 81})
        control = client.get("/gdelt/compare", params={"term": "bitcoin\ninjected"})
        bad_window = client.get("/gdelt/compare", params={"term": "bitcoin", "window_days": 0})
    assert empty.status_code == 400
    assert too_long.status_code == 400
    assert control.status_code == 400
    assert bad_window.status_code == 400


def test_compare_index_not_ready_is_503(monkeypatch, tmp_path: Path) -> None:
    with _client(monkeypatch, tmp_path, index=_fake_index(ready=False)) as client:
        res = client.get("/gdelt/compare", params={"term": "bitcoin"})
    assert res.status_code == 503
    assert "not ready" in res.json()["detail"]


def test_compare_gdelt_failure_still_200_with_note(monkeypatch, tmp_path: Path) -> None:
    async def _no_counts(self, term: str, start: object, end: object) -> list[GdeltWindow]:
        return []

    monkeypatch.setattr("awareness.gdeltx.router.GdeltBridge", _bridge_factory_factory(tmp_path))
    monkeypatch.setattr(GdeltBridge, "_gdelt_counts", _no_counts)
    app = FastAPI()
    app.include_router(create_gdeltx_router(_fake_index()))
    with TestClient(app) as client:
        res = client.get("/gdelt/compare", params={"term": "bitcoin", "window_days": 7})
    assert res.status_code == 200
    data = res.json()
    assert data["gdelt_series"] == []
    assert data["gdelt_count"] == 0
    assert "gdelt API unavailable" in data["note"]


def test_gaps_with_three_terms(monkeypatch, tmp_path: Path) -> None:
    per_day = {"alpha": 100, "beta": 40, "gamma": 2}
    with _client(monkeypatch, tmp_path, per_day=per_day) as client:
        res = client.get("/gdelt/gaps", params={"terms": "alpha,beta,gamma", "window_days": 7})
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 3
    assert set(data[0]) == {"term", "local_count", "gdelt_count", "ratio", "gap", "truncated", "note"}
    by_term = {row["term"]: row for row in data}
    assert by_term["alpha"]["gap"] is True
    assert by_term["alpha"]["gdelt_count"] == 100
    assert by_term["beta"]["gap"] is True  # >= 25 volume with zero local capture
    assert by_term["gamma"]["gap"] is False  # below the "big story" bar
    # Gaps first, then by descending gdelt volume.
    assert [row["term"] for row in data] == ["alpha", "beta", "gamma"]


def test_gaps_bad_terms_and_limit(monkeypatch, tmp_path: Path) -> None:
    with _client(monkeypatch, tmp_path) as client:
        empty = client.get("/gdelt/gaps", params={"terms": ""})
        too_many = client.get(
            "/gdelt/gaps", params={"terms": ",".join(f"t{i}" for i in range(21))}
        )
        control = client.get("/gdelt/gaps", params={"terms": "ok,bad\x00term"})
    assert empty.status_code == 400
    assert too_many.status_code == 400
    assert "at most 20" in too_many.json()["detail"]
    assert control.status_code == 400


def test_gaps_index_not_ready_is_503(monkeypatch, tmp_path: Path) -> None:
    with _client(monkeypatch, tmp_path, index=_fake_index(ready=False)) as client:
        res = client.get("/gdelt/gaps", params={"terms": "bitcoin,eth"})
    assert res.status_code == 503
