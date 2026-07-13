"""Search response domain/source facets (GROUP BY on match set)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from awareness.storage.duckdb_index import DuckDbIndex

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
    title: str,
    text: str,
    domain: str,
    source_type: str = "rss",
    language: str | None = "en",
) -> None:
    day = root / "captures" / "2026" / "06" / "01"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}",
        capture_id=f"cap-{idx}",
        source_type=source_type,
        domain=domain,
        url=f"https://{domain}/{idx}",
        fetch_ts="2026-06-01T12:00:00+00:00",
        title=title,
        text=text,
        language=language,
    )
    (day / f"chunk-{idx}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


@pytest.fixture()
def faceted_index(tmp_path: Path) -> DuckDbIndex:
    jsonl_dir = tmp_path / "jsonl"
    # Shared term "alpha" across domains so facets are non-trivial.
    _write_doc(jsonl_dir, 1, title="Alpha news", text="alpha one", domain="a.example", language="en")
    _write_doc(jsonl_dir, 2, title="Alpha again", text="alpha two", domain="a.example", language="en")
    _write_doc(jsonl_dir, 3, title="Alpha other", text="alpha three", domain="b.example", language="tr")
    _write_doc(
        jsonl_dir, 4,
        title="Alpha wet", text="alpha four",
        domain="c.example", source_type="common_crawl_wet", language="en",
    )
    _write_doc(jsonl_dir, 5, title="Sports", text="football only", domain="sports.example")
    return DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=jsonl_dir,
        iceberg_warehouse=None,
    )


def test_search_facets_top_domains_prefix(faceted_index: DuckDbIndex) -> None:
    res = faceted_index.search("alpha", mode="prefix")
    assert res["total"] >= 3
    assert "facets" in res
    domains = res["facets"]["domains"]
    assert isinstance(domains, list) and domains
    # a.example appears twice → should rank first among domain facets.
    assert domains[0]["domain"] == "a.example"
    assert int(domains[0]["n"]) == 2
    names = {d["domain"] for d in domains}
    assert "b.example" in names
    assert "c.example" in names
    assert "sports.example" not in names  # no match
    assert len(domains) <= 10


def test_search_facets_sources_present(faceted_index: DuckDbIndex) -> None:
    res = faceted_index.search("alpha", mode="substring")
    assert res["total"] > 0
    sources = res["facets"]["sources"]
    by_src = {s["source_type"]: int(s["n"]) for s in sources}
    assert by_src.get("rss", 0) >= 2
    assert by_src.get("common_crawl_wet", 0) == 1


def test_search_facets_languages_present(faceted_index: DuckDbIndex) -> None:
    res = faceted_index.search("alpha", mode="prefix")
    assert res["total"] >= 3
    langs = res["facets"]["languages"]
    by_lang = {str(row["language"]): int(row["n"]) for row in langs}
    assert by_lang.get("en", 0) >= 2
    assert by_lang.get("tr", 0) == 1


def test_search_domain_filter_case_insensitive(faceted_index: DuckDbIndex) -> None:
    """Domain filter matches regardless of user/SPA casing."""
    upper = faceted_index.search("alpha", mode="substring", domain="A.EXAMPLE")
    lower = faceted_index.search("alpha", mode="substring", domain="a.example")
    assert upper["total"] == lower["total"]
    assert upper["total"] >= 1
    for row in upper["rows"]:
        assert str(row.get("domain") or "").lower() == "a.example"


def test_search_language_filter(faceted_index: DuckDbIndex) -> None:
    """Language filter narrows matches and is case-insensitive."""
    en = faceted_index.search("alpha", mode="substring", language="EN")
    tr = faceted_index.search("alpha", mode="substring", language="tr")
    assert en["total"] >= 2
    assert tr["total"] == 1
    for row in en["rows"]:
        assert str(row.get("language") or "").lower() == "en"
    for row in tr["rows"]:
        assert str(row.get("language") or "").lower() == "tr"
    # Facets on a language-filtered search still include languages.
    assert en.get("facets", {}).get("languages")


def test_search_facets_omitted_when_empty(faceted_index: DuckDbIndex) -> None:
    res = faceted_index.search("zzzz-no-match-zzzz", mode="substring")
    assert res["total"] == 0
    assert "facets" not in res


def test_search_facets_fts_when_available(
    faceted_index: DuckDbIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    from awareness.config import get_settings

    monkeypatch.setattr(get_settings(), "search_idf_threshold", 0.0)
    res = faceted_index.search("alpha", mode="fts")
    if res["total"] == 0:
        pytest.skip("FTS unavailable or no hits in this environment")
    assert res.get("ranked") is True
    assert res["facets"]["domains"]
    assert res["facets"]["domains"][0]["domain"] == "a.example"
    assert res["facets"].get("languages")
