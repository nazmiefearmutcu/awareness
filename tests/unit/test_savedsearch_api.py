"""API tests for the /saved router (200 / 201 / 400 / 404 / 503 handling).

Mounts :func:`~awareness.savedsearch.router.create_savedsearch_router` on a
bare FastAPI app wired to a real tmp-file SavedSearchStore plus a fake index
getter (only touched by ``/run``), and drives it with FastAPI's TestClient.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from awareness.savedsearch.router import create_savedsearch_router
from awareness.savedsearch.store import SavedSearchStore


class _FakeIndex:
    """DuckDbIndex stand-in: canned readiness + canned search payload."""

    def __init__(self, *, ready: bool = True, total: int = 2) -> None:
        self.ready = ready
        self.total = total
        self.calls: list[dict[str, Any]] = []

    def health_snapshot(self) -> dict[str, Any]:
        if not self.ready:
            raise RuntimeError("index not ready")
        return {"ready": True}

    def search(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"query": query, **kwargs})
        rows = [
            {
                "capture_id": "cap-1",
                "doc_id": "doc-1",
                "title": "Alpha one",
                "domain": "a.example",
                "source_type": "rss",
                "fetch_ts": "2026-06-01T12:00:00+00:00",
                "text_len": 42,
                "snippet": "alpha news",
                "score": 1.5,
            },
            {
                "capture_id": "cap-2",
                "doc_id": "doc-2",
                "title": "Alpha two",
                "domain": "b.example",
                "source_type": "rss",
                "fetch_ts": "2026-06-01T13:00:00+00:00",
                "text_len": 33,
                "snippet": "alpha again",
                "score": 0.9,
            },
        ][: self.total]
        return {
            "total": len(rows),
            "limit": kwargs.get("limit", 10),
            "offset": 0,
            "rows": rows,
            "ranked": True,
            "mode": kwargs.get("mode", "auto"),
            "fields": ["title", "text"],
            "query": query,
        }


def _client(
    tmp_path: Path,
    *,
    index_ready: bool = True,
    index_total: int = 2,
) -> tuple[TestClient, _FakeIndex]:
    store = SavedSearchStore(tmp_path / "saved_searches.db")
    index = _FakeIndex(ready=index_ready, total=index_total)
    app = FastAPI()
    app.include_router(create_savedsearch_router(lambda: store, lambda: index))
    return TestClient(app), index


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "alpha watch",
        "query": "alpha",
        "mode": "auto",
        "fields": "title,text",
        "limit": 10,
    }
    payload.update(overrides)
    return payload


def test_create_201(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    with client:
        res = client.post("/saved", json=_payload())
    assert res.status_code == 201
    body = res.json()
    assert body["id"]
    assert body["name"] == "alpha watch"
    assert body["query"] == "alpha"
    assert body["mode"] == "auto"
    assert body["fields"] == "title,text"
    assert body["limit"] == 10
    assert body["pinned"] is False
    assert body["created_at"]
    assert body["updated_at"] == body["created_at"]


def test_create_defaults_and_custom(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    with client:
        res = client.post("/saved", json={"name": "x", "query": "y"})
    assert res.status_code == 201
    body = res.json()
    assert body["mode"] == "auto"
    assert body["fields"] == "title,text"
    assert body["limit"] == 10


def test_create_400_on_bad_payload(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    with client:
        assert client.post("/saved", json=_payload(name="")).status_code == 400
        assert client.post("/saved", json=_payload(query="")).status_code == 400
        assert client.post("/saved", json=_payload(query="bad\x00q")).status_code == 400
        assert client.post("/saved", json=_payload(mode="nope")).status_code == 400
        assert client.post("/saved", json=_payload(limit=0)).status_code == 400
        assert client.post("/saved", json=_payload(limit=999)).status_code == 400
        assert client.post("/saved", json=_payload(fields="   ")).status_code == 400


def test_list_200_pinned_first(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    with client:
        a = client.post("/saved", json=_payload(name="a")).json()
        client.post("/saved", json=_payload(name="b"))
        client.post(f"/saved/{a['id']}/pin", json={"pinned": True})
        res = client.get("/saved")
    assert res.status_code == 200
    names = [s["name"] for s in res.json()]
    assert names == ["a", "b"]


def test_get_200_and_404(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    with client:
        created = client.post("/saved", json=_payload()).json()
        res = client.get(f"/saved/{created['id']}")
        assert res.status_code == 200
        assert res.json()["id"] == created["id"]
        assert client.get("/saved/missing").status_code == 404


def test_update_200_400_404(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    with client:
        created = client.post("/saved", json=_payload()).json()
        res = client.put(f"/saved/{created['id']}", json={"name": "renamed", "limit": 25})
        assert res.status_code == 200
        assert res.json()["name"] == "renamed"
        assert res.json()["limit"] == 25
        assert res.json()["query"] == "alpha"
        assert client.put(f"/saved/{created['id']}", json={"bogus": 1}).status_code == 400
        assert client.put(f"/saved/{created['id']}", json={"query": "bad\x00q"}).status_code == 400
        assert client.put("/saved/missing", json={"name": "x"}).status_code == 404


def test_delete_204_then_404(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    with client:
        created = client.post("/saved", json=_payload()).json()
        assert client.delete(f"/saved/{created['id']}").status_code == 204
        assert client.delete(f"/saved/{created['id']}").status_code == 404


def test_pin_200_and_404(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    with client:
        created = client.post("/saved", json=_payload()).json()
        res = client.post(f"/saved/{created['id']}/pin", json={"pinned": True})
        assert res.status_code == 200
        assert res.json()["pinned"] is True
        res = client.post(f"/saved/{created['id']}/pin", json={"pinned": False})
        assert res.json()["pinned"] is False
        assert client.post("/saved/missing/pin", json={"pinned": True}).status_code == 404


def test_crud_does_not_touch_index(tmp_path: Path) -> None:
    client, index = _client(tmp_path)
    with client:
        created = client.post("/saved", json=_payload()).json()
        client.put(f"/saved/{created['id']}", json={"name": "x"})
        client.get("/saved")
        client.get(f"/saved/{created['id']}")
        client.post(f"/saved/{created['id']}/pin", json={"pinned": True})
        client.delete(f"/saved/{created['id']}")
    assert index.calls == []


def test_run_200_payload_and_touch(tmp_path: Path) -> None:
    client, index = _client(tmp_path, index_total=2)
    with client:
        created = client.post("/saved", json=_payload(mode="substring", limit=5)).json()
        before = client.get(f"/saved/{created['id']}").json()
        res = client.get(f"/saved/{created['id']}/run")
        after = client.get(f"/saved/{created['id']}").json()
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 2
    assert len(body["rows"]) == 2
    assert body["ranked"] is True
    assert body["mode"] == "substring"
    assert body["rows"][0]["title"] == "Alpha one"
    # The index got the saved query/mode/limit/fields.
    assert index.calls == [
        {
            "query": "alpha",
            "limit": 5,
            "mode": "substring",
            "fields": ["title", "text"],
        }
    ]
    # updated_at bumped as a last-run marker; created_at untouched.
    assert after["updated_at"] >= before["updated_at"]
    assert after["created_at"] == before["created_at"]


def test_run_404_unknown_id(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    with client:
        assert client.get("/saved/missing/run").status_code == 404


def test_503_when_index_not_ready(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, index_ready=False)
    with client:
        created = client.post("/saved", json=_payload()).json()
        assert created["id"]
        # CRUD still works without the index.
        assert client.get("/saved").status_code == 200
        # Only /run is gated on readiness.
        assert client.get(f"/saved/{created['id']}/run").status_code == 503
