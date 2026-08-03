"""Unit tests for the alert evaluation engine.

Builds small in-memory corpora through the same JSONL-chunk pattern as the
rest of the unit suite (see ``test_analytics_engine.py``) and drives
:class:`~awareness.alerts.engine.AlertEngine` against them with a tmp sqlite
:class:`~awareness.alerts.store.AlertStore`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from awareness.alerts.engine import AlertEngine
from awareness.alerts.models import AlertRuleCreate
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


def _store(tmp_path: Path) -> AlertStore:
    return AlertStore(tmp_path / "alerts" / "alerts.db")


def _rule(store: AlertStore, *, kind: str = "term_count", **overrides: object) -> object:
    payload = {
        "name": "bitcoin watch",
        "kind": kind,
        "term": "bitcoin",
        "threshold": 3.0,
        "window_hours": 24.0,
        "cooldown_minutes": 30.0,
        "active": True,
        **overrides,
    }
    return store.create_rule(AlertRuleCreate(**payload))


# ── term_count ──────────────────────────────────────────────────────────────


def test_term_count_fires_when_threshold_met(tmp_path: Path) -> None:
    index = _index(tmp_path)
    store = _store(tmp_path)
    try:
        rule = _rule(store, threshold=3.0)
        base = datetime.now(UTC).replace(microsecond=0)
        for i in range(3):
            _write_doc(
                tmp_path / "jsonl", i, ts=base - timedelta(hours=2),
                title=f"Bitcoin news {i}", text="market update",
            )
        _write_doc(tmp_path / "jsonl", 99, ts=base, title="Sports", text="no crypto here")

        firings = AlertEngine(index, store).evaluate_rules()
        assert len(firings) == 1
        f = firings[0]
        assert f.rule_id == rule.id
        assert f.rule_name == rule.name
        assert f.kind == "term_count"
        assert f.term == "bitcoin"
        assert f.count == 3
        assert f.threshold == 3.0
        assert f.fired_at.tzinfo is not None
        assert "3 docs" in f.detail
        # firing is persisted
        rows = store.list_firings()
        assert len(rows) == 1
        assert rows[0]["id"] == f.id
    finally:
        index.close()
        store.close()


def test_term_count_does_not_fire_below_threshold(tmp_path: Path) -> None:
    index = _index(tmp_path)
    store = _store(tmp_path)
    try:
        _rule(store, threshold=3.0)
        base = datetime.now(UTC).replace(microsecond=0)
        _write_doc(tmp_path / "jsonl", 1, ts=base - timedelta(hours=2), title="Bitcoin dip")
        _write_doc(tmp_path / "jsonl", 2, ts=base - timedelta(hours=1), title="Bitcoin rally")

        assert AlertEngine(index, store).evaluate_rules() == []
        assert store.list_firings() == []
    finally:
        index.close()
        store.close()


def test_term_count_ignores_outside_window_and_inactive_rules(tmp_path: Path) -> None:
    index = _index(tmp_path)
    store = _store(tmp_path)
    try:
        _rule(store, threshold=1.0, active=False)
        _rule(store, threshold=1.0, active=True, term="sports")
        base = datetime.now(UTC).replace(microsecond=0)
        # bitcoin mentions: 1 inside window (title), 1 stale outside window,
        # 1 partial word that must not match (word-boundary regex).
        _write_doc(tmp_path / "jsonl", 1, ts=base - timedelta(hours=1), title="Bitcoin up")
        _write_doc(tmp_path / "jsonl", 2, ts=base - timedelta(days=3), title="Bitcoin old")
        _write_doc(tmp_path / "jsonl", 3, ts=base, title="Cryptobitcoin scam")

        firings = AlertEngine(index, store).evaluate_rules()
        assert firings == []  # bitcoin rule inactive, sports rule sees nothing
    finally:
        index.close()
        store.close()


def test_cooldown_suppresses_repeat_firings(tmp_path: Path) -> None:
    index = _index(tmp_path)
    store = _store(tmp_path)
    try:
        _rule(store, threshold=1.0, cooldown_minutes=60.0)
        base = datetime.now(UTC).replace(microsecond=0)
        _write_doc(tmp_path / "jsonl", 1, ts=base - timedelta(hours=1), title="Bitcoin now")

        engine = AlertEngine(index, store)
        first = engine.evaluate_rules()
        assert len(first) == 1
        # within the 60-minute cooldown: suppressed
        assert engine.evaluate_rules() == []
        assert len(store.list_firings()) == 1
    finally:
        index.close()
        store.close()


def test_check_rule_respects_cooldown_and_inactive(tmp_path: Path) -> None:
    index = _index(tmp_path)
    store = _store(tmp_path)
    try:
        rule = _rule(store, threshold=1.0, cooldown_minutes=60.0)
        _write_doc(tmp_path / "jsonl", 1, ts=datetime.now(UTC), title="Bitcoin hot")

        engine = AlertEngine(index, store)
        firing = engine.check_rule(rule.id)
        assert firing is not None
        assert engine.check_rule(rule.id) is None  # cooldown
        assert engine.check_rule("missing-rule") is None
    finally:
        index.close()
        store.close()


# ── term_spike ──────────────────────────────────────────────────────────────


def test_term_spike_fires_on_outlier_day(tmp_path: Path) -> None:
    index = _index(tmp_path)
    store = _store(tmp_path)
    try:
        _rule(store, kind="term_spike", threshold=3.0)
        base = datetime.now(UTC).replace(microsecond=0)
        # 7 baseline days, 1 mention each → baseline mean = 1.0
        for day in range(1, 8):
            ts = base - timedelta(hours=day * 24 + 3)
            _write_doc(tmp_path / "jsonl", day, ts=ts, text="bitcoin background")
        # outlier day: 4 mentions inside the current window
        for i in range(4):
            _write_doc(tmp_path / "jsonl", 100 + i, ts=base - timedelta(hours=2), text="bitcoin breakout")

        firings = AlertEngine(index, store).evaluate_rules()
        assert len(firings) == 1
        f = firings[0]
        assert f.kind == "term_spike"
        assert f.count == 4
        assert "baseline" in f.detail
    finally:
        index.close()
        store.close()


def test_term_spike_does_not_fire_without_outlier(tmp_path: Path) -> None:
    index = _index(tmp_path)
    store = _store(tmp_path)
    try:
        _rule(store, kind="term_spike", threshold=3.0)
        base = datetime.now(UTC).replace(microsecond=0)
        for day in range(1, 8):
            ts = base - timedelta(hours=day * 24 + 3)
            _write_doc(tmp_path / "jsonl", day, ts=ts, text="bitcoin background")
        # current window matches baseline volume (2 < threshold 3)
        for i in range(2):
            _write_doc(tmp_path / "jsonl", 100 + i, ts=base - timedelta(hours=2), text="bitcoin quiet")

        assert AlertEngine(index, store).evaluate_rules() == []
    finally:
        index.close()
        store.close()


def test_term_spike_zero_baseline_requires_absolute_floor(tmp_path: Path) -> None:
    index = _index(tmp_path)
    store = _store(tmp_path)
    try:
        _rule(store, kind="term_spike", threshold=1.0)
        base = datetime.now(UTC).replace(microsecond=0)
        # no older captures → baseline 0 → need count >= max(1, 3) = 3
        for i in range(2):
            _write_doc(tmp_path / "jsonl", 100 + i, ts=base - timedelta(hours=2), text="bitcoin fresh")
        assert AlertEngine(index, store).evaluate_rules() == []

        for i in range(3):
            _write_doc(tmp_path / "jsonl", 200 + i, ts=base - timedelta(hours=1), text="bitcoin more")
        firings = AlertEngine(index, store).evaluate_rules()
        assert len(firings) == 1
        assert firings[0].count == 5
    finally:
        index.close()
        store.close()


# ── readiness ───────────────────────────────────────────────────────────────


def test_index_not_ready_raises_runtime_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    index = _index(tmp_path)
    store = _store(tmp_path)
    try:
        monkeypatch.setattr(
            index, "health_snapshot", lambda: {"ready": False}, raising=False
        )
        engine = AlertEngine(index, store)
        with pytest.raises(RuntimeError, match="index not ready"):
            engine.evaluate_rules()
        with pytest.raises(RuntimeError, match="index not ready"):
            engine.check_rule("any")
    finally:
        index.close()
        store.close()
