"""Ordered seen_urls window for feed checkpoints.

The checkpoint must retain a stable most-recently-seen window, not an
unordered set slice. Random set order would drop the wrong URLs and
re-discover recently seen articles.
"""

from __future__ import annotations

from awareness.sources.feeds import SEEN_URLS_CAP, merge_seen_urls
from awareness.util.urls import canonical_url


def test_merge_seen_urls_preserves_order_and_appends_new() -> None:
    prev = [
        "https://example.com/old-1",
        "https://example.com/old-2",
    ]
    discovered = [
        "https://example.com/new-1",
        "https://example.com/new-2",
    ]
    out = merge_seen_urls(prev, discovered)
    assert out == [
        "https://example.com/old-1",
        "https://example.com/old-2",
        "https://example.com/new-1",
        "https://example.com/new-2",
    ]


def test_merge_seen_urls_moves_reseen_to_end() -> None:
    prev = [
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
    ]
    # Re-discover "a" — it should become most recent.
    out = merge_seen_urls(prev, ["https://example.com/a"])
    assert out == [
        "https://example.com/b",
        "https://example.com/c",
        "https://example.com/a",
    ]


def test_merge_seen_urls_cap_keeps_newest() -> None:
    prev = [f"https://example.com/u/{i}" for i in range(10)]
    discovered = [f"https://example.com/u/{i}" for i in range(10, 15)]
    out = merge_seen_urls(prev, discovered, cap=5)
    assert len(out) == 5
    # Newest five after merge: u/10..u/14 (previous oldest dropped).
    assert out == [f"https://example.com/u/{i}" for i in range(10, 15)]


def test_merge_seen_urls_cap_drops_oldest_when_previous_alone_exceeds() -> None:
    prev = [f"https://example.com/p/{i}" for i in range(8)]
    out = merge_seen_urls(prev, [], cap=3)
    assert out == [
        "https://example.com/p/5",
        "https://example.com/p/6",
        "https://example.com/p/7",
    ]


def test_merge_seen_urls_canonicalizes_discovered() -> None:
    prev = ["https://example.com/story"]
    discovered = [
        "HTTPS://Example.COM/story?utm_source=rss",
        "https://example.com/other",
    ]
    out = merge_seen_urls(prev, discovered)
    assert out == [
        "https://example.com/story",  # re-seen, moved to end as canonical
        "https://example.com/other",
    ]
    # Explicit: re-seen canonical form is at end after first pass then move.
    # Wait — previous already had story, then discovered re-sees it (move end),
    # then appends other. So order is: (after pop+append story) [story], then other.
    # Actually: start {story}, discover story → pop+append → {story}, discover other → {story, other}
    assert out[-1] == "https://example.com/other"
    assert out[0] == canonical_url("HTTPS://Example.COM/story?utm_source=rss")


def test_merge_seen_urls_skips_empty_and_invalid() -> None:
    out = merge_seen_urls(["https://ok.example/x", ""], ["", "not a url", "https://ok.example/y"])
    assert out == ["https://ok.example/x", "https://ok.example/y"]


def test_merge_seen_urls_default_cap_is_5000() -> None:
    assert SEEN_URLS_CAP == 5000
    # Build previous at cap, add one more → oldest dropped, newest retained.
    prev = [f"https://example.com/n/{i}" for i in range(SEEN_URLS_CAP)]
    out = merge_seen_urls(prev, ["https://example.com/n/newest"])
    assert len(out) == SEEN_URLS_CAP
    assert out[0] == "https://example.com/n/1"  # n/0 dropped
    assert out[-1] == "https://example.com/n/newest"
    assert "https://example.com/n/0" not in out


def test_merge_seen_urls_stable_across_repeated_empty_merges() -> None:
    """Unlike set-slicing, repeated merges must not reshuffle order."""
    prev = [f"https://example.com/s/{i}" for i in range(20)]
    once = merge_seen_urls(prev, [], cap=10)
    twice = merge_seen_urls(once, [], cap=10)
    thrice = merge_seen_urls(twice, [], cap=10)
    assert once == twice == thrice == [f"https://example.com/s/{i}" for i in range(10, 20)]
