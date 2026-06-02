"""Unit tests for the ingest-time TopicFilter."""

from __future__ import annotations

from awareness.filters import TopicFilter


def test_keyword_or_default() -> None:
    f = TopicFilter(["climate", "carbon"])
    assert f.active
    assert f.matches("Carbon tax debate", "body")
    assert f.matches("", "a story about the climate")
    assert not f.matches("Sports roundup", "football scores")


def test_keyword_and_requires_all() -> None:
    f = TopicFilter(["climate", "policy"], match_all=True)
    assert f.matches("climate policy update", "")
    assert not f.matches("climate only", "")
    assert not f.matches("policy only", "")


def test_case_insensitive() -> None:
    assert TopicFilter(["CLIMATE"]).matches("the Climate Report", "")


def test_keyword_is_whole_word_not_substring() -> None:
    # The classic footgun: "ai" must NOT match "said"/"campaign", only the word AI.
    f = TopicFilter(["ai"])
    assert f.matches("New AI model released", "")
    assert f.matches("breakthrough in a.i.", "") is False  # punctuated form isn't the bare word
    assert not f.matches("he said the campaign remains", "")
    # phrases still work as whole phrases
    assert TopicFilter(["climate policy"]).matches("the climate policy debate", "")


def test_keyword_word_boundary_vs_regex_substring() -> None:
    # whole-word default: "crypto" does not match "cryptocurrency"
    assert not TopicFilter(["crypto"]).matches("cryptocurrency surges", "")
    # regex mode gives partial/substring power back
    assert TopicFilter(["crypto"], regex=True).matches("cryptocurrency surges", "")


def test_terms_with_punctuation_edges() -> None:
    # leading/trailing punctuation: boundaries are skipped where they'd never match
    assert TopicFilter([".net"]).matches("built on .net core", "")
    assert TopicFilter(["c++"]).matches("a c++ tutorial", "")


def test_regex_mode() -> None:
    f = TopicFilter([r"climate\s+change"], regex=True)
    assert f.matches("a climate   change story", "")
    assert not f.matches("climate", "")


def test_bad_regex_falls_back_to_literal() -> None:
    # An invalid regex must not raise; it degrades to a literal substring.
    f = TopicFilter(["climate(", "carbon["], regex=True)
    assert f.active
    assert f.matches("about climate( growth", "")
    assert not f.matches("unrelated", "")


def test_field_selection() -> None:
    title_only = TopicFilter(["climate"], field="title")
    assert title_only.matches("Climate news", "football")
    assert not title_only.matches("Sports", "a climate body")

    text_only = TopicFilter(["climate"], field="text")
    assert text_only.matches("Sports", "a climate body")
    assert not text_only.matches("Climate news", "football")

    both = TopicFilter(["climate"], field="both")
    assert both.matches("Climate news", "football")
    assert both.matches("Sports", "a climate body")


def test_invalid_field_defaults_to_both() -> None:
    assert TopicFilter(["x"], field="nonsense").field == "both"


def test_inactive_filter_passes_everything() -> None:
    f = TopicFilter([])
    assert not f.active
    assert f.matches("anything", "at all")
    assert f.matches("", "")


def test_whitespace_only_terms_are_dropped() -> None:
    # A blank/whitespace --match must collapse to an inactive (pass-all) filter,
    # never an "active" filter that matches runs of spaces.
    assert not TopicFilter(["   "]).active
    assert not TopicFilter(["\t", " ", ""]).active
    assert TopicFilter(["   "]).matches("totally unrelated text", "")
    # surrounding whitespace on a real term is trimmed, not treated as the term
    assert TopicFilter(["  climate  "]).matches("a climate story", "")
    assert TopicFilter.from_config({"match": ["  ", "\n"]}) is None


def test_from_config_variants() -> None:
    assert TopicFilter.from_config(None) is None
    assert TopicFilter.from_config({}) is None
    assert TopicFilter.from_config({"match": []}) is None
    assert TopicFilter.from_config({"match": ["x"]}).matches("x marks", "")
    # string instead of list is tolerated
    assert TopicFilter.from_config({"match": "solo"}).matches("solo act", "")
    cfg = {"match": ["a", "b"], "match_all": True, "match_regex": False, "match_field": "title"}
    f = TopicFilter.from_config(cfg)
    assert f.match_all and f.field == "title"


def test_describe() -> None:
    assert "off" in TopicFilter([]).describe()
    assert "OR" in TopicFilter(["a", "b"]).describe()
    assert "AND" in TopicFilter(["a", "b"], match_all=True).describe()
    assert "regex" in TopicFilter(["a"], regex=True).describe()
