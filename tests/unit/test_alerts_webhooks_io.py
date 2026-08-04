"""Alerts webhooks I/O: multiple webhooks, Slack delivery, import/export, migration.

Covers the multi-webhook rule surface (``webhooks`` list + ``webhook_url``
compat), Slack-format payloads (explicit hint + ``hooks.slack.com``
auto-detection), rules export/import (store / API / CLI), and the
``webhooks_json`` ALTER TABLE migration for pre-existing databases.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from awareness.alerts import notify
from awareness.alerts.models import AlertFiring, AlertRuleCreate
from awareness.alerts.notify import (
    build_slack_payload,
    detect_webhook_format,
    validate_webhook_url,
)
from awareness.alerts.router import create_alerts_router
from awareness.alerts.store import AlertStore

# Invoke alerts CLI commands through the main app ("awareness alerts ..."):
# cli/main.py moves the sub-app's commands into its own alerts group, so the
# bare alerts Typer instance loses its commands once main is imported.
from awareness.cli.main import app as cli_main_app


@pytest.fixture(autouse=True)
def _allow_public_webhooks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass the public-host DNS gate for unit tests."""
    monkeypatch.setattr(
        "awareness.alerts.notify.is_public_http_url",
        lambda url: True,
    )


def _firing() -> AlertFiring:
    return AlertFiring(
        id=7,
        rule_id="rule-1",
        rule_name="bitcoin watch",
        kind="term_count",
        term="bitcoin",
        count=5,
        threshold=3.0,
        fired_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        detail="5 docs matched 'bitcoin'",
    )


def _create_rule(store: AlertStore, name: str = "bitcoin", **overrides: object) -> Any:
    payload: dict[str, object] = {
        "name": name,
        "kind": "term_count",
        "term": "bitcoin",
        "threshold": 3.0,
        "window_hours": 24.0,
        "cooldown_minutes": 30.0,
        "active": True,
    }
    payload.update(overrides)
    return store.create_rule(AlertRuleCreate(**payload))


# ── multiple webhooks ─────────────────────────────────────────────────────


def test_create_with_webhooks_persists_all(tmp_path: Path) -> None:
    store = AlertStore(tmp_path / "alerts.db")
    try:
        rule = _create_rule(
            store,
            webhooks=[
                "https://hooks.example/a",
                "https://hooks.example/b",
                "https://hooks.example/c",
            ],
        )
        assert rule.webhooks == [
            "https://hooks.example/a",
            "https://hooks.example/b",
            "https://hooks.example/c",
        ]
        assert rule.webhook_url == "https://hooks.example/a"
        fetched = store.get_rule(rule.id)
        assert fetched is not None
        assert fetched.webhooks == rule.webhooks
        listed = next(r for r in store.list_rules() if r.id == rule.id)
        assert listed.webhooks == rule.webhooks
        assert listed.webhook_url == "https://hooks.example/a"
    finally:
        store.close()


def test_webhook_url_compat_seeds_single_webhook(tmp_path: Path) -> None:
    store = AlertStore(tmp_path / "alerts.db")
    try:
        rule = _create_rule(store, webhook_url="https://hooks.example/legacy")
        assert rule.webhooks == ["https://hooks.example/legacy"]
        assert rule.webhook_url == "https://hooks.example/legacy"
    finally:
        store.close()


def test_webhooks_wins_over_webhook_url(tmp_path: Path) -> None:
    store = AlertStore(tmp_path / "alerts.db")
    try:
        rule = _create_rule(
            store,
            webhooks=["https://hooks.example/a"],
            webhook_url="https://hooks.example/legacy",
        )
        assert rule.webhooks == ["https://hooks.example/a"]
        assert rule.webhook_url == "https://hooks.example/a"
    finally:
        store.close()


def test_update_rule_patches_webhooks(tmp_path: Path) -> None:
    store = AlertStore(tmp_path / "alerts.db")
    try:
        rule = _create_rule(store, webhooks=["https://hooks.example/a"])
        updated = store.update_rule(
            rule.id,
            {"webhooks": ["https://hooks.example/x", "https://hooks.example/y"]},
        )
        assert updated.webhooks == ["https://hooks.example/x", "https://hooks.example/y"]
        assert updated.webhook_url == "https://hooks.example/x"
        assert store.get_rule(rule.id).webhooks == updated.webhooks
    finally:
        store.close()


