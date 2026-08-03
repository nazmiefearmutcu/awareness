"""H-18 regression: language filters accept raw BCP-47 ("EN", "en-US")."""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from awareness.schemas.doc import SourceKind
from awareness.sources.base import AdapterContext, PartitionSpec
from awareness.sources.commoncrawl_wet import (
    _normalize_languages_filter,
    _parse_wet_to_captures,
)
from awareness.sources.fineweb import FineWebAdapter

# A short English WET record (enough text for LID + quality gate).
_EN_WET_RECORD = (
    "WARC/1.0\r\n"
    "WARC-Type: conversion\r\n"
    "WARC-Target-URI: https://example.com/en-page\r\n"
    "WARC-Date: 2026-06-19T20:00:00Z\r\n"
    "WARC-Record-ID: <urn:uuid:lang-test>\r\n"
    "Content-Length: 460\r\n"
    "\r\n"
    "This is an English language page used for testing that the language "
    "filter matches primary language tags case-insensitively. The quick brown "
    "fox jumps over the lazy dog near the river while the autumn leaves fall "
    "slowly to the ground and children laugh and play in the park all day "
    "long with their friends and family members enjoying the warm sunshine "
    "together in the afternoon before dinner time arrives once again.\r\n\r\n"
)


def _write_wet(path: Path) -> None:
    path.write_bytes(_EN_WET_RECORD.encode("utf-8"))


def test_normalize_languages_filter_handles_raw_bcp47() -> None:
    assert _normalize_languages_filter(["EN", "en-US", "fr_FR", "de"]) == {"en", "fr", "de"}
    assert _normalize_languages_filter(None) is None
    assert _normalize_languages_filter([]) is None


def test_wet_primary_subtag_filter_admits_matching_record(tmp_path: Path) -> None:
    wet = tmp_path / "shard.warc"
    _write_wet(wet)
    captures = _parse_wet_to_captures(
        path=wet,
        crawl_id="CC-MAIN-2026-06",
        shard_path="crawl-data/CC-MAIN-2026-06/segments/x/wet/shard.warc.wet.gz",
        domains_filter=None,
        languages_filter={"en"},  # primary subtag, as built from "EN"/"en-US"
        user_agent="test-agent",
        job_id="job",
        task_id="task",
        batch_id="batch",
        ingest_version="v1",
    )
    assert len(captures) == 1
    assert captures[0].language == "en"


def test_wet_primary_subtag_filter_rejects_other_language(tmp_path: Path) -> None:
    wet = tmp_path / "shard.warc"
    _write_wet(wet)
    captures = _parse_wet_to_captures(
        path=wet,
        crawl_id="CC-MAIN-2026-06",
        shard_path="crawl-data/CC-MAIN-2026-06/segments/x/wet/shard.warc.wet.gz",
        domains_filter=None,
        languages_filter={"fr"},
        user_agent="test-agent",
        job_id="job",
        task_id="task",
        batch_id="batch",
        ingest_version="v1",
    )
    assert captures == []


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


@pytest.mark.asyncio
async def test_fineweb_lang_filter_accepts_uppercase_and_region_tags(monkeypatch) -> None:
    """--lang EN / en-US matches rows tagged 'en'; fr is filtered."""
    rows = [
        {"text": "a" * 400, "url": "https://e.example/1", "language": "en"},
        {"text": "b" * 400, "url": "https://e.example/2", "language": "fr"},
    ]
    mod = types.ModuleType("datasets")
    mod.load_dataset = lambda *a, **k: iter(rows)  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "datasets", mod)
    monkeypatch.setattr(
        "awareness.sources.fineweb.get_settings",
        lambda: types.SimpleNamespace(text_min_chars=10, text_max_chars=1_500_000),
    )

    adapter = FineWebAdapter()
    part = PartitionSpec(
        source_type=SourceKind.FINEWEB,
        partition_key="HuggingFaceFW/fineweb:CC-MAIN-2024-26",
        payload={
            "dataset": "HuggingFaceFW/fineweb",
            "dump": "CC-MAIN-2024-26",
            "rows_per_partition": 10,
            "languages": ["EN", "en-US"],
            "domains": [],
        },
    )
    caps = [c async for c in adapter.run_partition(part, _ctx())]
    assert len(caps) == 1
    assert caps[0].url == "https://e.example/1"
    assert caps[0].language == "en"
