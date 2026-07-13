"""IcebergWriter records append latency and row counters."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from awareness.obs import metrics as metrics_mod
from awareness.obs.metrics import MetricsRegistry, get_metrics
from awareness.storage.iceberg import IcebergWriter


@pytest.fixture(autouse=True)
def _fresh_metrics() -> None:
    metrics_mod._REGISTRY = MetricsRegistry()
    yield
    metrics_mod._REGISTRY = None


def _capture_row(**overrides: object) -> dict:
    now = datetime.now(UTC)
    row: dict = {
        "doc_id": "d1",
        "capture_id": "c1",
        "parent_doc_or_dup_group": None,
        "source_type": "tail_recrawl",
        "source_name": "tail",
        "source_locator": "https://example.com/1",
        "source_shard": "rss",
        "source_offset_or_record_id": None,
        "discovery_channel": "rss",
        "job_id": "j1",
        "batch_id": "b1",
        "ingest_version": "0.2.0",
        "url": "https://example.com/1",
        "canonical_url": "https://example.com/1",
        "domain": "example.com",
        "fetch_ts": now,
        "observed_ts": now,
        "published_ts": None,
        "last_modified": None,
        "content_type": "text/html",
        "http_status": 200,
        "etag": None,
        "title": "Hello",
        "text": "hello world " * 20,
        "language": "en",
        "content_hash": "abc123",
        "near_dup_hash": 0,
        "robots_decision": "allowed",
        "terms_note_if_relevant": None,
    }
    row.update(overrides)
    return row


def test_append_empty_is_noop_without_metrics(tmp_path: Path) -> None:
    w = IcebergWriter(catalog_db=tmp_path / "cat.sqlite", warehouse=tmp_path / "wh")
    assert w.append([]) == 0
    snap = get_metrics().snapshot()
    assert not any(c["name"].startswith("iceberg.") for c in snap["counters"])


def test_append_records_rows_and_latency(tmp_path: Path) -> None:
    w = IcebergWriter(catalog_db=tmp_path / "cat.sqlite", warehouse=tmp_path / "wh")
    w.ensure_table()
    n = w.append([_capture_row(), _capture_row(doc_id="d2", capture_id="c2")])
    assert n == 2

    m = get_metrics()
    assert m.counter_sum("iceberg.appended_rows") == 2.0
    assert m.counter_value("iceberg.append_batches", labels={"outcome": "ok"}) == 1.0
    assert m.counter_sum("iceberg.append_errors") == 0.0

    snap = m.snapshot()
    hists = [h for h in snap["histograms"] if h["name"] == "iceberg.append_seconds"]
    assert len(hists) == 1
    assert hists[0]["count"] == 1
    assert hists[0]["labels"].get("outcome") == "ok"
    assert hists[0]["sum"] >= 0.0


def test_append_error_records_error_metrics(tmp_path: Path) -> None:
    w = IcebergWriter(catalog_db=tmp_path / "cat.sqlite", warehouse=tmp_path / "wh")
    w.ensure_table()
    # Force failure after table is ready by replacing the table with a stub.
    boom = MagicMock()
    boom.append.side_effect = RuntimeError("parquet write failed")
    w._table = boom

    with pytest.raises(RuntimeError, match="parquet write failed"):
        w.append([_capture_row()])

    m = get_metrics()
    assert m.counter_sum("iceberg.append_errors") == 1.0
    assert m.counter_sum("iceberg.appended_rows") == 0.0
    snap = m.snapshot()
    hists = [h for h in snap["histograms"] if h["name"] == "iceberg.append_seconds"]
    assert len(hists) == 1
    assert hists[0]["labels"].get("outcome") == "error"
