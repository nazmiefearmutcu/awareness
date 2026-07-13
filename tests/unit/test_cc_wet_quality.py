"""WET records below Gopher/C4 quality are dropped when the filter is on.

Mirrors the codebase's WET-helper test convention (cf. test_cc_wet_domain_filter):
the per-record decision lives in a pure helper, tested directly without WARC I/O.
"""

from __future__ import annotations

import io
from pathlib import Path

from warcio.warcwriter import WARCWriter

from awareness.config.settings import Settings, reset_settings
from awareness.obs import metrics as metrics_mod
from awareness.obs.metrics import MetricsRegistry, get_metrics
from awareness.sources.commoncrawl_wet import (
    _parse_wet_to_captures,
    _record_passes_quality,
    _wet_quality_verdict,
)


def _clean() -> str:
    return (
        "The committee reviewed the annual report and approved the budget "
        "with broad support from the members that attended the meeting. "
    ) * 4


def test_quality_filter_default_is_on() -> None:
    assert Settings().wet_quality_filter is True


def test_clean_record_passes_when_enabled() -> None:
    assert _record_passes_quality(_clean(), enabled=True, lang="en") is True
    v = _wet_quality_verdict(_clean(), enabled=True, lang="en")
    assert v.ok is True
    assert v.reason is None


def test_junk_record_is_dropped_when_enabled() -> None:
    assert _record_passes_quality("buy now buy now", enabled=True, lang="en") is False
    v = _wet_quality_verdict("buy now buy now", enabled=True, lang="en")
    assert v.ok is False
    assert v.reason == "too_few_words"


def test_disabled_filter_passes_everything() -> None:
    assert _record_passes_quality("buy now buy now", enabled=False, lang="en") is True


def test_non_english_record_is_not_judged_by_english_gates() -> None:
    # English-leaning Gopher gates only judge English; a record the language
    # filter admitted in another language passes through unjudged (no silent
    # data loss for non-English WET text).
    assert _record_passes_quality("buy now buy now", enabled=True, lang="de") is True


# Passes text_min_chars + LID=en, but fails Gopher stopword gate.
_ENGLISH_JUNK = (
    "buy now click here free offer limited time act fast discount sale deal "
    "cheap price best price lowest price guaranteed money back offer expires "
) * 4


def test_quality_filter_emits_reason_and_admitted_metrics(tmp_path: Path) -> None:
    """Filtered junk is labelled by reason; clean records increment admitted."""
    reset_settings()
    metrics_mod._REGISTRY = MetricsRegistry()
    wet = tmp_path / "shard.warc"
    # Two records: English junk (quality drop) + clean English prose (admit).
    with open(wet, "wb") as fh:
        writer = WARCWriter(fh, gzip=False)
        for url, text in (
            ("http://example.com/junk", _ENGLISH_JUNK),
            ("http://example.com/ok", _clean()),
        ):
            payload = text.encode("utf-8")
            rec = writer.create_warc_record(
                url,
                "conversion",
                payload=io.BytesIO(payload),
                length=len(payload),
                warc_content_type="text/plain",
            )
            writer.write_record(rec)

    captures = _parse_wet_to_captures(
        path=wet,
        crawl_id="CC-MAIN-2026-06",
        shard_path="crawl-data/CC-MAIN-2026-06/segments/x/wet/shard.warc.wet.gz",
        domains_filter=None,
        languages_filter=None,
        user_agent="test-agent",
        job_id="job",
        task_id="task",
        batch_id="batch",
        ingest_version="v1",
    )
    assert len(captures) == 1

    m = get_metrics()
    filtered = m.counter_sum("cc_wet.quality_filtered")
    admitted = m.counter_sum("cc_wet.records_admitted")
    assert filtered >= 1
    assert admitted >= 1
    # Reason label must be present on at least one filtered series.
    snap = m.snapshot(prefix="cc_wet.")
    reasons = [
        (c.get("labels") or {}).get("reason")
        for c in snap.get("counters") or []
        if c.get("name") == "cc_wet.quality_filtered"
    ]
    assert any(r for r in reasons if r and r != "unknown")
