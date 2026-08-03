"""Tests for the LLM-ready dataset export (llm_export.py)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from awareness.consume.llm_export import (
    HARD_MAX_LIMIT,
    export_llm_dataset,
    sample_corpus,
)
from awareness.storage.duckdb_index import DuckDbIndex

_FULL_KEYS = (
    "doc_id",
    "capture_id",
    "parent_doc_or_dup_group",
    "source_type",
    "source_name",
    "source_locator",
    "source_shard",
    "source_offset_or_record_id",
    "discovery_channel",
    "job_id",
    "batch_id",
    "ingest_version",
    "url",
    "canonical_url",
    "domain",
    "fetch_ts",
    "observed_ts",
    "published_ts",
    "last_modified",
    "content_type",
    "http_status",
    "etag",
    "title",
    "text",
    "language",
    "content_hash",
    "near_dup_hash",
    "robots_decision",
    "terms_note_if_relevant",
)


def _write(
    root: Path,
    *,
    capture_id: str,
    parent: str | None,
    observed_ts: str,
    domain: str = "example.com",
    title: str | None = None,
    text: str | None = None,
) -> None:
    ts = datetime.fromisoformat(observed_ts)
    day_dir = root / "captures" / ts.strftime("%Y/%m/%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{capture_id}",
        capture_id=capture_id,
        parent_doc_or_dup_group=parent,
        source_type="rss",
        domain=domain,
        url=f"https://{domain}/{capture_id}",
        canonical_url=f"https://{domain}/{capture_id}",
        fetch_ts=observed_ts,
        observed_ts=observed_ts,
        title=title or f"Title {capture_id}",
        text=text or f"Body {capture_id}",
        content_hash=f"h-{capture_id}",
        language="en",
    )
    (day_dir / f"{capture_id}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _index(tmp_path: Path) -> DuckDbIndex:
    jsonl = tmp_path / "jsonl"
    # Group g1 has two members (c1 earliest, c2 later); g2 likewise (c3 then
    # c5). c4 is a singleton with no parent group.
    _write(jsonl, capture_id="c1", parent="g1", observed_ts="2026-06-01T10:00:00+00:00")
    _write(jsonl, capture_id="c2", parent="g1", observed_ts="2026-06-01T12:00:00+00:00")
    _write(jsonl, capture_id="c3", parent="g2", observed_ts="2026-06-01T11:00:00+00:00")
    _write(jsonl, capture_id="c4", parent=None, observed_ts="2026-06-01T09:00:00+00:00")
    _write(jsonl, capture_id="c5", parent="g2", observed_ts="2026-06-01T13:00:00+00:00")
    return DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=jsonl,
        iceberg_warehouse=None,
    )


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_export_jsonl_writes_llm_shape(tmp_path: Path) -> None:
    index = _index(tmp_path)
    result = export_llm_dataset(index, tmp_path / "exports")

    assert result.count == 3  # dedupe folds g1 and g2 to their earliest member
    assert result.format == "jsonl"
    assert result.dedupe is True
    assert len(result.files) == 1
    assert result.path == result.files[0]
    path = Path(result.path)
    assert path.exists()
    assert path.suffix == ".jsonl"
    assert not list(tmp_path.glob("exports/*.tmp")), "no temp files left behind"

    rows = _read_jsonl(path)
    assert [r["metadata"]["capture_id"] for r in rows] == ["c4", "c1", "c3"]
    for row in rows:
        assert set(row) == {"instruction", "input", "output", "metadata"}
        assert row["instruction"] is None
        assert row["input"] is None
        assert set(row["metadata"]) == {"capture_id", "domain", "url", "observed_ts", "language"}
        cid = row["metadata"]["capture_id"]
        assert row["output"] == f"Title {cid}\n\nBody {cid}"
        assert row["metadata"]["domain"] == "example.com"
        assert row["metadata"]["language"] == "en"
        assert row["metadata"]["observed_ts"].endswith("+00:00")


def test_export_no_dedupe_keeps_all_rows(tmp_path: Path) -> None:
    index = _index(tmp_path)
    result = export_llm_dataset(index, tmp_path / "exports", dedupe=False)
    assert result.count == 5
    rows = _read_jsonl(Path(result.path))
    assert len(rows) == 5


def test_export_limit_is_respected_and_clamped(tmp_path: Path) -> None:
    index = _index(tmp_path)
    result = export_llm_dataset(index, tmp_path / "exports", limit=2)
    assert result.count == 2
    rows = _read_jsonl(Path(result.path))
    assert [r["metadata"]["capture_id"] for r in rows] == ["c4", "c1"]

    # Hard max clamp: 999999 → HARD_MAX_LIMIT (corpus only has 5 rows).
    result = export_llm_dataset(index, tmp_path / "exports", limit=999_999, dedupe=False)
    assert result.limit == HARD_MAX_LIMIT
    assert result.count == 5


def test_export_window_and_domain_filters(tmp_path: Path) -> None:
    index = _index(tmp_path)
    result = export_llm_dataset(
        index,
        tmp_path / "exports",
        start="2026-06-01T10:30:00+00:00",
        end="2026-06-01T12:30:00+00:00",
        dedupe=False,
    )
    assert result.count == 2
    rows = _read_jsonl(Path(result.path))
    assert [r["metadata"]["capture_id"] for r in rows] == ["c3", "c2"]

    result = export_llm_dataset(index, tmp_path / "exports", domains=["Example.COM"], dedupe=False)
    assert result.count == 5  # all rows are example.com; case-insensitive match


def test_export_parquet_roundtrip(tmp_path: Path) -> None:
    index = _index(tmp_path)
    result = export_llm_dataset(index, tmp_path / "exports", format="parquet")
    assert result.count == 3
    path = Path(result.path)
    assert path.suffix == ".parquet"
    assert not list(tmp_path.glob("exports/*.tmp"))

    table = pq.read_table(path)
    assert table.num_rows == 3
    assert set(table.column_names) == {"instruction", "input", "output", "metadata"}
    meta = table.column("metadata")
    assert meta.to_pylist()[0]["capture_id"] == "c4"


def test_export_empty_corpus_is_valid(tmp_path: Path) -> None:
    index = DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )
    result = export_llm_dataset(index, tmp_path / "exports")
    assert result.count == 0
    assert Path(result.path).exists()
    assert _read_jsonl(Path(result.path)) == []


def test_export_bad_format_raises(tmp_path: Path) -> None:
    index = _index(tmp_path)
    with pytest.raises(ValueError, match="unsupported export format"):
        export_llm_dataset(index, tmp_path / "exports", format="csv")


def test_sample_corpus_bounded_and_shaped(tmp_path: Path) -> None:
    index = _index(tmp_path)
    sample = sample_corpus(index, n=3)
    assert len(sample) <= 3
    assert {k: type(v) for k, v in sample[0].items()}["capture_id"] is str
    assert "observed_ts" in sample[0]

    # n is clamped: 0 → 1, huge → 1000 (corpus only has 5 rows).
    assert len(sample_corpus(index, n=0)) == 1
    assert len(sample_corpus(index, n=100_000)) == 5

    # Window filter applies.
    filtered = sample_corpus(index, n=10, start="2026-06-01T12:00:00+00:00")
    assert all(r["observed_ts"] >= "2026-06-01T12:00:00" for r in filtered)
