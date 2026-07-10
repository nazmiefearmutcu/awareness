"""Quality gating must run downstream of language selection, not upstream.

``gopher_quality`` carries English-leaning gates (the stopword signal, the
mean-word-length / alpha-fraction checks misfire on non-space-delimited or
non-English scripts). ``quality.py`` documents that this is safe *because*
non-English text is excluded by the language filter, not dropped by quality.

The default config keeps every language (``BackfillRequest.languages`` -> None
-> ``languages_filter`` None -> accept all), so a non-English record that LID
admits must survive the pipeline even with ``wet_quality_filter`` on. This pins
that ordering: a German record (detected ``de``, but failing the Gopher English
stopword gate) is NOT dropped when ``languages_filter`` is None.
"""

from __future__ import annotations

import io
from pathlib import Path

from warcio.warcwriter import WARCWriter

from awareness.config.settings import reset_settings
from awareness.normalize.quality import gopher_quality
from awareness.normalize.text import detect_language
from awareness.sources.commoncrawl_wet import _parse_wet_to_captures

# German prose: passes normalization + LID (de) but fails the English Gopher
# stopword gate, so it is the canary for upstream-of-LID quality gating.
_GERMAN = (
    "Der Ausschuss prüfte den Jahresbericht und genehmigte den Haushalt "
    "mit breiter Unterstützung der Mitglieder die an der Sitzung teilgenommen "
    "haben. "
) * 6


def _write_wet(path: Path, url: str, text: str) -> None:
    payload = text.encode("utf-8")
    with open(path, "wb") as fh:
        writer = WARCWriter(fh, gzip=False)
        rec = writer.create_warc_record(
            url,
            "conversion",
            payload=io.BytesIO(payload),
            length=len(payload),
            warc_content_type="text/plain",
        )
        writer.write_record(rec)


def test_german_record_is_a_valid_canary() -> None:
    # Guard: the regression below only bites if German truly fails the English
    # Gopher gate yet is admitted by LID.
    assert detect_language(_GERMAN) == "de"
    assert gopher_quality(_GERMAN).ok is False


def test_non_english_record_survives_when_languages_filter_is_none(tmp_path: Path) -> None:
    reset_settings()  # ensure default wet_quality_filter=True is in effect
    wet = tmp_path / "shard.warc"
    _write_wet(wet, "http://example.de/artikel", _GERMAN)

    captures = _parse_wet_to_captures(
        path=wet,
        crawl_id="CC-MAIN-2026-06",
        shard_path="crawl-data/CC-MAIN-2026-06/segments/x/wet/shard.warc.wet.gz",
        domains_filter=None,
        languages_filter=None,  # default: accept ALL languages
        user_agent="test-agent",
        job_id="job",
        task_id="task",
        batch_id="batch",
        ingest_version="v1",
    )

    assert len(captures) == 1, "non-English record dropped despite languages_filter=None"
    assert captures[0].language == "de"


# English junk: detected ``en`` and failing the Gopher stopword gate, so the
# English-leaning quality filter still applies and drops it (the fix must not
# loosen quality for English).
_ENGLISH_JUNK = (
    "buy now click here free offer limited time act fast discount sale deal "
    "cheap price best price lowest price guaranteed money back offer expires "
) * 4


def test_english_junk_is_still_dropped(tmp_path: Path) -> None:
    reset_settings()
    assert detect_language(_ENGLISH_JUNK) == "en"
    assert gopher_quality(_ENGLISH_JUNK).ok is False
    wet = tmp_path / "shard.warc"
    _write_wet(wet, "http://example.com/spam", _ENGLISH_JUNK)

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

    assert captures == [], "English junk should still be quality-filtered"
