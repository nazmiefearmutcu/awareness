"""M-27: alias-host stripping must not collapse real single-label domains.

``www.example.com`` → ``example.com`` (still), but ``www.com`` must stay
``www.com`` and ``m.me`` must stay ``m.me`` — stripping the alias prefix
would collapse a real registered domain to its bare TLD.
"""

from __future__ import annotations

from awareness.util.urls import canonical_url, _strip_alias_host


def test_alias_host_preserves_single_label_remainder() -> None:
    # www.com → "com" would be a 1-label remainder — refuse to strip.
    assert _strip_alias_host("www.com") == "www.com"
    assert _strip_alias_host("m.me") == "m.me"
    assert _strip_alias_host("mobile.com") == "mobile.com"
    assert _strip_alias_host("amp.dev") == "amp.dev"


def test_alias_host_still_strips_multi_label_remainders() -> None:
    assert _strip_alias_host("www.example.com") == "example.com"
    assert _strip_alias_host("m.news.example") == "news.example"
    assert _strip_alias_host("www.m.bbc.co.uk") == "bbc.co.uk"


def test_canonical_url_keeps_www_com_identity() -> None:
    assert canonical_url("https://www.com/x") == "https://www.com/x"
    assert canonical_url("https://m.me/x") == "https://m.me/x"
    # Normal multi-label aliasing still collapses.
    assert canonical_url("https://www.example.com/x") == "https://example.com/x"
    assert canonical_url("https://m.example.com/x") == "https://example.com/x"
