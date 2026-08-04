"""API tests for the /alerts router (200 / 201 / 400 / 404 / 503 handling).

Mounts :func:`~awareness.alerts.router.create_alerts_router` on a bare
FastAPI app wired to a real tmp-file AlertStore plus a fake index getter, and
drives it with FastAPI's TestClient.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from awareness.alerts import router as alerts_router
from awareness.alerts.router import create_alerts_router
from awareness.alerts.store import AlertStore


class _FakeIndex:
    """DuckDbIndex stand-in: canned health + canned count responses."""

    def __init__(self, *, ready: bool = True, count: int = 5) -> None:
        self.ready = ready
        self.count = count

    def health_snapshot(self) -> dict[str, Any]:
        if not self.ready:
            raise RuntimeError("index not ready")
        return {"ready": True}

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if "count" in sql.lower():
            return [{"n": self.count}]
        return []


def _client(tmp_path: Path, *, index_ready: bool = True, index_count: int = 5) -> TestClient:
    store = AlertStore(tmp_path / "alerts.db")
    index = _FakeIndex(ready=index_ready, count=index_count)
    app = FastAPI()
    app.include_router(create_alerts_router(lambda: index, lambda: store))
    return TestClient(app)


def _rule_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "bitcoin watch",
        "kind": "term_count",
        "term": "bitcoin",
        "threshold": 3.0,
        "window_hours": 24.0,
        "cooldown_minutes": 30.0,
        "active": True,
    }
    payload.update(overrides)
    return payload


def test_create_rule_201(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        res = client.post("/alerts/rules", json=_rule_payload())
    assert res.status_code == 201
    body = res.json()
    assert body["id"]
    assert body["name"] == "bitcoin watch"
    assert body["term"] == "bitcoin"
    assert body["created_at"]
    assert body["updated_at"] == body["created_at"]


def test_create_rule_400_on_bad_payload(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        assert client.post("/alerts/rules", json=_rule_payload(term="bad\x00term")).status_code == 400
        assert client.post("/alerts/rules", json=_rule_payload(kind="nope")).status_code == 400
        assert client.post("/alerts/rules", json=_rule_payload(term="")).status_code == 400
        assert client.post("/alerts/rules", json=_rule_payload(threshold=-1.0)).status_code == 400


def test_list_rules_200(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        client.post("/alerts/rules", json=_rule_payload(name="a"))
        client.post("/alerts/rules", json=_rule_payload(name="b"))
        res = client.get("/alerts/rules")
    assert res.status_code == 200
    assert len(res.json()) == 2


def test_get_rule_200_and_404(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        created = client.post("/alerts/rules", json=_rule_payload()).json()
        res = client.get(f"/alerts/rules/{created['id']}")
        assert res.status_code == 200
        assert res.json()["id"] == created["id"]
        assert client.get("/alerts/rules/missing").status_code == 404


def test_update_rule_200_400_404(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        created = client.post("/alerts/rules", json=_rule_payload()).json()
        res = client.put(f"/alerts/rules/{created['id']}", json={"threshold": 9.0})
        assert res.status_code == 200
        assert res.json()["threshold"] == 9.0
        assert res.json()["name"] == "bitcoin watch"
        assert client.put(f"/alerts/rules/{created['id']}", json={"bogus": 1}).status_code == 400
        assert client.put("/alerts/rules/missing", json={"threshold": 1.0}).status_code == 404


def test_delete_rule_204_then_404(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        created = client.post("/alerts/rules", json=_rule_payload()).json()
        assert client.delete(f"/alerts/rules/{created['id']}").status_code == 204
        assert client.delete(f"/alerts/rules/{created['id']}").status_code == 404


def test_check_ok_without_rules(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        res = client.post("/alerts/check")
    assert res.status_code == 200
    assert res.json() == {"firings": [], "deliveries": []}


def test_check_fires_and_delivers_webhook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_deliver(url: str, firing: object, format: str | None = None) -> bool:
        return True

    monkeypatch.setattr(alerts_router, "deliver_webhook", _fake_deliver)
    # Rule creation now validates webhook URLs against the public-host gate;
    # bypass the internal validator for the fixture host.
    monkeypatch.setattr(
        "awareness.alerts.notify.is_public_http_url",
        lambda url: True,
    )
    with _client(tmp_path, index_count=5) as client:
        rule = client.post(
            "/alerts/rules",
            json=_rule_payload(webhook_url="https://hooks.example/alert"),
        ).json()
        res = client.post("/alerts/check")
    assert res.status_code == 200
    body = res.json()
    assert len(body["firings"]) == 1
    firing = body["firings"][0]
    assert firing["rule_id"] == rule["id"]
    assert firing["count"] == 5
    assert body["deliveries"] == [
        {"rule_id": rule["id"], "webhook_url": "https://hooks.example/alert", "delivered": True}
    ]


def test_status_200(tmp_path: Path) -> None:
    with _client(tmp_path, index_count=3) as client:
        client.post("/alerts/rules", json=_rule_payload())
        client.post("/alerts/rules", json=_rule_payload(name="inactive", active=False))
        client.post("/alerts/check")  # fires the active rule once
        res = client.get("/alerts/status")
    assert res.status_code == 200
    body = res.json()
    assert body["rules_total"] == 2
    assert body["rules_active"] == 1
    assert body["firings_24h"] == 1
    assert body["last_firing"] is not None


def test_firings_list_and_limit_clamp(tmp_path: Path) -> None:
    with _client(tmp_path, index_count=3) as client:
        for i in range(3):
            client.post("/alerts/rules", json=_rule_payload(name=f"rule {i}"))
        client.post("/alerts/check")  # 3 firings recorded
        res = client.get("/alerts/firings", params={"limit": 2})
        assert res.status_code == 200
        assert len(res.json()) == 2
        assert len(client.get("/alerts/firings", params={"limit": 0}).json()) == 1
        assert len(client.get("/alerts/firings", params={"limit": 9999}).json()) == 3


def test_503_when_index_not_ready(tmp_path: Path) -> None:
    with _client(tmp_path, index_ready=False) as client:
        assert client.get("/alerts/rules").status_code == 503
        assert client.post("/alerts/rules", json=_rule_payload()).status_code == 503
        assert client.get("/alerts/rules/some-id").status_code == 503
        assert client.put("/alerts/rules/some-id", json={"threshold": 1.0}).status_code == 503
        assert client.delete("/alerts/rules/some-id").status_code == 503
        assert client.post("/alerts/check").status_code == 503
        assert client.get("/alerts/status").status_code == 503
        assert client.get("/alerts/firings").status_code == 503
