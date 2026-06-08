from __future__ import annotations

from awareness.sources.commoncrawl_wet import _normalize_domain_filter, _record_passes_domain_filter


def test_subdomain_request_matches_etld1_records() -> None:
    flt = _normalize_domain_filter(["news.bbc.co.uk", "www.cnn.com"])
    assert flt == {"bbc.co.uk", "cnn.com"}
    assert _record_passes_domain_filter("http://news.bbc.co.uk/a", flt) is True
    assert _record_passes_domain_filter("http://www.cnn.com/x", flt) is True
    assert _record_passes_domain_filter("http://example.org/y", flt) is False


def test_none_filter_passes_everything() -> None:
    assert _record_passes_domain_filter("http://anything.test/z", None) is True
