"""API tests for the entities router."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from awareness.entities.router import create_entities_router


def _fake_index(ready: bool = True):
    class _Idx:
        def health_snapshot(self) -> dict:
            return {"ready": ready}

        def execute(self, sql: str, params: dict | None = None) -> list[dict]:
            if "regexp_matches" not in sql:
                return []
            return []

    return _Idx()


def _client(ready: bool = True) -> TestClient:
    app = FastAPI()
    app.include_router(create_entities_router(lambda: _fake_index(ready)))
    return TestClient(app)


def test_top_ok() -> None:
    resp = _client().get("/entities/top")
    assert resp.status_code == 200
    assert resp.json() == []


def test_top_bad_limit() -> None:
    assert _client().get("/entities/top?limit=0").status_code == 422
    assert _client().get("/entities/top?limit=9999").status_code == 422


def test_cooccurring_requires_entity() -> None:
    assert _client().get("/entities/co-occurring").status_code == 400
    assert _client().get("/entities/co-occurring?entity=").status_code == 400


def test_cooccurring_ok() -> None:
    resp = _client().get("/entities/co-occurring?entity=BTC")
    assert resp.status_code == 200


def test_trend_ok() -> None:
    resp = _client().get("/entities/trend?entity=BTC&window_days=7&granularity=day")
    assert resp.status_code == 200


def test_trend_bad_granularity() -> None:
    resp = _client().get("/entities/trend?entity=BTC&granularity=hour")
    assert resp.status_code == 422


def test_correlation_ok() -> None:
    resp = _client().get("/entities/correlation?a=BTC&b=ETH")
    assert resp.status_code == 200
    body = resp.json()
    assert body["r"] == 0.0
    assert body["n"] >= 0


def test_correlation_missing_param() -> None:
    assert _client().get("/entities/correlation?a=BTC").status_code == 400


def test_not_ready_503() -> None:
    client = _client(ready=False)
    for path in ("/entities/top", "/entities/trend?entity=BTC", "/entities/correlation?a=BTC&b=ETH"):
        assert client.get(path).status_code == 503
