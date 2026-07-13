from __future__ import annotations

import json
from pathlib import Path
import pytest
from typer.testing import CliRunner

from awareness.cli.main import app, highlight_tokens

runner = CliRunner()

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
    domain: str = "example.com",
    content_hash: str | None = None,
    fetch_ts: str = "2026-06-01T12:00:00+00:00",
    language: str | None = None,
) -> None:
    day = root / "data" / "jsonl" / "captures" / "2026" / "06" / "01"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}",
        capture_id=f"cap-{idx}",
        source_type="rss",
        domain=domain,
        url=f"https://{domain}/{idx}",
        fetch_ts=fetch_ts,
        title=title,
        text=text,
        content_hash=content_hash,
        language=language,
    )
    (day / f"chunk-{idx}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def test_highlight_tokens_helper() -> None:
    # 1. Empty query
    assert highlight_tokens("hello world", "") == "hello world"
    # 2. Token too short
    assert highlight_tokens("hello world", "a") == "hello world"
    # 3. Simple matching
    assert highlight_tokens("The sports news was great.", "sports") == "The [bold yellow]sports[/bold yellow] news was great."
    # 4. Prefix matching
    assert highlight_tokens("The financial report.", "financ") == "The [bold yellow]financial[/bold yellow] report."
    # 5. Case insensitivity
    assert highlight_tokens("The SPORTS news.", "sports") == "The [bold yellow]SPORTS[/bold yellow] news."
    # 6. HTML/Rich tag escaping (the [awesome] becomes escaped and highlighted)
    assert highlight_tokens("An [awesome] link.", "awesome") == "An \\[[bold yellow]awesome[/bold yellow]] link."


def test_search_non_interactive_highlighting(tmp_project: Path) -> None:
    # Populate documents
    _write_doc(tmp_project, 1, title="Breaking sports news", text="Football match ended today.")
    _write_doc(tmp_project, 2, title="Global financial markets", text="Stock indices rose today.")
    
    # Run search command with --no-interactive
    result = runner.invoke(app, ["search", "sports", "--no-interactive"])
    assert result.exit_code == 0
    # Check that text is printed correctly (stripped of rich tags in non-TTY)
    assert "• Breaking sports news" in result.output
    
    # Text with sports in body text
    _write_doc(tmp_project, 3, title="More news", text="This is a sports news text.")
    result_snippet = runner.invoke(app, ["search", "sports", "--no-interactive"])
    assert result_snippet.exit_code == 0
    assert "sports news text." in result_snippet.output


def test_search_calls_highlight_tokens(tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_doc(tmp_project, 1, title="Breaking sports news", text="Football match ended today.")
    from awareness.cli.main import search
    called = []
    original = highlight_tokens
    def mock_highlight_tokens(text: str, query: str) -> str:
        called.append((text, query))
        return original(text, query)
    monkeypatch.setitem(search.__globals__, "highlight_tokens", mock_highlight_tokens)
    
    result = runner.invoke(app, ["search", "sports", "--no-interactive"])
    import sys
    print("GLOBALS NAME:", search.__globals__["__name__"])
    print("SYS.MODULES main:", sys.modules.get("awareness.cli.main"))
    print("SYS.MODULES main.highlight_tokens:", getattr(sys.modules.get("awareness.cli.main"), "highlight_tokens", None))
    print("CALLED:", called)
    assert result.exit_code == 0
    assert len(called) > 0
    assert any(c[1] == "sports" for c in called)


def test_browse_query_filter_and_highlighting(tmp_project: Path) -> None:
    # Populate documents
    _write_doc(tmp_project, 1, title="Breaking sports news", text="Football match ended today.")
    _write_doc(tmp_project, 2, title="Global financial markets", text="Stock indices rose today.")

    # 1. Run browse without query: should show both
    import os
    jsonl_dir = tmp_project / "data" / "jsonl"
    print("STAGING FILES:", list(jsonl_dir.rglob("*.jsonl")))
    
    from awareness.storage.duckdb_index import DuckDbIndex
    idx = DuckDbIndex(
        db_path=tmp_project / "data" / "duckdb" / "metadata.duckdb",
        jsonl_dir=jsonl_dir,
        iceberg_warehouse=None,
    )
    print("DB ROWS:", idx.execute("SELECT * FROM captures"))
    
    result_all = runner.invoke(app, ["browse"], input="q\n")
    print("BROWSE ALL OUTPUT:")
    print(result_all.output)
    assert result_all.exit_code == 0
    assert "sports" in result_all.output
    assert "financial" in result_all.output

    # 2. Run browse with query: should only show matching doc
    result_filtered = runner.invoke(app, ["browse", "--query", "sports"], input="q\n")
    assert result_filtered.exit_code == 0
    assert "sports" in result_filtered.output
    assert "financial" not in result_filtered.output

    # 3. Read view in browse should display correctly
    result_read = runner.invoke(app, ["browse", "--query", "sports"], input="1\nr\nq\n")
    if result_read.exit_code != 0:
        print("OUTPUT:")
        print(result_read.output)
        if result_read.exception:
            import traceback
            traceback.print_exception(type(result_read.exception), result_read.exception, result_read.exception.__traceback__)
        assert False
    assert "Title:       Breaking sports news" in result_read.output


def test_browse_calls_highlight_tokens(tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_doc(tmp_project, 1, title="Breaking sports news", text="Football match ended today.")
    from awareness.cli.main import browse
    called = []
    original = highlight_tokens
    def mock_highlight_tokens(text: str, query: str) -> str:
        called.append((text, query))
        return original(text, query)
    monkeypatch.setitem(browse.__globals__, "highlight_tokens", mock_highlight_tokens)
    
    result = runner.invoke(app, ["browse", "--query", "sports"], input="1\nr\nq\n")
    assert result.exit_code == 0
    assert len(called) > 0
    assert any(c[1] == "sports" for c in called)


def test_browse_unique_content_collapses_and_shows_flag(tmp_project: Path) -> None:
    """browse --unique content folds same content_hash and labels the pager."""
    body = "Shared body text about climate policy updates worldwide."
    _write_doc(
        tmp_project,
        10,
        title="Climate old copy",
        text=body,
        content_hash="hash-shared",
        fetch_ts="2026-06-01T10:00:00+00:00",
    )
    _write_doc(
        tmp_project,
        11,
        title="Climate newest copy",
        text=body,
        content_hash="hash-shared",
        fetch_ts="2026-06-01T14:00:00+00:00",
    )
    _write_doc(
        tmp_project,
        12,
        title="Unrelated markets brief",
        text="Stock indices rose in afternoon trading session.",
        content_hash="hash-other",
        fetch_ts="2026-06-01T12:00:00+00:00",
    )

    raw = runner.invoke(app, ["browse"], input="q\n")
    assert raw.exit_code == 0
    assert "Climate old copy" in raw.output
    assert "Climate newest copy" in raw.output
    assert "unique=content" not in raw.output

    folded = runner.invoke(app, ["browse", "--unique", "content"], input="q\n")
    assert folded.exit_code == 0
    assert "unique=content" in folded.output
    assert "Climate newest copy" in folded.output
    assert "Climate old copy" not in folded.output
    assert "Unrelated markets brief" in folded.output


def test_browse_unique_invalid_mode(tmp_project: Path) -> None:
    result = runner.invoke(app, ["browse", "--unique", "bogus"], input="q\n")
    assert result.exit_code == 2
    assert "invalid unique mode" in result.output.lower()


def test_browse_lang_filter_case_insensitive(tmp_project: Path) -> None:
    """browse --lang keeps only matching BCP-47 language rows and labels pager."""
    _write_doc(
        tmp_project,
        20,
        title="English sports brief",
        text="Football match ended today in London.",
        language="en",
    )
    _write_doc(
        tmp_project,
        21,
        title="Turkish markets note",
        text="Borsa Istanbul yukseldi bugun.",
        language="tr",
    )
    _write_doc(
        tmp_project,
        22,
        title="German markets note",
        text="Die Aktien stiegen heute stark.",
        language="DE",  # upper-case in store; filter is lower() matched
    )

    all_rows = runner.invoke(app, ["browse"], input="q\n")
    assert all_rows.exit_code == 0
    assert "English sports brief" in all_rows.output
    assert "Turkish markets note" in all_rows.output
    assert "German markets note" in all_rows.output

    en_only = runner.invoke(app, ["browse", "--lang", "EN"], input="q\n")
    assert en_only.exit_code == 0
    assert "lang=en" in en_only.output
    assert "English sports brief" in en_only.output
    assert "Turkish markets note" not in en_only.output
    assert "German markets note" not in en_only.output

    tr_only = runner.invoke(app, ["browse", "--lang", "tr"], input="q\n")
    assert tr_only.exit_code == 0
    assert "lang=tr" in tr_only.output
    assert "Turkish markets note" in tr_only.output
    assert "English sports brief" not in tr_only.output


def test_browse_source_filter_case_insensitive(tmp_project: Path) -> None:
    """browse --source matches RSS vs rss (parity with API/search)."""
    _write_doc(
        tmp_project,
        30,
        title="RSS climate brief",
        text="Climate talks continue this week.",
    )
    day = tmp_project / "data" / "jsonl" / "captures" / "2026" / "06" / "01"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id="doc-31",
        capture_id="cap-31",
        source_type="gdelt",
        domain="gdelt.example",
        url="https://gdelt.example/31",
        fetch_ts="2026-06-01T13:00:00+00:00",
        title="GDELT markets brief",
        text="Markets moved after the report.",
        language="en",
    )
    (day / "chunk-31.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")

    rss_upper = runner.invoke(app, ["browse", "--source", "RSS"], input="q\n")
    assert rss_upper.exit_code == 0
    assert "RSS climate brief" in rss_upper.output
    assert "GDELT markets brief" not in rss_upper.output
    assert "source=rss" in rss_upper.output  # pager title surfaces active filter

    gdelt_mixed = runner.invoke(app, ["browse", "--source", "Gdelt"], input="q\n")
    assert gdelt_mixed.exit_code == 0
    assert "GDELT markets brief" in gdelt_mixed.output
    assert "RSS climate brief" not in gdelt_mixed.output
    assert "source=gdelt" in gdelt_mixed.output


def test_browse_domain_filter_surfaces_in_title(tmp_project: Path) -> None:
    """browse --domain is case-insensitive and labels the pager title."""
    _write_doc(
        tmp_project,
        40,
        title="Example climate note",
        text="Example body about climate.",
        domain="example.com",
    )
    _write_doc(
        tmp_project,
        41,
        title="Other markets note",
        text="Other body about markets.",
        domain="other.com",
    )
    result = runner.invoke(app, ["browse", "--domain", "Example.COM"], input="q\n")
    assert result.exit_code == 0
    assert "domain=example.com" in result.output
    assert "Example climate note" in result.output
    assert "Other markets note" not in result.output

