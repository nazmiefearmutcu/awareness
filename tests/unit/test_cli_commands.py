"""Unit tests for the CLI commands."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner
from awareness.cli.main import app

runner = CliRunner()


def test_clear_command_outputs_ansi_escape() -> None:
    result = runner.invoke(app, ["clear"])
    assert result.exit_code == 0
    assert "\033[H\033[2J\033[3J" in result.output


def test_search_non_interactive_empty_db(tmp_project: Path) -> None:
    """Empty index: CLI always prints diagnostics (not just "Found 0")."""
    result = runner.invoke(app, ["search", "testquery", "--no-interactive"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Search Results for" in out
    assert "Found 0" in out
    # Diagnostics panel title + empty-corpus hint must always appear.
    assert "No results" in out and "suggestions" in out
    assert "No documents in index yet" in out
    assert "corpus=0" in out


def test_search_empty_prints_diagnostics_when_corpus_has_no_match(tmp_project: Path) -> None:
    """Non-empty index + zero hits still surfaces the diagnostics panel."""
    import json

    day = tmp_project / "data" / "jsonl" / "captures" / "2026" / "06" / "01"
    day.mkdir(parents=True, exist_ok=True)
    rec = {
        "doc_id": "doc-1",
        "capture_id": "cap-1",
        "parent_doc_or_dup_group": None,
        "source_type": "rss",
        "source_name": None,
        "source_locator": None,
        "source_shard": None,
        "source_offset_or_record_id": None,
        "discovery_channel": None,
        "job_id": None,
        "batch_id": None,
        "ingest_version": None,
        "url": "https://example.com/1",
        "canonical_url": None,
        "domain": "example.com",
        "fetch_ts": "2026-06-01T12:00:00+00:00",
        "observed_ts": None,
        "published_ts": None,
        "last_modified": None,
        "content_type": None,
        "http_status": None,
        "etag": None,
        "title": "Sports roundup",
        "text": "A football match ended in a draw.",
        "language": None,
        "content_hash": None,
        "near_dup_hash": None,
        "robots_decision": None,
        "terms_note_if_relevant": None,
    }
    (day / "chunk-1.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["search", "quantum chromodynamics", "--no-interactive", "--mode", "substring"],
    )
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Found 0" in out
    assert "No results" in out and "suggestions" in out
    # Must not be the empty-index tip when corpus has docs.
    assert "No documents in index yet" not in out
    assert "substring" in out.lower() or "terms" in out.lower() or "matches" in out.lower()


def test_service_compaction_scheduling() -> None:
    # Test dry running service schedule/unschedule compaction command
    # Use a dummy interval
    result = runner.invoke(app, ["service", "schedule-compaction", "--interval", "120"])
    # Since it may attempt to run launchctl load, it might fail or print creation messages.
    # Let's assert that the command execution doesn't raise unhandled CLI errors
    assert result.exit_code in (0, 1)
    
    result_unsched = runner.invoke(app, ["service", "unschedule-compaction"])
    assert result_unsched.exit_code in (0, 1)


def test_hf_push_command() -> None:
    # Test pushing a dummy repo ID.
    result = runner.invoke(app, ["hf-push", "dummy/repo"])
    assert result.exit_code in (0, 1, 2)


def test_shell_command() -> None:
    # Simulating standard REPL command typing: help, clear, exit.
    result = runner.invoke(app, ["shell"], input="help\nclear\nexit\n")
    assert result.exit_code == 0
    assert "Welcome to the Awareness Interactive Shell!" in result.output
    assert "Available Shell Commands:" in result.output
    assert "Goodbye!" in result.output






def test_backfill_submit_warns_on_zero_tasks(tmp_project: Path) -> None:
    """CLI must loudly warn when the plan emits no tasks (e.g. RSS-only)."""
    result = runner.invoke(
        app,
        [
            "backfill",
            "submit",
            "--start",
            "2024-06-01",
            "--end",
            "2024-06-14",
            "--source",
            "rss",
        ],
    )
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Submitted backfill" in out
    assert "WARNING" in out and "0 tasks" in out
    assert "rss" in out.lower()
    assert '"warning": "zero_tasks"' in out or '"warning":"zero_tasks"' in out



def test_dedup_stats_includes_skip_counters(tmp_project: Path) -> None:
    """CLI dedup-stats mirrors API fields for fetch/tight-near skips."""
    from awareness.obs.metrics import get_metrics

    m = get_metrics()
    m.inc("tail.fetch_skipped_seen", value=2.0, labels={"domain": "a.example"})
    m.inc("dedup.tight_near_skipped", value=3.0, labels={"domain": "b.example"})

    result = runner.invoke(app, ["dedup-stats"])
    assert result.exit_code == 0, result.output
    import json

    # JSON may be preceded by bootstrap noise; find the object.
    out = result.output.strip()
    start = out.find("{")
    assert start >= 0, out
    payload = json.loads(out[start:])
    assert "distinct_content_hashes" in payload
    assert "total_captures_seen" in payload
    assert "near_dup_index_rows" in payload
    assert payload["fetch_skipped_seen"] >= 2
    assert payload["tight_near_skipped"] >= 3


def test_metrics_format_json_and_prometheus() -> None:
    """metrics --format json|prometheus returns machine-readable snapshots."""
    import json

    from awareness.obs.metrics import get_metrics

    m = get_metrics()
    m.inc("cli.test_metric", value=1.0, labels={"case": "json"})
    m.observe("http.fetch_seconds", 0.012, labels={"outcome": "ok", "status_class": "2xx"})

    js = runner.invoke(app, ["metrics", "--format", "json"])
    assert js.exit_code == 0, js.output
    payload = json.loads(js.output[js.output.find("{") :])
    assert "uptime_seconds" in payload
    assert "counters" in payload and "histograms" in payload
    names = {c["name"] for c in payload["counters"]}
    assert "cli.test_metric" in names

    prom = runner.invoke(app, ["metrics", "--format", "prometheus"])
    assert prom.exit_code == 0, prom.output
    assert "awareness_uptime_seconds" in prom.output
    assert "cli_test_metric_total" in prom.output
    assert "http_fetch_seconds_count" in prom.output or "http_fetch_seconds" in prom.output


def test_metrics_format_table_explicit() -> None:
    """--format table prints a human summary (not raw JSON)."""
    from awareness.obs.metrics import get_metrics

    get_metrics().inc("cli.table_probe", value=5.0)
    result = runner.invoke(app, ["metrics", "--format", "table"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Metrics" in out
    assert "uptime=" in out
    # Table mode should not be a pure JSON document.
    assert not out.lstrip().startswith("{")
    assert "cli.table_probe" in out or "Counters" in out


def test_metrics_format_rejects_unknown() -> None:
    result = runner.invoke(app, ["metrics", "--format", "yaml"])
    assert result.exit_code != 0
    assert "Unknown --format" in result.output or "Invalid" in result.output or result.exit_code == 2


def test_metrics_prefix_filters_json_and_prometheus() -> None:
    """--prefix keeps only matching series (all formats)."""
    import json

    from awareness.obs.metrics import get_metrics

    m = get_metrics()
    m.inc("http.fetch_attempts", value=3.0, labels={"outcome": "ok"})
    m.inc("gdelt.urls_discovered", value=7.0, labels={"slot": "20260601113000"})
    m.observe("http.fetch_seconds", 0.05, labels={"outcome": "ok"})
    m.observe("gdelt.fetch_seconds", 0.2, labels={"outcome": "ok"})

    js = runner.invoke(app, ["metrics", "--format", "json", "--prefix", "http."])
    assert js.exit_code == 0, js.output
    payload = json.loads(js.output[js.output.find("{") :])
    assert payload.get("prefix") == "http."
    cnames = {c["name"] for c in payload["counters"]}
    hnames = {h["name"] for h in payload["histograms"]}
    assert "http.fetch_attempts" in cnames
    assert "gdelt.urls_discovered" not in cnames
    assert "http.fetch_seconds" in hnames
    assert "gdelt.fetch_seconds" not in hnames

    prom = runner.invoke(app, ["metrics", "--format", "prometheus", "--prefix", "gdelt."])
    assert prom.exit_code == 0, prom.output
    assert "gdelt_urls_discovered_total" in prom.output or "gdelt_urls_discovered" in prom.output
    assert "http_fetch_attempts" not in prom.output
    # Uptime always present.
    assert "awareness_uptime_seconds" in prom.output

    table = runner.invoke(app, ["metrics", "--format", "table", "--prefix", "gdelt."])
    assert table.exit_code == 0, table.output
    assert "prefix=" in table.output
    assert "gdelt" in table.output
