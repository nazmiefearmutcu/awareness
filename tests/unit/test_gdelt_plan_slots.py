"""M-14 regression: GDELT plan must not truncate real backfills to 8 slots."""

from __future__ import annotations

from datetime import UTC, datetime

from awareness.schemas.doc import SourceKind
from awareness.schemas.jobs import BackfillRequest
from awareness.sources.gdelt import GdeltAdapter, _quarter_hours


def test_plan_all_slots_for_backfill(monkeypatch) -> None:
    """A 3-hour backfill (12 slots) must NOT be silently capped to 8."""
    settings = type("S", (), {"tail_gdelt_max_urls": 250})()
    monkeypatch.setattr(
        "awareness.sources.gdelt.get_settings", lambda: settings
    )
    req = BackfillRequest(
        start=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end=datetime(2024, 1, 1, 2, 45, tzinfo=UTC),
        sources=[SourceKind.GDELT],
    )
    slots = _quarter_hours(req.start, req.end)
    assert len(slots) == 12
    parts = GdeltAdapter().plan(req)
    assert len(parts) == 12
    assert all(p.payload["max_urls"] == 250 for p in parts)


def test_plan_explicit_max_tasks_caps_smoke_run(monkeypatch) -> None:
    settings = type("S", (), {"tail_gdelt_max_urls": 500})()
    monkeypatch.setattr(
        "awareness.sources.gdelt.get_settings", lambda: settings
    )
    req = BackfillRequest(
        start=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end=datetime(2024, 1, 1, 2, 45, tzinfo=UTC),
        sources=[SourceKind.GDELT],
        max_tasks=3,  # EXPLICIT smoke cap
    )
    parts = GdeltAdapter().plan(req)
    assert len(parts) == 3


def test_plan_payload_caps_subpartitions_via_tail_gdelt_max_urls(monkeypatch) -> None:
    """Backfill payload carries max_urls (mirrors tail_gdelt_max_urls)."""
    settings = type("S", (), {"tail_gdelt_max_urls": 0})()
    monkeypatch.setattr(
        "awareness.sources.gdelt.get_settings", lambda: settings
    )
    req = BackfillRequest(
        start=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end=datetime(2024, 1, 1, 0, 15, tzinfo=UTC),
        sources=[SourceKind.GDELT],
    )
    parts = GdeltAdapter().plan(req)
    assert len(parts) == 2
    # max_urls 0 → no per-slot cap key in the payload (run_partition treats
    # missing/0 as unlimited).
    assert all("max_urls" not in p.payload for p in parts)
