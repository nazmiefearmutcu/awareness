"""FineWeb stream load / admit / filter process-local metrics."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from awareness.obs.metrics import MetricsRegistry
from awareness.schemas.doc import SourceKind
from awareness.sources.base import AdapterContext, PartitionSpec
from awareness.sources.fineweb import (
    FineWebAdapter,
    _fineweb_dataset_label,
    _normalize_filter_reason,
    _record_fineweb_load,
)
from awareness.util.http import RetryableHTTPError


def test_fineweb_dataset_label() -> None:
    assert _fineweb_dataset_label("HuggingFaceFW/fineweb") == "fineweb"
    assert _fineweb_dataset_label("HuggingFaceFW/fineweb-2") == "fineweb_2"
    assert _fineweb_dataset_label("other") == "fineweb"


def test_normalize_filter_reason() -> None:
    assert _normalize_filter_reason("empty") == "empty"
    assert _normalize_filter_reason("too_short<200") == "too_short"
    assert _normalize_filter_reason("weird") == "normalize"
    assert _normalize_filter_reason(None) == "normalize"


@pytest.fixture()
def metrics(monkeypatch: pytest.MonkeyPatch) -> MetricsRegistry:
    reg = MetricsRegistry()
    monkeypatch.setattr("awareness.sources.fineweb.get_metrics", lambda: reg)
    return reg


def test_record_fineweb_load_ok(metrics: MetricsRegistry) -> None:
    _record_fineweb_load(outcome="ok", elapsed=0.12, dataset="fineweb")
    assert metrics.counter_value(
        "fineweb.load_attempts", labels={"outcome": "ok", "dataset": "fineweb"}
    ) == 1.0
    snap = metrics.snapshot()
    hists = [h for h in snap["histograms"] if h["name"] == "fineweb.load_seconds"]
    assert hists and hists[0]["count"] >= 1


def _ctx() -> AdapterContext:
    return AdapterContext(
        user_agent="test-agent",
        job_id="job-fw",
        task_id="task-fw",
        batch_id="batch-fw",
        ingest_version="test",
        checkpoint={},
        is_stopping=lambda: False,
        extras={},
    )


def _part(**payload: Any) -> PartitionSpec:
    base = {
        "dataset": "HuggingFaceFW/fineweb",
        "dump": "sample-10BT",
        "rows_per_partition": 10,
        "languages": [],
        "domains": [],
    }
    base.update(payload)
    return PartitionSpec(
        source_type=SourceKind.FINEWEB,
        partition_key=f"{base['dataset']}:{base['dump']}",
        payload=base,
    )


@pytest.mark.asyncio
async def test_run_partition_missing_datasets_lib(
    metrics: MetricsRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "datasets":
            raise ImportError("no datasets")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    adapter = FineWebAdapter()
    async for _ in adapter.run_partition(_part(), _ctx()):
        pass
    assert metrics.counter_value(
        "fineweb.load_attempts",
        labels={"outcome": "missing_dep", "dataset": "fineweb"},
    ) == 1.0
    assert metrics.counter_sum("fineweb.rows_admitted") == 0.0


@pytest.mark.asyncio
async def test_run_partition_load_error(
    metrics: MetricsRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_ds = MagicMock()
    fake_ds.load_dataset = MagicMock(side_effect=RuntimeError("hf down"))
    monkeypatch.setitem(__import__("sys").modules, "datasets", fake_ds)
    # Ensure import of load_dataset works via the module attribute.
    monkeypatch.setattr(
        "awareness.sources.fineweb.load_dataset",
        fake_ds.load_dataset,
        raising=False,
    )

    # Patch the import inside run_partition by injecting a module with load_dataset.
    import types

    mod = types.ModuleType("datasets")
    mod.load_dataset = MagicMock(side_effect=RuntimeError("hf down"))  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "datasets", mod)

    adapter = FineWebAdapter()
    # H-15: a load failure must NOT mark the partition COMPLETED — it records
    # the metric, then raises RetryableHTTPError so the task retries.
    with pytest.raises(RetryableHTTPError):
        async for _ in adapter.run_partition(_part(), _ctx()):
            pass
    assert metrics.counter_value(
        "fineweb.load_attempts",
        labels={"outcome": "error", "dataset": "fineweb"},
    ) == 1.0


@pytest.mark.asyncio
async def test_run_partition_stream_metrics(
    metrics: MetricsRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        {"text": "", "url": "https://ex.example.com/a"},  # empty
        {"text": "x" * 50, "url": "https://ex.example.com/b", "language": "fr"},  # lang
        {
            "text": "y" * 400,
            "url": "https://other.example.org/c",
            "language": "en",
        },  # domain filter
        {
            "text": "z" * 400,
            "url": "https://keep.example.com/d",
            "language": "en",
        },  # admitted
        {
            "text": "w" * 10,
            "url": "https://keep.example.com/e",
            "language": "en",
        },  # too short normalize
    ]

    import types

    mod = types.ModuleType("datasets")

    def _load_dataset(*_a, **_k):
        return iter(rows)

    mod.load_dataset = _load_dataset  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "datasets", mod)

    # Lower min chars so "z"*400 admits and "w"*10 filters as too_short.
    monkeypatch.setattr(
        "awareness.sources.fineweb.get_settings",
        lambda: types.SimpleNamespace(text_min_chars=100, text_max_chars=1_500_000),
    )

    adapter = FineWebAdapter()
    ctx = _ctx()
    part = _part(
        rows_per_partition=5,
        languages=["en"],
        domains=["example.com"],
    )
    out: list[Any] = []
    async for cap in adapter.run_partition(part, ctx):
        out.append(cap)

    assert len(out) == 1
    assert metrics.counter_value(
        "fineweb.load_attempts", labels={"outcome": "ok", "dataset": "fineweb"}
    ) == 1.0
    assert metrics.counter_sum("fineweb.rows_seen") == 5.0
    assert metrics.counter_sum("fineweb.rows_admitted") == 1.0
    assert metrics.counter_value(
        "fineweb.rows_filtered", labels={"reason": "empty", "dataset": "fineweb"}
    ) == 1.0
    assert metrics.counter_value(
        "fineweb.rows_filtered", labels={"reason": "language", "dataset": "fineweb"}
    ) == 1.0
    assert metrics.counter_value(
        "fineweb.rows_filtered", labels={"reason": "domain", "dataset": "fineweb"}
    ) == 1.0
    assert metrics.counter_value(
        "fineweb.rows_filtered", labels={"reason": "too_short", "dataset": "fineweb"}
    ) == 1.0
    snap = metrics.snapshot()
    load_h = [h for h in snap["histograms"] if h["name"] == "fineweb.load_seconds"]
    part_h = [h for h in snap["histograms"] if h["name"] == "fineweb.partition_seconds"]
    assert load_h and load_h[0]["count"] >= 1
    assert part_h and part_h[0]["count"] >= 1
