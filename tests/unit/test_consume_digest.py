"""Tests for the weekly digest generator (digest.py)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from awareness.consume.digest import generate_digest, render_digest_markdown
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
    when: datetime,
    domain: str,
    title: str,
    language: str = "en",
) -> None:
    day_dir = root / "captures" / when.strftime("%Y/%m/%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    ts = when.isoformat()
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{capture_id}",
        capture_id=capture_id,
        parent_doc_or_dup_group=None,
        source_type="rss",
        domain=domain,
        url=f"https://{domain}/{capture_id}",
        canonical_url=f"https://{domain}/{capture_id}",
        fetch_ts=ts,
        observed_ts=ts,
        title=title,
        text=f"article text for {title}",
        language=language,
    )
    (day_dir / f"{capture_id}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _index(tmp_path: Path, *, now: datetime) -> DuckDbIndex:
    jsonl = tmp_path / "jsonl"
    # Previous window (now-9d / now-8d): 2 captures on alpha.example.
    _write(
        jsonl,
        capture_id="p1",
        when=now - timedelta(days=9),
        domain="alpha.example",
        title="Old headline one",
    )
    _write(
        jsonl,
        capture_id="p2",
        when=now - timedelta(days=8),
        domain="alpha.example",
        title="Old headline two",
    )
    # Current window: alpha.example x3, brandnew.example x2 (first-seen here).
    _write(
        jsonl,
        capture_id="n1",
        when=now - timedelta(days=2),
        domain="alpha.example",
        title="Fresh market analysis",
    )
    _write(
        jsonl,
        capture_id="n2",
        when=now - timedelta(days=1, hours=2),
        domain="brandnew.example",
        title="Brand new exclusive scoop",
    )
    _write(
        jsonl,
        capture_id="n3",
        when=now - timedelta(days=1),
        domain="brandnew.example",
        title="Brand new follow-up story",
    )
    _write(
        jsonl,
        capture_id="n4",
        when=now - timedelta(hours=6),
        domain="alpha.example",
        title="Fresh market wrap",
    )
    return DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=jsonl,
        iceberg_warehouse=None,
    )


def _empty_index(tmp_path: Path) -> DuckDbIndex:
    return DuckDbIndex(
        db_path=tmp_path / "duckdb" / "metadata.duckdb",
        jsonl_dir=tmp_path / "jsonl",
        iceberg_warehouse=None,
    )


def test_digest_growth_rate_two_window_corpus(tmp_path: Path) -> None:
    now = datetime.now(tz=UTC)
    index = _index(tmp_path, now=now)
    digest = generate_digest(index, days=7, title="Weekly Test Digest")

    assert digest.title == "Weekly Test Digest"
    assert digest.days == 7
    assert digest.total_captures == 4  # n1..n4 within [now-7d, now]
    assert digest.previous_captures == 2  # p1, p2 within [now-14d, now-7d)
    assert digest.growth_rate == pytest.approx(1.0)  # 4 / 2 - 1
    assert digest.new_domains == ["brandnew.example"]
    assert [(d.term, d.count) for d in digest.top_domains] == [
        ("alpha.example", 2),  # count DESC, then domain ASC (tie with brandnew)
        ("brandnew.example", 2),
    ]
    assert digest.sample_titles == [
        "Fresh market wrap",
        "Brand new follow-up story",
        "Brand new exclusive scoop",
        "Fresh market analysis",
    ]
    assert [(lang.term, lang.count) for lang in digest.languages] == [("en", 4)]

    # Deterministic content: same corpus → identical digest (modulo the
    # wall-clock window which advances between the two calls).
    again = generate_digest(index, days=7, title="Weekly Test Digest")

    def _content(d: object) -> dict:
        payload = d.model_dump()
        for key in (
            "generated_at",
            "window_start",
            "window_end",
            "previous_window_start",
            "previous_window_end",
        ):
            payload.pop(key, None)
        return payload

    assert _content(again) == _content(digest)


def test_digest_top_terms_deterministic(tmp_path: Path) -> None:
    now = datetime.now(tz=UTC)
    index = _index(tmp_path, now=now)
    digest = generate_digest(index, days=7)

    terms = [t.term for t in digest.top_terms]
    assert "market" in terms  # appears in n1 + n4 titles/bodies
    assert "brand" in terms  # n2 + n3
    assert all(len(t) >= 3 for t in terms)
    assert len(terms) <= 20

    entities = [e.term for e in digest.top_entities]
    assert "brand new" in entities
    assert "fresh market" in entities


def test_digest_empty_corpus_is_zeroed(tmp_path: Path) -> None:
    index = _empty_index(tmp_path)
    digest = generate_digest(index, days=7)
    assert digest.total_captures == 0
    assert digest.previous_captures == 0
    assert digest.growth_rate is None
    assert digest.new_domains == []
    assert digest.top_domains == []
    assert digest.top_terms == []
    assert digest.top_entities == []
    assert digest.sample_titles == []
    assert digest.languages == []


def test_render_markdown_contains_key_sections(tmp_path: Path) -> None:
    now = datetime.now(tz=UTC)
    index = _index(tmp_path, now=now)
    digest = generate_digest(index, days=7)
    md = render_digest_markdown(digest)

    for section in (
        "# Weekly Digest",
        "## At a glance",
        "| Metric | Value |",
        "| Captures (this window) | 4 |",
        "| Growth vs previous window | +100.0% |",
        "## Top domains",
        "1. alpha.example — 2 captures",
        "### Newly seen domains",
        "brandnew.example",
        "## Top terms",
        "## Top title entities",
        "## Headlines",
        "- Fresh market wrap",
        "## Notes on growth",
        "grew +100.0%",
    ):
        assert section in md, f"missing section in markdown: {section!r}"

    # Language row reflects the breakdown.
    assert "| Languages | en |" in md


def test_render_markdown_empty_corpus(tmp_path: Path) -> None:
    index = _empty_index(tmp_path)
    md = render_digest_markdown(generate_digest(index, days=7))
    assert "## Notes on growth" in md
    assert "n/a (no previous data)" in md
    assert "_No captures in window._" in md
    assert "_No data in window._" in md
