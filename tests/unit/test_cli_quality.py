"""CLI tests for ``awareness quality`` (corpus report) and ``awareness feeds``
(feed-health report).

``quality`` builds a tiny JSONL corpus under the tmp project root (same chunk
pattern as the rest of the unit suite) and drives the command through Typer's
CliRunner. ``feeds`` is exercised against a fake metrics registry whose
snapshot mirrors :meth:`awareness.obs.metrics.MetricsRegistry.snapshot`, so the
command never depends on real process counters.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from awareness.cli.main import app, summarize_feed_health

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
    ts: datetime,
    text: str = "",
    domain: str = "example.com",
    language: str | None = None,
    content_hash: str | None = None,
) -> None:
    day = root / "captures" / f"{ts:%Y}" / f"{ts:%m}" / f"{ts:%d}"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}",
        capture_id=f"cap-{idx:04d}",
        source_type="rss",
        domain=domain,
        url=f"https://{domain}/{idx}",
        fetch_ts=ts.isoformat(),
        observed_ts=ts.isoformat(),
        title=f"doc {idx}",
        text=text,
        language=language,
        content_hash=content_hash,
    )
    (day / f"chunk-{idx:04d}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _quality_corpus(tmp_project: Path) -> None:
    """Six docs: one exact-dup pair (shared content_hash), one empty text,
    varied languages/domains."""
    root = tmp_project / "data" / "jsonl"
    now = datetime.now(UTC)
    _write_doc(
        root, 1, ts=now - timedelta(days=1), domain="news.example", language="en",
        text="alpha market report", content_hash="h-dup",
    )
    _write_doc(
        root, 2, ts=now - timedelta(hours=20), domain="news.example", language="en",
        text="alpha market report", content_hash="h-dup",
    )
    _write_doc(
        root, 3, ts=now - timedelta(hours=10), domain="blog.example", language="tr",
        text="", content_hash="h-3",
    )
    _write_doc(
        root, 4, ts=now - timedelta(hours=8), domain="blog.example", language="tr",
        text="beta yazi", content_hash="h-4",
    )
    _write_doc(
        root, 5, ts=now - timedelta(hours=5), domain="markets.example", language="de",
        text="gamma nachrichten", content_hash="h-5",
    )
    _write_doc(
        root, 6, ts=now - timedelta(hours=1), domain="news.example", language="en",
        text="delta report", content_hash="h-6",
    )


# ── quality ─────────────────────────────────────────────────────────────────


def test_quality_table_report(tmp_project: Path) -> None:
    _quality_corpus(tmp_project)
    result = runner.invoke(app, ["quality"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "total_captures" in out
    assert "6" in out
    assert "33.3%" in out  # 2 of 6 docs share a content_hash
    assert "news.example" in out  # top domain (3 captures)
    assert "en" in out  # top language
    assert "█" in out  # scaled language bar


def test_quality_json_output(tmp_project: Path) -> None:
    _quality_corpus(tmp_project)
    result = runner.invoke(app, ["quality", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["total_captures"] == 6
    assert payload["empty_text"] == 1
    assert payload["duplicate_ratio"] == pytest.approx(2 / 6)
    assert payload["top_domains"][0] == {"domain": "news.example", "count": 3}
    assert payload["languages"] == {"en": 3, "tr": 2, "de": 1}


def test_quality_empty_corpus_message(tmp_project: Path) -> None:
    result = runner.invoke(app, ["quality"])
    assert result.exit_code == 0, result.output
    assert "empty corpus" in result.output


# ── feeds ───────────────────────────────────────────────────────────────────


class _FakeMetrics:
    """Minimal stand-in for MetricsRegistry: snapshot() returns a crafted dict."""

    def __init__(self, snap: dict) -> None:
        self._snap = snap

    def snapshot(self, **kwargs: object) -> dict:
        return self._snap


def _feed_health_snapshot() -> dict:
    """100 attempts (98 ok, 1 http error, 1 retry_exhausted), 1 non-200,
    3 tail non-200, fetch p95 = 0.25s → score = 100 - 10*1 - 5*1 = 85."""
    return {
        "uptime_seconds": 60.0,
        "counters": [
            {"name": "feeds.fetch_attempts", "labels": {"outcome": "ok", "kind": "rss"}, "value": 50},
            {"name": "feeds.fetch_attempts", "labels": {"outcome": "ok", "kind": "sitemap"}, "value": 48},
            {"name": "feeds.fetch_attempts", "labels": {"outcome": "http_error", "kind": "rss"}, "value": 1},
            {"name": "feeds.fetch_attempts", "labels": {"outcome": "retry_exhausted", "kind": "rss"}, "value": 1},
            {"name": "feeds.fetch_non_200", "labels": {"kind": "rss", "status": "503"}, "value": 1},
            {"name": "tail.fetch_non_200", "labels": {}, "value": 3},
        ],
        "gauges": [],
        "histograms": [
            {
                "name": "feeds.fetch_seconds",
                "labels": {"outcome": "ok", "kind": "rss"},
                "count": 100,
                "p95": 0.25,
            },
        ],
    }


def test_summarize_feed_health_buckets_outcomes() -> None:
    snap = {
        "counters": [
            {"name": "feeds.fetch_attempts", "labels": {"outcome": "ok", "kind": "rss"}, "value": 10},
            {"name": "feeds.fetch_attempts", "labels": {"outcome": "http_error", "kind": "rss"}, "value": 2},
            {"name": "feeds.fetch_attempts", "labels": {"outcome": "retry_exhausted", "kind": "rss"}, "value": 1},
            {"name": "feeds.fetch_attempts", "labels": {"outcome": "transport_error", "kind": "rss"}, "value": 1},
        ],
        "histograms": [],
    }
    summary = summarize_feed_health(snap)
    assert summary["attempts"] == 14
    assert summary["ok"] == 10
    assert summary["error"] == 3  # http_error + transport_error
    assert summary["retry_exhausted"] == 1
    assert summary["score"] == 0  # clamped: 100 - 10*21.4% < 0


def test_feeds_table_reports_health(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "awareness.cli.main.get_metrics", lambda: _FakeMetrics(_feed_health_snapshot())
    )
    result = runner.invoke(app, ["feeds"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "attempts" in out
    assert "100" in out
    assert "1.0%" in out  # error and non-200 rates
    assert "250 ms" in out  # p95 0.25s
    assert "85" in out  # health score
    assert "tail non-200" in out


def test_feeds_json_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "awareness.cli.main.get_metrics", lambda: _FakeMetrics(_feed_health_snapshot())
    )
    result = runner.invoke(app, ["feeds", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["attempts"] == 100
    assert payload["ok"] == 98
    assert payload["error"] == 1
    assert payload["retry_exhausted"] == 1
    assert payload["non200"] == 1
    assert payload["tail_non200"] == 3
    assert payload["p95_sec"] == pytest.approx(0.25)
    assert payload["error_rate_pct"] == pytest.approx(1.0)
    assert payload["score"] == 85


def test_feeds_no_activity_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "awareness.cli.main.get_metrics",
        lambda: _FakeMetrics(
            {"uptime_seconds": 5.0, "counters": [], "gauges": [], "histograms": []}
        ),
    )
    result = runner.invoke(app, ["feeds"])
    assert result.exit_code == 0, result.output
    assert "no fetch activity recorded" in result.output
