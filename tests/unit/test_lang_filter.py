"""BCP-47 language filter helpers and primary-subtag matching in search."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from awareness.storage.duckdb_index import DuckDbIndex
from awareness.util.lang import (
    append_language_filter,
    language_sql_filter,
    normalize_language_tag,
)

_FULL_KEYS = (
    "doc_id", "capture_id", "parent_doc_or_dup_group", "source_type",
    "source_name", "source_locator", "source_shard",
    "source_offset_or_record_id", "discovery_channel", "job_id", "batch_id",
    "ingest_version", "url", "canonical_url", "domain", "fetch_ts",
    "observed_ts", "published_ts", "last_modified", "content_type",
    "http_status", "etag", "title", "text", "language", "content_hash",
    "near_dup_hash", "robots_decision", "terms_note_if_relevant",
)


def test_normalize_language_tag() -> None:
    assert normalize_language_tag(None) is None
    assert normalize_language_tag("") is None
    assert normalize_language_tag("  ") is None
    assert normalize_language_tag("EN") == "en"
    assert normalize_language_tag("en-US") == "en-us"
    assert normalize_language_tag("en_US") == "en-us"
    assert normalize_language_tag("EN_gb") == "en-gb"


def test_language_sql_filter_primary_matches_subtags() -> None:
    clause, params = language_sql_filter("EN")
    assert clause is not None
    assert "LIKE $langpfx" in clause
    assert params["lang"] == "en"
    assert params["langpfx"] == "en-%"


def test_language_sql_filter_full_tag_exact() -> None:
    clause, params = language_sql_filter("en_US")
    assert clause is not None
    assert "LIKE" not in clause
    assert params == {"lang": "en-us"}


def test_language_sql_filter_empty() -> None:
    clause, params = language_sql_filter(None)
    assert clause is None
    assert params == {}


def test_append_language_filter() -> None:
    where: list[str] = []
    params: dict = {}
    append_language_filter(where, params, "tr")
    assert len(where) == 1
    assert params["lang"] == "tr"
    assert params["langpfx"] == "tr-%"


def _write_doc(
    root: Path,
    idx: int,
    *,
    title: str,
    text: str,
    language: str | None,
    domain: str = "example.com",
) -> None:
    day = root / "captures" / "2026" / "06" / "01"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}",
        capture_id=f"cap-{idx}",
        source_type="rss",
        domain=domain,
        url=f"https://{domain}/{idx}",
        fetch_ts="2026-06-01T12:00:00+00:00",
        title=title,
        text=text,
        language=language,
    )
    (day / f"chunk-{idx}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


@pytest.fixture()
def lang_index(tmp_path: Path) -> DuckDbIndex:
    jsonl_dir = tmp_path / "jsonl"
    _write_doc(jsonl_dir, 1, title="Alpha en", text="alpha bare english", language="en")
    _write_doc(jsonl_dir, 2, title="Alpha US", text="alpha american english", language="en-US")
    _write_doc(jsonl_dir, 3, title="Alpha underscore", text="alpha uk english", language="en_GB")
    _write_doc(jsonl_dir, 4, title="Alpha tr", text="alpha turkce metin", language="tr")
    _write_doc(jsonl_dir, 5, title="Alpha none", text="alpha unknown lang", language=None)
    return DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=jsonl_dir,
        iceberg_warehouse=None,
    )


def test_search_primary_language_matches_regional_subtags(lang_index: DuckDbIndex) -> None:
    """language=en must include en, en-US, and en_GB (underscore store)."""
    res = lang_index.search("alpha", mode="substring", language="en")
    langs = {str(r.get("language") or "").lower().replace("_", "-") for r in res["rows"]}
    assert res["total"] == 3
    assert langs == {"en", "en-us", "en-gb"}
    # Full tag stays exact (does not pull bare en or other regions).
    us = lang_index.search("alpha", mode="substring", language="en-US")
    assert us["total"] == 1
    assert str(us["rows"][0].get("language") or "").lower() in ("en-us", "en_us")
    # Underscore input normalizes to same full-tag match.
    us2 = lang_index.search("alpha", mode="substring", language="en_US")
    assert us2["total"] == 1
    # Turkish primary only.
    tr = lang_index.search("alpha", mode="substring", language="TR")
    assert tr["total"] == 1
    assert str(tr["rows"][0].get("language") or "").lower() == "tr"
