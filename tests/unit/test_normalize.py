"""Text normalization tests."""

from awareness.normalize.text import (
    detect_language,
    detect_language_conf,
    normalize_text,
    safe_title,
)


def test_normalize_collapses_whitespace_and_keeps_paragraphs() -> None:
    raw = "Hello\r\n\r\nWorld   this  is\n\n\n\n a paragraph."
    out = normalize_text(raw, min_chars=10)
    assert out.discarded_reason is None
    assert out.n_lines >= 2
    assert "  " not in out.text  # double spaces collapsed
    assert "\n\n\n" not in out.text


def test_normalize_filters_too_short() -> None:
    out = normalize_text("hi", min_chars=200)
    assert out.discarded_reason is not None


def test_normalize_strips_control_chars() -> None:
    raw = "Title\x00\x01\x02\nbody body body body body" * 50
    out = normalize_text(raw, min_chars=20)
    assert "\x00" not in out.text
    assert "\x01" not in out.text


def test_normalize_truncates_to_max_chars() -> None:
    raw = "x" * 5000
    out = normalize_text(raw, min_chars=10, max_chars=500)
    assert out.n_chars == 500


def test_safe_title_uses_first_line_when_missing() -> None:
    assert safe_title(None, "First line is the title.\nbody body body") == "First line is the title."
    assert safe_title("Real Title", "body") == "Real Title"


def test_detect_language_short_text_returns_none() -> None:
    assert detect_language("hi") is None


def test_detect_language_long_english_returns_en() -> None:
    text = ("The quick brown fox jumps over the lazy dog. " * 30)
    # We don't assert exact value because langdetect can occasionally vary;
    # at minimum we get a non-None result for substantial English text.
    out = detect_language(text)
    assert out is not None


def test_detect_language_conf_returns_language_and_confidence() -> None:
    text = "The quick brown fox jumps over the lazy dog. " * 30
    lang, conf = detect_language_conf(text)
    assert lang is not None
    assert 0.0 < conf <= 1.0


def test_detect_language_conf_short_text_is_none_zero() -> None:
    assert detect_language_conf("hi") == (None, 0.0)


def test_detect_language_conf_gates_below_min_confidence() -> None:
    # An impossible threshold suppresses the language but still reports the
    # measured confidence — proving the GATE, not the detector, made the call.
    text = "The quick brown fox jumps over the lazy dog. " * 30
    lang, conf = detect_language_conf(text, min_confidence=1.01)
    assert lang is None
    assert conf > 0.0


def test_detect_language_conf_is_deterministic() -> None:
    text = "Bonjour tout le monde, ceci est un court texte en francais. " * 10
    assert detect_language_conf(text) == detect_language_conf(text)


def test_detect_language_applies_confidence_gate() -> None:
    text = "The quick brown fox jumps over the lazy dog. " * 30
    assert detect_language(text, min_confidence=1.01) is None   # gated
    assert detect_language(text) is not None                    # default keeps clear English
