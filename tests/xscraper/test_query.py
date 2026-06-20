from __future__ import annotations

from datetime import timedelta

import pytest

from awareness.xscraper.query import build_search_query, parse_lookback


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10m", timedelta(minutes=10)),
        ("1h", timedelta(hours=1)),
        ("2h30m", timedelta(hours=2, minutes=30)),
        ("1d12h", timedelta(days=1, hours=12)),
    ],
)
def test_parse_lookback_supports_composite_windows(text: str, expected: timedelta) -> None:
    assert parse_lookback(text) == expected


def test_build_search_query_combines_keywords_accounts_and_advanced_flags() -> None:
    query = build_search_query(
        keywords=["ai safety", "open source"],
        accounts=["@OpenAI", "anthropic"],
        raw_query="has:links",
        language="tr",
        include_retweets=False,
        include_replies=False,
    )

    assert '("ai safety" OR "open source")' in query
    assert '(from:OpenAI OR from:anthropic)' in query
    assert 'has:links' in query
    assert 'lang:tr' in query
    assert '-is:retweet' in query
    assert '-is:reply' in query
