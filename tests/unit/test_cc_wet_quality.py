"""WET records below Gopher/C4 quality are dropped when the filter is on.

Mirrors the codebase's WET-helper test convention (cf. test_cc_wet_domain_filter):
the per-record decision lives in a pure helper, tested directly without WARC I/O.
"""

from __future__ import annotations

from awareness.config.settings import Settings
from awareness.sources.commoncrawl_wet import _record_passes_quality


def _clean() -> str:
    return (
        "The committee reviewed the annual report and approved the budget "
        "with broad support from the members that attended the meeting. "
    ) * 4


def test_quality_filter_default_is_on() -> None:
    assert Settings().wet_quality_filter is True


def test_clean_record_passes_when_enabled() -> None:
    assert _record_passes_quality(_clean(), enabled=True) is True


def test_junk_record_is_dropped_when_enabled() -> None:
    assert _record_passes_quality("buy now buy now", enabled=True) is False


def test_disabled_filter_passes_everything() -> None:
    assert _record_passes_quality("buy now buy now", enabled=False) is True
