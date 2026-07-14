"""compact path records iceberg.compact_* process-local metrics."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from awareness.cli.main import app
from awareness.obs import metrics as metrics_mod
from awareness.obs.metrics import MetricsRegistry, get_metrics
from awareness.storage.state import StateDB

runner = CliRunner()


@pytest.fixture(autouse=True)
def _fresh_metrics() -> None:
    metrics_mod._REGISTRY = MetricsRegistry()
    yield
    metrics_mod._REGISTRY = None


def _state(tmp_path: Path) -> StateDB:
    db = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    db.init()
    return db


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def test_compact_ok_records_metrics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state(tmp_path)
    chunk = tmp_path / "staging" / "a.jsonl"
    _write_jsonl(
        chunk,
        [
            {"doc_id": "d1", "text": "hello"},
            {"doc_id": "d2", "text": "world"},
        ],
    )
    state.add_manifest(str(chunk), records=2, bytes_=chunk.stat().st_size)

    mock_writer = MagicMock()
    mock_writer.append.return_value = 2

    class _W:
        def __init__(self, *a, **k):
            pass

        def ensure_table(self) -> None:
            return None

        def append(self, rows):
            return mock_writer.append(rows)

    monkeypatch.setattr("awareness.storage.iceberg.IcebergWriter", _W)

    from awareness.config import settings as settings_mod

    def _settings():
        s = settings_mod.Settings()
        s.state_db_url = f"sqlite:///{tmp_path / 'state.db'}"
        s.enable_iceberg = True
        s.iceberg_catalog_db = tmp_path / "cat.sqlite"
        s.iceberg_warehouse = tmp_path / "wh"
        return s

    monkeypatch.setattr("awareness.cli.main.get_settings", _settings)
    monkeypatch.setattr("awareness.cli.main._bootstrap", lambda: (state, None))

    result = runner.invoke(app, ["compact", "--force"])
    assert result.exit_code == 0, result.output
    assert mock_writer.append.called

    m = get_metrics()
    assert m.counter_value("iceberg.compact_manifests", labels={"outcome": "ok"}) == 1.0
    assert m.counter_sum("iceberg.compacted_rows") == 2.0
    assert m.counter_sum("iceberg.compact_errors") == 0.0
    snap = m.snapshot()
    hists = [h for h in snap["histograms"] if h["name"] == "iceberg.compact_seconds"]
    assert len(hists) == 1
    assert hists[0]["labels"].get("outcome") == "ok"
    assert hists[0]["count"] == 1

    # Manifest marked compacted.
    assert state.pending_manifest_summary()["pending_count"] == 0


def test_compact_missing_file_records_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    state.add_manifest(str(tmp_path / "no-such.jsonl"), records=1, bytes_=10)

    class _W:
        def __init__(self, *a, **k):
            pass

        def ensure_table(self) -> None:
            return None

        def append(self, rows):
            raise AssertionError("append should not run for missing file")

    monkeypatch.setattr("awareness.storage.iceberg.IcebergWriter", _W)

    from awareness.config import settings as settings_mod

    def _settings():
        s = settings_mod.Settings()
        s.enable_iceberg = True
        s.iceberg_catalog_db = tmp_path / "cat.sqlite"
        s.iceberg_warehouse = tmp_path / "wh"
        return s

    monkeypatch.setattr("awareness.cli.main.get_settings", _settings)
    monkeypatch.setattr("awareness.cli.main._bootstrap", lambda: (state, None))

    result = runner.invoke(app, ["compact", "--force"])
    assert result.exit_code == 0, result.output

    m = get_metrics()
    assert (
        m.counter_value("iceberg.compact_manifests", labels={"outcome": "missing"})
        == 1.0
    )
    assert state.pending_manifest_summary()["pending_count"] == 0


def test_compact_append_error_records_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    chunk = tmp_path / "staging" / "b.jsonl"
    _write_jsonl(chunk, [{"doc_id": "d1"}])
    state.add_manifest(str(chunk), records=1, bytes_=chunk.stat().st_size)

    class _W:
        def __init__(self, *a, **k):
            pass

        def ensure_table(self) -> None:
            return None

        def append(self, rows):
            raise RuntimeError("parquet failed")

    monkeypatch.setattr("awareness.storage.iceberg.IcebergWriter", _W)

    from awareness.config import settings as settings_mod

    def _settings():
        s = settings_mod.Settings()
        s.enable_iceberg = True
        s.iceberg_catalog_db = tmp_path / "cat.sqlite"
        s.iceberg_warehouse = tmp_path / "wh"
        return s

    monkeypatch.setattr("awareness.cli.main.get_settings", _settings)
    monkeypatch.setattr("awareness.cli.main._bootstrap", lambda: (state, None))

    result = runner.invoke(app, ["compact", "--force"])
    assert result.exit_code == 0, result.output

    m = get_metrics()
    assert (
        m.counter_value(
            "iceberg.compact_manifests", labels={"outcome": "append_error"}
        )
        == 1.0
    )
    assert m.counter_value("iceberg.compact_errors", labels={"stage": "append"}) == 1.0
    # Still pending — not marked compacted on append failure.
    assert state.pending_manifest_summary()["pending_count"] == 1
