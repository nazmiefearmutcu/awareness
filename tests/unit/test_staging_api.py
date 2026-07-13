"""GET /staging: JSONL staging backlog + oldest age (compact --status parity)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import awareness.api.server as server
from awareness.storage.state import ManifestRow, StateDB
from sqlalchemy import select


def _staging_endpoint(app):
    for route in app.routes:
        if getattr(route, "path", None) == "/staging" and "GET" in getattr(
            route, "methods", set()
        ):
            return route.endpoint
    raise AssertionError("/staging route not found")


def _state(tmp_path: Path) -> StateDB:
    db = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    db.init()
    return db


def test_staging_route_registered() -> None:
    app = server.create_app()
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/staging" in paths


def test_staging_empty(tmp_path: Path, monkeypatch) -> None:
    app = server.create_app()
    st = _state(tmp_path)
    server._State.state = st
    try:
        payload = _staging_endpoint(app)()
        assert payload["pending_count"] == 0
        assert payload["total_records"] == 0
        assert payload["total_bytes"] == 0
        assert payload["oldest_committed_at"] is None
        assert payload["oldest_age_seconds"] is None
        assert payload.get("manifests") == []
    finally:
        server._State.state = None


def test_staging_summary_with_age(tmp_path: Path) -> None:
    app = server.create_app()
    st = _state(tmp_path)
    st.add_manifest("/data/new.jsonl.gz", records=3, bytes_=300)
    st.add_manifest("/data/old.jsonl.gz", records=7, bytes_=700)
    old_ts = datetime.now(UTC) - timedelta(hours=2)
    with st.session() as s:
        rows = list(s.scalars(select(ManifestRow).order_by(ManifestRow.id)))
        rows[1].committed_at = old_ts
        s.commit()

    server._State.state = st
    try:
        payload = _staging_endpoint(app)()
        assert payload["pending_count"] == 2
        assert payload["total_records"] == 10
        assert payload["total_bytes"] == 1000
        assert payload["oldest_committed_at"] is not None
        assert payload["oldest_age_seconds"] is not None
        assert payload["oldest_age_seconds"] >= 2 * 3600 - 5
        assert len(payload["manifests"]) == 2
    finally:
        server._State.state = None


def test_staging_without_manifests_list(tmp_path: Path) -> None:
    app = server.create_app()
    st = _state(tmp_path)
    st.add_manifest("/data/a.jsonl.gz", records=1, bytes_=10)
    server._State.state = st
    try:
        endpoint = _staging_endpoint(app)
        # FastAPI injects Query defaults; call with include_manifests=False.
        payload = endpoint(include_manifests=False)
        assert payload["pending_count"] == 1
        assert payload["total_records"] == 1
        assert "manifests" not in payload
        assert "oldest_age_seconds" in payload
    finally:
        server._State.state = None


def test_staging_requires_state() -> None:
    app = server.create_app()
    server._State.state = None
    from fastapi import HTTPException

    try:
        _staging_endpoint(app)()
        raise AssertionError("expected HTTPException")
    except HTTPException as exc:
        assert exc.status_code == 500


def test_cli_status_shows_staging_backlog(tmp_path: Path, monkeypatch) -> None:
    """``awareness status`` surfaces pending staging age (compact --status parity)."""
    from typer.testing import CliRunner

    from awareness.cli.main import app

    st = _state(tmp_path)
    st.add_manifest(str(tmp_path / "chunk.jsonl.gz"), records=4, bytes_=400)
    old_ts = datetime.now(UTC) - timedelta(minutes=90)
    with st.session() as s:
        rows = list(s.scalars(select(ManifestRow)))
        rows[0].committed_at = old_ts
        s.commit()

    monkeypatch.setattr("awareness.cli.main._bootstrap", lambda: (st, None))
    monkeypatch.setattr("awareness.cli.main._get_api_pid", lambda: None)
    monkeypatch.setattr("awareness.cli.main._is_port_active", lambda *a, **k: False)
    monkeypatch.setattr(
        "awareness.cli.main._query_db_metrics",
        lambda _s: {
            "total_bytes_processed": 0,
            "total_docs_emitted": 0,
            "total_docs_dedup_dropped": 0,
            "manifests_count": 1,
            "manifests_compacted_count": 0,
            "dlq_count": 0,
        },
    )

    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    out = result.output.lower()
    assert "staging pending" in out
    assert "1" in result.output
    # Age should mention minutes/hours (90m → 1h30m or similar)
    assert "oldest" in out
