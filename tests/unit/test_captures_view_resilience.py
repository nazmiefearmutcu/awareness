from __future__ import annotations

import json

from awareness.storage.duckdb_index import DuckDbIndex


def test_missing_column_does_not_break_captures(tmp_path) -> None:
    jsonl_dir = tmp_path / "jsonl"
    day = jsonl_dir / "captures" / "2026" / "06" / "08"
    day.mkdir(parents=True)
    row = {
        "doc_id": "d1", "capture_id": "c1",
        "url": "http://e.test/a", "canonical_url": "http://e.test/a",
        "domain": "e.test", "title": "Bitcoin", "text": "bitcoin is here",
        "language": "en", "fetch_ts": "2026-06-08T00:00:00+00:00",
    }
    (day / "c.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    idx = DuckDbIndex(db_path=tmp_path / "i.duckdb", jsonl_dir=jsonl_dir, iceberg_warehouse=None)
    rows = idx.execute("SELECT count(*) AS n FROM captures")
    assert rows[0]["n"] == 1
    res = idx.search("bitcoin", limit=10)
    assert res["total"] >= 1
    got = idx.execute("SELECT parent_doc_or_dup_group FROM captures LIMIT 1")
    assert got[0]["parent_doc_or_dup_group"] is None
    idx.close()
