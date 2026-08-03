"""TUI analytics panel tests: ``_sparkline`` helper + analytics layout rendering.

Mirrors how :mod:`test_tui_controls_and_cancellation` drives the TUI: the
layout builders are called directly with a tiny real DuckDbIndex over a
JSONL corpus (same chunk pattern as ``test_cli_trends``), and the rendered
output is asserted on.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock

from rich.console import Console

from awareness.cli.main import (
    _SPARK_MAX_WIDTH,
    _SPARK_MIN_WIDTH,
    _TUI_ANALYTICS_KEY,
    _make_tui_analytics_layout,
    _make_tui_layout,
    _sparkline,
    tui,
)
from awareness.config import get_settings
from awareness.planner.planner import Planner
from awareness.storage.duckdb_index import DuckDbIndex
from awareness.storage.state import StateDB

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


def _corpus(tmp_project: Path) -> None:
    """Tiny corpus: 'bitcoin' dominant; 'market'/'rally' also above min_count."""
    root = tmp_project / "data" / "jsonl"
    now = datetime.now(UTC)
    _write_doc(
        root, 1, ts=now - timedelta(hours=2),
        title="Bitcoin hits record", text="market rally bitcoin surge",
        domain="coinnews.example",
    )
    _write_doc(
        root, 2, ts=now - timedelta(hours=1),
        title="bitcoin crash watch", text="bitcoin dip",
        domain="coinnews.example",
    )
    _write_doc(
        root, 3, ts=now - timedelta(hours=3),
        title="Sports roundup", text="market rally sports",
        domain="sports.example",
    )
    _write_doc(
        root, 4, ts=now - timedelta(hours=2),
        title="Bitcoin ETF update", text="bitcoin market",
        domain="etfnews.example",
    )


def _real_index(tmp_project: Path) -> DuckDbIndex:
    settings = get_settings()
    return DuckDbIndex(
        db_path=settings.duckdb_path(),
        jsonl_dir=settings.staging_jsonl_dir(),
        iceberg_warehouse=settings.iceberg_warehouse,
    )


def _render(layout: object) -> str:
    console = Console(
        width=140, height=120, file=StringIO(), force_terminal=False, color_system=None
    )
    console.print(layout)
    return console.file.getvalue()


# ── _sparkline ──────────────────────────────────────────────────────────────


def test_sparkline_empty_list() -> None:
    assert _sparkline([]) == ""


def test_sparkline_flat_series_is_all_same_block() -> None:
    out = _sparkline([5, 5, 5, 5])
    assert out  # flat series must not divide by zero
    assert len(set(out)) == 1
    assert out == "▁" * len(out)


def test_sparkline_increasing_series_scales_to_blocks() -> None:
    out = _sparkline(list(range(8)))
    assert len(out) == 40  # default width
    assert out[0] == "▁"  # min scaled to floor block
    assert out[-1] == "█"  # max scaled to top block
    assert out.count("█") >= 1


def test_sparkline_width_is_clamped() -> None:
    assert len(_sparkline([1, 2, 3], width=5)) == _SPARK_MIN_WIDTH
    assert len(_sparkline([1, 2, 3], width=200)) == _SPARK_MAX_WIDTH


# ── TUI analytics panel rendering ───────────────────────────────────────────


def test_analytics_layout_renders_top_terms_and_domains(tmp_project) -> None:
    _corpus(tmp_project)
    settings = get_settings()
    idx = _real_index(tmp_project)
    try:
        out = _render(_make_tui_analytics_layout(settings, idx=idx))
    finally:
        idx.close()
    assert "Top 10 Terms" in out
    assert "Top 8 Domains" in out
    assert "bitcoin" in out
    assert "market" in out
    assert "coinnews.example" in out
    assert "sports.example" in out


def test_analytics_layout_term_view(tmp_project) -> None:
    _corpus(tmp_project)
    settings = get_settings()
    idx = _real_index(tmp_project)
    try:
        out = _render(_make_tui_analytics_layout(settings, idx=idx, term="bitcoin"))
    finally:
        idx.close()
    assert "Term: 'bitcoin'" in out
    assert "Sparkline" in out
    assert "█" in out  # busy day reaches the top block
    assert "Date" in out
    assert "Z" in out  # z-score column
    assert "Sentiment" in out  # sentiment avg-score column (engine importable)
    assert "!" in out  # spike day mark (single 3-doc day z > 2.5)


def test_analytics_layout_graceful_on_broken_index(tmp_path) -> None:
    mock_settings = MagicMock()
    mock_settings.data_dir = tmp_path
    out = _render(_make_tui_analytics_layout(mock_settings, idx=MagicMock()))
    assert "Analytics unavailable" in out


# ── Keybinding / integration surface ────────────────────────────────────────


def test_tui_analytics_key_registered() -> None:
    assert _TUI_ANALYTICS_KEY == "y"
    source = inspect.getsource(tui)
    assert "key_lower == _TUI_ANALYTICS_KEY" in source  # jump key wired
    assert 'current_view = "analytics"' in source  # panel switch wired
    assert 'current_view == "analytics"' in source  # refresh branch wired
    assert "esc" in source  # Esc clears the term input mode


def test_tui_help_line_documents_analytics(tmp_path) -> None:
    db = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    db.init()
    _ = Planner(db)

    mock_settings = MagicMock()
    mock_settings.data_dir = tmp_path
    mock_settings.staging_jsonl_dir.return_value = tmp_path / "staging"
    mock_settings.duckdb_path.return_value = tmp_path / "metadata.duckdb"
    mock_settings.iceberg_warehouse = "s3://warehouse"
    mock_settings.iceberg_catalog_db = "db"

    mock_idx = MagicMock()
    mock_idx.execute.return_value = [
        {"fetch_ts": datetime.now(), "title": "Test Capture 1", "domain": "example.com"},
    ]

    out = _render(_make_tui_layout(db, mock_settings, mock_idx, selected_job_idx=0))
    assert "[Y] Analytics" in out
