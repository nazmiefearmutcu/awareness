"""Tests for the single-rule test area: engine ``check_rule(ignore_cooldown=...)`` /
``check_rule_report`` and the ``POST /alerts/rules/{rule_id}/test`` endpoint.

Engine tests build small in-memory corpora (same JSONL-chunk pattern as
``test_alerts_engine.py``); API tests mount the router on a fake index
(same pattern as ``test_alerts_api.py``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from awareness.alerts.engine import AlertEngine, RuleCheckReport
from awareness.alerts.models import AlertRuleCreate
from awareness.alerts.router import create_alerts_router
from awareness.alerts.store import AlertStore
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


def _write_doc(
    root: Path,
    idx: int,
    *,
    ts: datetime,
    title: str = "",
    text: str = "",
    domain: str = "example.com",
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
    )
    (day / f"chunk-{idx:04d}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _index(tmp_path: Path) -> DuckDbIndex:
    return DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )


def _store(tmp_path: Path) -> AlertStore:
    return AlertStore(tmp_path / "alerts" / "alerts.db")


def _rule(store: AlertStore, **overrides: object) -> object:
    payload = {
        "name": "bitcoin watch",
        "kind": "term_count",
        "term": "bitcoin",
        "threshold": 3.0,
        "window_hours": 24.0,
        "cooldown_minutes": 60.0,
        "active": True,
        **overrides,
    }
    return store.create_rule(AlertRuleCreate(**payload))


# ── engine: check_rule ignore_cooldown ───────────────────────────────────────


def test_check_rule_ignore_cooldown_fires_despite_cooldown(tmp_path: Path) -> None:
    index = _index(tmp_path)
    store = _store(tmp_path)
    try:
        rule = _rule(store, threshold=1.0, cooldown_minutes=60.0)
        _write_doc(tmp_path / "jsonl", 1, ts=datetime.now(UTC), title="Bitcoin hot")

        engine = AlertEngine(index, store)
        first = engine.check_rule(rule.id)
        assert first is not None
        assert first.id > 0  # normal mode persists
        # Normal mode within cooldown: suppressed.
        assert engine.check_rule(rule.id) is None
        # Test mode: the current condition is surfaced despite the cooldown.
        test_firing = engine.check_rule(rule.id, ignore_cooldown=True)
        assert test_firing is not None
        assert test_firing.rule_id == rule.id
        assert test_firing.count == 1
        # Test mode never persists: still exactly the one recorded firing.
        assert len(store.list_firings()) == 1
    finally:
        index.close()
        store.close()


def test_check_rule_test_mode_does_not_persist_placeholder_id(tmp_path: Path) -> None:
    index = _index(tmp_path)
    store = _store(tmp_path)
    try:
        rule = _rule(store, threshold=1.0)
        _write_doc(tmp_path / "jsonl", 1, ts=datetime.now(UTC), title="Bitcoin now")

        engine = AlertEngine(index, store)
        firing = engine.check_rule(rule.id, ignore_cooldown=True)
        assert firing is not None
        assert firing.id == 0  # not recorded → placeholder id
        assert store.list_firings() == []
    finally:
        index.close()
        store.close()


# ── engine: check_rule_report ────────────────────────────────────────────────


def test_check_rule_report_fired_with_cooldown_flag(tmp_path: Path) -> None:
    index = _index(tmp_path)
    store = _store(tmp_path)
    try:
        rule = _rule(store, threshold=1.0, cooldown_minutes=60.0)
        _write_doc(tmp_path / "jsonl", 1, ts=datetime.now(UTC), title="Bitcoin hot")

        engine = AlertEngine(index, store)
        assert engine.check_rule(rule.id) is not None  # persist one firing
        report = engine.check_rule_report(rule.id)
        assert isinstance(report, RuleCheckReport)
        assert report.fired is True
        assert report.firing is not None
        assert report.firing.count == 1
        assert report.count == 1
        assert report.threshold == 1.0
        assert report.suppressed_by_cooldown is True  # would be suppressed in a real run
        # The report itself never persisted anything.
        assert len(store.list_firings()) == 1
    finally:
        index.close()
        store.close()


def test_check_rule_report_not_fired_exposes_count(tmp_path: Path) -> None:
    index = _index(tmp_path)
    store = _store(tmp_path)
    try:
        rule = _rule(store, threshold=5.0)
        base = datetime.now(UTC).replace(microsecond=0)
        _write_doc(tmp_path / "jsonl", 1, ts=base - timedelta(hours=1), title="Bitcoin dip")

        report = AlertEngine(index, store).check_rule_report(rule.id)
        assert report is not None
        assert report.fired is False
        assert report.firing is None
        assert report.count == 1
        assert report.threshold == 5.0
        assert report.suppressed_by_cooldown is False
    finally:
        index.close()
        store.close()


def test_check_rule_report_unknown_rule_none(tmp_path: Path) -> None:
    index = _index(tmp_path)
    store = _store(tmp_path)
    try:
        assert AlertEngine(index, store).check_rule_report("missing") is None
    finally:
        index.close()
        store.close()


def test_check_rule_report_evaluates_inactive_rule(tmp_path: Path) -> None:
    index = _index(tmp_path)
    store = _store(tmp_path)
    try:
        rule = _rule(store, threshold=1.0, active=False)
        _write_doc(tmp_path / "jsonl", 1, ts=datetime.now(UTC), title="Bitcoin latent")

        report = AlertEngine(index, store).check_rule_report(rule.id)
        assert report is not None
        assert report.fired is True  # a test is explicit: shows the live condition
    finally:
        index.close()
        store.close()


# ── API: POST /alerts/rules/{rule_id}/test ───────────────────────────────────


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


def _create_rule(client: TestClient, **overrides: object) -> dict[str, Any]:
    res = client.post("/alerts/rules", json=_rule_payload(**overrides))
    assert res.status_code == 201
    return res.json()


def test_api_test_rule_200_fired(tmp_path: Path) -> None:
    with _client(tmp_path, index_count=5) as client:
        rule = _create_rule(client, threshold=3.0)
        res = client.post(f"/alerts/rules/{rule['id']}/test")
    assert res.status_code == 200
    body = res.json()
    assert body["fired"] is True
    assert body["count"] == 5
    assert body["threshold"] == 3.0
    assert body["suppressed_by_cooldown"] is False
    firing = body["firing"]
    assert firing is not None
    assert firing["rule_id"] == rule["id"]
    assert firing["term"] == "bitcoin"
    assert firing["count"] == 5


def test_api_test_rule_200_not_fired(tmp_path: Path) -> None:
    with _client(tmp_path, index_count=1) as client:
        rule = _create_rule(client, threshold=5.0)
        res = client.post(f"/alerts/rules/{rule['id']}/test")
    assert res.status_code == 200
    body = res.json()
    assert body["fired"] is False
    assert body["firing"] is None
    assert body["count"] == 1
    assert body["threshold"] == 5.0
    assert body["suppressed_by_cooldown"] is False


def test_api_test_rule_404_unknown(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        res = client.post("/alerts/rules/missing/test")
    assert res.status_code == 404
    assert res.json()["detail"] == "alert rule not found"


def test_api_test_rule_503_index_not_ready(tmp_path: Path) -> None:
    with _client(tmp_path, index_ready=False) as client:
        res = client.post("/alerts/rules/any-id/test")
    assert res.status_code == 503
    assert "index not ready" in res.json()["detail"]


def test_api_test_rule_does_not_persist(tmp_path: Path) -> None:
    with _client(tmp_path, index_count=5) as client:
        rule = _create_rule(client, threshold=3.0)
        assert client.post(f"/alerts/rules/{rule['id']}/test").status_code == 200
        assert client.get("/alerts/firings").json() == []


def test_api_test_rule_reports_cooldown_suppression(tmp_path: Path) -> None:
    with _client(tmp_path, index_count=5) as client:
        rule = _create_rule(client, threshold=3.0)
        # A real check persists a firing → the rule enters its cooldown window.
        check = client.post("/alerts/check")
        assert check.status_code == 200
        assert len(check.json()["firings"]) == 1
        res = client.post(f"/alerts/rules/{rule['id']}/test")
    assert res.status_code == 200
    body = res.json()
    assert body["fired"] is True  # test bypasses cooldown
    assert body["suppressed_by_cooldown"] is True  # a real run would be suppressed
