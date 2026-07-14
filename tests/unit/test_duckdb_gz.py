from __future__ import annotations

import gzip
import json
from pathlib import Path

from awareness.storage.duckdb_index import DuckDbIndex, _is_staging_jsonl


def _row(**overrides: object) -> dict:
    base = {
        "doc_id": "d1",
        "capture_id": "c1",
        "url": "http://example.test/a",
        "canonical_url": "http://example.test/a",
        "domain": "example.test",
        "title": "Bitcoin rally",
        "text": "bitcoin surged today",
        "language": "en",
        "fetch_ts": "2026-06-08T00:00:00+00:00",
        "parent_doc_or_dup_group": None,
        "source_type": None,
        "source_name": None,
        "source_locator": None,
        "source_shard": None,
        "source_offset_or_record_id": None,
        "discovery_channel": None,
        "job_id": None,
        "batch_id": None,
        "ingest_version": None,
        "observed_ts": None,
        "published_ts": None,
        "last_modified": None,
        "content_type": None,
        "http_status": None,
        "etag": None,
        "content_hash": None,
        "near_dup_hash": None,
        "robots_decision": None,
        "terms_note_if_relevant": None,
    }
    base.update(overrides)
    return base


def _write_gz_chunk(jsonl_dir: Path, row: dict, name: str = "chunk-0001.jsonl.gz") -> Path:
    day = jsonl_dir / "captures" / "2026" / "06" / "08"
    day.mkdir(parents=True, exist_ok=True)
    path = day / name
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    return path


def _write_plain_chunk(jsonl_dir: Path, row: dict, name: str = "chunk-0001.jsonl") -> Path:
    day = jsonl_dir / "captures" / "2026" / "06" / "08"
    day.mkdir(parents=True, exist_ok=True)
    path = day / name
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    return path


def test_is_staging_jsonl_accepts_final_only() -> None:
    assert _is_staging_jsonl(Path("captures-1.jsonl")) is True
    assert _is_staging_jsonl(Path("captures-1.jsonl.gz")) is True
    assert _is_staging_jsonl(Path("captures-1.jsonl.tmp")) is False
    assert _is_staging_jsonl(Path("captures-1.jsonl.gz.tmp")) is False
    assert _is_staging_jsonl(Path("captures-1.jsonl.bak")) is False


def test_gz_chunks_are_indexed(tmp_path: Path) -> None:
    jsonl_dir = tmp_path / "jsonl"
    _write_gz_chunk(jsonl_dir, _row())
    idx = DuckDbIndex(db_path=tmp_path / "idx.duckdb", jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    rows = idx.execute("SELECT count(*) AS n FROM captures")
    assert rows[0]["n"] == 1
    res = idx.search("bitcoin", limit=10)
    assert res["total"] >= 1
    idx.close()


def test_tmp_staging_chunks_are_not_indexed(tmp_path: Path) -> None:
    """In-flight writer temps (*.jsonl.tmp / *.jsonl.gz.tmp) must not be read."""
    jsonl_dir = tmp_path / "jsonl"
    day = jsonl_dir / "captures" / "2026" / "06" / "08"
    day.mkdir(parents=True, exist_ok=True)

    # Only temps present: index should see an empty corpus.
    tmp_plain = day / "captures-open.jsonl.tmp"
    tmp_plain.write_text(json.dumps(_row(capture_id="tmp-plain", title="tmp plain")) + "\n")
    with gzip.open(day / "captures-open.jsonl.gz.tmp", "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(_row(capture_id="tmp-gz", title="tmp gz", text="tmp only")) + "\n")

    idx = DuckDbIndex(db_path=tmp_path / "idx-tmp.duckdb", jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    assert idx._get_partition_globs() == []
    rows = idx.execute("SELECT count(*) AS n FROM captures")
    assert rows[0]["n"] == 0
    idx.close()

    # Finalized plain + gz still indexed; temps co-located must not inflate count.
    _write_plain_chunk(jsonl_dir, _row(capture_id="final-plain", title="final plain", text="alpha token"), "final.jsonl")
    _write_gz_chunk(jsonl_dir, _row(capture_id="final-gz", title="final gz", text="beta token"), "final.jsonl.gz")

    idx2 = DuckDbIndex(db_path=tmp_path / "idx-mixed.duckdb", jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    globs = idx2._get_partition_globs()
    assert any(g.endswith("*.jsonl") for g in globs)
    assert any(g.endswith("*.jsonl.gz") for g in globs)
    assert not any(".tmp" in g for g in globs)
    n = idx2.execute("SELECT count(*) AS n FROM captures")[0]["n"]
    assert n == 2
    ids = {r["capture_id"] for r in idx2.execute("SELECT capture_id FROM captures")}
    assert ids == {"final-plain", "final-gz"}
    idx2.close()