def test_validation_rejects_private_host_in_any_webhook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSRF gate active (autouse bypass undone): ANY URL in the list is checked."""
    monkeypatch.undo()

    def _fake_public(url: str) -> bool:
        return url.startswith("https://hooks.example")

    monkeypatch.setattr(notify, "is_public_http_url", _fake_public)
    with pytest.raises(ValueError):
        AlertRuleCreate(
            name="x",
            kind="term_count",
            term="bitcoin",
            threshold=1.0,
            webhooks=[
                "https://hooks.example/ok",
                "http://127.0.0.1:9000/hook",  # private → reject whole payload
            ],
        )
    with pytest.raises(ValueError):
        validate_webhook_url("http://127.0.0.1:9000/hook")
    assert validate_webhook_url("https://hooks.example/ok") == "https://hooks.example/ok"


# ── Slack delivery ────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code


class _FakeClient:
    """AsyncClient stand-in; records posts."""

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout
        self.posts: list[tuple[str, dict]] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def post(self, url: str, json: dict) -> _FakeResponse:
        self.posts.append((url, json))
        return _FakeResponse(200)


async def test_deliver_slack_format_posts_text(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(timeout=0.0)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake)

    ok = await notify.deliver_webhook(
        "https://hooks.example/alert", _firing(), format="slack"
    )
    assert ok is True
    assert len(fake.posts) == 1
    _, payload = fake.posts[0]
    assert set(payload) == {"text"}
    assert (
        payload["text"]
        == "bitcoin watch: 'bitcoin' fired — count 5 ≥ threshold 3 at "
        "2026-06-01T12:00:00Z (5 docs matched 'bitcoin')"
    )


async def test_deliver_json_format_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(timeout=0.0)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake)

    ok = await notify.deliver_webhook(
        "https://hooks.example/alert", _firing(), format="json"
    )
    assert ok is True
    _, payload = fake.posts[0]
    assert payload["event"] == "alert"
    assert payload["firing"]["id"] == 7
    assert payload["firing"]["fired_at"] == "2026-06-01T12:00:00Z"
    assert "text" not in payload


async def test_slack_host_auto_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(timeout=0.0)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake)

    ok = await notify.deliver_webhook(
        "https://hooks.slack.com/services/T0000/B0000/secret", _firing()
    )
    assert ok is True
    _, payload = fake.posts[0]
    assert set(payload) == {"text"}
    assert payload["text"].startswith("bitcoin watch: 'bitcoin' fired")


def test_detect_webhook_format_hosts() -> None:
    assert detect_webhook_format("https://hooks.slack.com/services/T/B/x") == "slack"
    assert detect_webhook_format("https://hooks.example/alert") == "json"


def test_slack_payload_blocks_fallback_for_long_text() -> None:
    firing = _firing()
    firing.detail = "x" * 5000
    payload = build_slack_payload(firing)
    assert "text" not in payload
    assert "blocks" in payload
    assert all(b["type"] == "section" for b in payload["blocks"])
    chunks = [b["text"]["text"] for b in payload["blocks"]]
    assert "".join(chunks) == notify._slack_text(firing)
    assert all(len(c) <= 3000 for c in chunks)


def test_deliver_unknown_format_raises_valueerror() -> None:
    with pytest.raises(ValueError, match="unknown webhook format"):
        asyncio.run(
            notify.deliver_webhook(
                "https://hooks.example/alert", _firing(), format="pagerduty"
            )
        )


# ── store export / import ─────────────────────────────────────────────────


def test_export_import_roundtrip(tmp_path: Path) -> None:
    src = AlertStore(tmp_path / "src.db")
    try:
        _create_rule(
            src,
            name="alpha",
            webhooks=["https://hooks.example/a", "https://hooks.example/b"],
            webhook_format="slack",
        )
        _create_rule(src, name="beta", webhook_url="https://hooks.example/legacy")
        exported = src.export_rules()
    finally:
        src.close()

    assert len(exported) == 2
    alpha = next(r for r in exported if r["name"] == "alpha")
    assert alpha["webhooks"] == ["https://hooks.example/a", "https://hooks.example/b"]
    assert alpha["webhook_url"] == "https://hooks.example/a"
    assert alpha["webhook_format"] == "slack"
    assert "id" in alpha and "created_at" in alpha

    dst = AlertStore(tmp_path / "dst.db")
    try:
        created, skipped = dst.import_rules(exported)
        assert (created, skipped) == (2, 0)
        rules = {r.name: r for r in dst.list_rules()}
        assert rules["alpha"].webhooks == ["https://hooks.example/a", "https://hooks.example/b"]
        assert rules["alpha"].webhook_format == "slack"
        assert rules["beta"].webhooks == ["https://hooks.example/legacy"]
        assert rules["beta"].webhook_url == "https://hooks.example/legacy"
    finally:
        dst.close()


def test_import_skips_duplicates_and_replace_recreates(tmp_path: Path) -> None:
    store = AlertStore(tmp_path / "alerts.db")
    try:
        rules = [
            {
                "name": "dup",
                "kind": "term_count",
                "term": "bitcoin",
                "threshold": 3.0,
                "webhooks": ["https://hooks.example/a"],
            },
            {
                "name": "fresh",
                "kind": "term_count",
                "term": "ethereum",
                "threshold": 5.0,
            },
        ]
        assert store.import_rules(rules) == (2, 0)
        # Re-import: both names already exist → skipped.
        assert store.import_rules(rules) == (0, 2)
        assert len(store.list_rules()) == 2
        # Re-importing just the first rule is also a skip.
        assert store.import_rules([rules[0]]) == (0, 1)
        # --replace: delete + recreate the existing names.
        replaced = [dict(r, term="updated term") for r in rules]
        assert store.import_rules(replaced, replace=True) == (2, 0)
        assert len(store.list_rules()) == 2
        terms = {r.name: r.term for r in store.list_rules()}
        assert terms["dup"] == "updated term"
    finally:
        store.close()


def test_import_invalid_rule_raises_valueerror(tmp_path: Path) -> None:
    store = AlertStore(tmp_path / "alerts.db")
    try:
        with pytest.raises(ValueError, match="invalid rule 'broken'"):
            store.import_rules([{"name": "broken", "kind": "nope"}])
        with pytest.raises(ValueError, match="invalid rule '<unknown>'"):
            store.import_rules(["not-a-dict"])
    finally:
        store.close()


# ── migration ─────────────────────────────────────────────────────────────


def test_migration_adds_webhook_columns_and_preserves_old_rule(tmp_path: Path) -> None:
    db = tmp_path / "alerts" / "alerts.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE rules (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          kind TEXT NOT NULL,
          term TEXT NOT NULL,
          threshold REAL NOT NULL,
          window_hours REAL NOT NULL,
          webhook_url TEXT,
          cooldown_minutes REAL NOT NULL,
          active INT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE firings (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          rule_id TEXT NOT NULL,
          rule_name TEXT NOT NULL,
          kind TEXT NOT NULL,
          term TEXT NOT NULL,
          count REAL NOT NULL,
          threshold REAL NOT NULL,
          detail TEXT NOT NULL,
          fired_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO rules (id, name, kind, term, threshold, window_hours, "
        "webhook_url, cooldown_minutes, active, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "old-1", "legacy rule", "term_count", "bitcoin", 3.0, 24.0,
            "https://hooks.example/legacy", 30.0, 1,
            "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()

    store = AlertStore(db)  # init() runs the ALTER TABLE migration
    try:
        cols = {
            row["name"]
            for row in store._conn.execute("PRAGMA table_info(rules)").fetchall()
        }
        assert {"webhooks_json", "webhook_format"} <= cols

        rule = store.get_rule("old-1")
        assert rule is not None
        assert rule.webhooks == ["https://hooks.example/legacy"]
        assert rule.webhook_url == "https://hooks.example/legacy"
        assert rule.webhook_format == "json"

        # Post-migration writes include the new columns.
        fresh = _create_rule(store, name="new", webhooks=["https://hooks.example/x"])
        assert fresh.webhooks == ["https://hooks.example/x"]
    finally:
        store.close()


def test_migration_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "alerts.db"
    store1 = AlertStore(db)
    store1.close()
    store2 = AlertStore(db)  # second init must not re-ALTER or fail
    try:
        assert store2.list_rules() == []
    finally:
        store2.close()


# ── API export / import ───────────────────────────────────────────────────


class _FakeIndex:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready

    def health_snapshot(self) -> dict[str, Any]:
        if not self.ready:
            raise RuntimeError("index not ready")
        return {"ready": True}

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if "count" in sql.lower():
            return [{"n": 5}]
        return []


def _api_client(tmp_path: Path, *, index_ready: bool = True) -> TestClient:
    store = AlertStore(tmp_path / "alerts.db")
    index = _FakeIndex(ready=index_ready)
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


def test_api_export_200_includes_webhooks(tmp_path: Path) -> None:
    with _api_client(tmp_path) as client:
        client.post(
            "/alerts/rules",
            json=_rule_payload(
                name="multi",
                webhooks=["https://hooks.example/a", "https://hooks.example/b"],
            ),
        )
        res = client.get("/alerts/rules/export")
    assert res.status_code == 200
    body = res.json()
    assert isinstance(body, list) and len(body) == 1
    assert body[0]["webhooks"] == ["https://hooks.example/a", "https://hooks.example/b"]
    assert body[0]["webhook_url"] == "https://hooks.example/a"


def test_api_export_503_when_index_not_ready(tmp_path: Path) -> None:
    with _api_client(tmp_path, index_ready=False) as client:
        assert client.get("/alerts/rules/export").status_code == 503


def test_api_import_200_and_dedup(tmp_path: Path) -> None:
    rules = [
        _rule_payload(name="one", webhooks=["https://hooks.example/a"]),
        _rule_payload(name="two"),
    ]
    with _api_client(tmp_path) as client:
        first = client.post("/alerts/rules/import", json=rules)
        assert first.status_code == 200
        assert first.json() == {"created": 2, "skipped": 0}
        second = client.post("/alerts/rules/import", json=rules)
        assert second.json() == {"created": 0, "skipped": 2}
        wrapped = client.post(
            "/alerts/rules/import",
            json={"rules": rules, "replace": True},
        )
        assert wrapped.status_code == 200
        assert wrapped.json() == {"created": 2, "skipped": 0}
        assert len(client.get("/alerts/rules").json()) == 2


def test_api_import_400_on_invalid_rule(tmp_path: Path) -> None:
    with _api_client(tmp_path) as client:
        res = client.post("/alerts/rules/import", json=[_rule_payload(kind="nope")])
        assert res.status_code == 400
        res2 = client.post("/alerts/rules/import", json={"rules": "nope"})
        assert res2.status_code == 400
        res3 = client.post("/alerts/rules/import", json=42)
        assert res3.status_code == 400
        assert len(client.get("/alerts/rules").json()) == 0


def test_api_import_400_on_private_webhook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()

    def _fake_public(url: str) -> bool:
        return url.startswith("https://hooks.example")

    from awareness.alerts import notify as notify_mod  # noqa: PLC0415

    monkeypatch.setattr(notify_mod, "is_public_http_url", _fake_public)
    with _api_client(tmp_path) as client:
        res = client.post(
            "/alerts/rules/import",
            json=[_rule_payload(webhooks=["http://127.0.0.1:9000/hook"])],
        )
        assert res.status_code == 400


def test_api_import_works_without_index(tmp_path: Path) -> None:
    with _api_client(tmp_path, index_ready=False) as client:
        res = client.post("/alerts/rules/import", json=[_rule_payload(name="offline")])
    assert res.status_code == 200
    assert res.json() == {"created": 1, "skipped": 0}


def test_api_check_delivers_to_all_webhooks_with_rule_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from awareness.alerts import router as alerts_router_mod  # noqa: PLC0415

    calls: list[tuple[str, object, str | None]] = []

    async def _recording_deliver(url: str, firing: object, format: str | None = None) -> bool:
        calls.append((url, firing, format))
        return True

    monkeypatch.setattr(alerts_router_mod, "deliver_webhook", _recording_deliver)
    with _api_client(tmp_path, index_ready=True) as client:
        client.post(
            "/alerts/rules",
            json=_rule_payload(
                webhooks=["https://hooks.example/a", "https://hooks.example/b"],
                webhook_format="slack",
            ),
        )
        res = client.post("/alerts/check")
    assert res.status_code == 200
    body = res.json()
    assert len(body["deliveries"]) == 2
    assert body["deliveries"] == [
        {"rule_id": body["firings"][0]["rule_id"], "webhook_url": "https://hooks.example/a", "delivered": True},
        {"rule_id": body["firings"][0]["rule_id"], "webhook_url": "https://hooks.example/b", "delivered": True},
    ]
    assert [(url, fmt) for url, _, fmt in calls] == [
        ("https://hooks.example/a", "slack"),
        ("https://hooks.example/b", "slack"),
    ]


# ── CLI export / import ───────────────────────────────────────────────────


def _cli() -> CliRunner:
    return CliRunner()


def test_cli_create_with_webhooks_then_export_import(tmp_project: Path) -> None:
    runner = _cli()
    res = runner.invoke(
        cli_main_app,
        [
            "alerts", "create", "--name", "alpha", "--kind", "term_count",
            "--term", "bitcoin", "--threshold", "3",
            "--webhook", "https://hooks.example/a",
            "--webhook", "https://hooks.example/b",
        ],
    )
    assert res.exit_code == 0, res.output
    assert "Created rule" in res.output

    out_file = tmp_project / "rules.json"
    exp = runner.invoke(cli_main_app, ["alerts", "export", "--out", str(out_file)])
    assert exp.exit_code == 0, exp.output
    exported = json.loads(out_file.read_text(encoding="utf-8"))
    assert len(exported) == 1
    assert exported[0]["webhooks"] == ["https://hooks.example/a", "https://hooks.example/b"]

    imp = runner.invoke(cli_main_app, ["alerts", "import", str(out_file)])
    assert imp.exit_code == 0, imp.output
    assert "Imported 0 rules, skipped 1" in imp.output
    assert "skipped existing rule 'alpha'" in imp.output

    rep = runner.invoke(cli_main_app, ["alerts", "import", str(out_file), "--replace"])
    assert rep.exit_code == 0, rep.output
    assert "Imported 1 rules, skipped 0" in rep.output


def test_cli_export_to_stdout(tmp_project: Path) -> None:
    runner = _cli()
    runner.invoke(
        cli_main_app,
        ["alerts", "create", "--name", "beta", "--term", "ethereum", "--threshold", "2"],
    )
    res = runner.invoke(cli_main_app, ["alerts", "export"])
    assert res.exit_code == 0, res.output
    rules = json.loads(res.output)
    assert rules[0]["name"] == "beta"


def test_cli_import_errors_exit_1(tmp_project: Path) -> None:
    runner = _cli()
    missing = runner.invoke(cli_main_app, ["alerts", "import", str(tmp_project / "nope.json")])
    assert missing.exit_code == 1
    assert "import failed" in missing.output

    bad_file = tmp_project / "bad.json"
    bad_file.write_text("{not json", encoding="utf-8")
    bad = runner.invoke(cli_main_app, ["alerts", "import", str(bad_file)])
    assert bad.exit_code == 1
    assert "import failed" in bad.output

    obj_file = tmp_project / "obj.json"
    obj_file.write_text('{"rules": []}', encoding="utf-8")
    obj = runner.invoke(cli_main_app, ["alerts", "import", str(obj_file)])
    assert obj.exit_code == 1
    assert "JSON array" in obj.output

    invalid_file = tmp_project / "invalid.json"
    invalid_file.write_text(json.dumps([{"name": "broken", "kind": "nope"}]), encoding="utf-8")
    invalid = runner.invoke(cli_main_app, ["alerts", "import", str(invalid_file)])
    assert invalid.exit_code == 1
    assert "import failed" in invalid.output


def test_cli_create_rejects_private_webhook(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.undo()

    def _fake_public(url: str) -> bool:
        return url.startswith("https://hooks.example")

    from awareness.alerts import notify as notify_mod  # noqa: PLC0415

    monkeypatch.setattr(notify_mod, "is_public_http_url", _fake_public)
    runner = _cli()
    res = runner.invoke(
        cli_main_app,
        [
            "alerts", "create", "--name", "x", "--term", "bitcoin",
            "--threshold", "1", "--webhook", "http://127.0.0.1:9000/hook",
        ],
    )
    assert res.exit_code == 2
    assert "invalid webhook URL" in res.output
    list_res = runner.invoke(cli_main_app, ["alerts", "list"])
    assert list_res.exit_code == 0
    assert "No alert rules configured." in list_res.output
