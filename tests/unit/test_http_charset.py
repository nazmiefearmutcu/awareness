"""Charset-aware HTTP body decoding (Content-Type, HTML meta, detector)."""

from __future__ import annotations

from awareness.util.http import (
    charset_from_content_type,
    charset_from_html_meta,
    decode_http_text,
    normalize_charset_label,
)


def test_normalize_charset_aliases() -> None:
    assert normalize_charset_label("UTF-8") == "utf-8"
    assert normalize_charset_label("utf8") == "utf-8"
    assert normalize_charset_label("ISO-8859-1") == "latin-1"
    assert normalize_charset_label("windows-1252") == "cp1252"
    assert normalize_charset_label("  ") is None
    assert normalize_charset_label(None) is None


def test_charset_from_content_type() -> None:
    assert charset_from_content_type("text/html; charset=utf-8") == "utf-8"
    assert charset_from_content_type('text/html; charset="ISO-8859-1"') == "latin-1"
    assert charset_from_content_type("text/html; charset=windows-1252") == "cp1252"
    assert charset_from_content_type("text/html") is None
    assert charset_from_content_type(None) is None


def test_charset_from_html_meta_charset_attr() -> None:
    html = b'<!doctype html><html><head><meta charset="iso-8859-1"><title>x</title></head>'
    assert charset_from_html_meta(html) == "latin-1"


def test_charset_from_html_meta_http_equiv() -> None:
    html = (
        b'<html><head><meta http-equiv="Content-Type" '
        b'content="text/html; charset=windows-1252"></head>'
    )
    assert charset_from_html_meta(html) == "cp1252"


def test_decode_prefers_content_type_over_meta() -> None:
    # Body is valid latin-1; header says latin-1; meta lies with utf-8.
    body = "Hôtel".encode("latin-1")
    html = (
        b'<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>'
        + body
        + b"</body></html>"
    )
    text, enc = decode_http_text(
        html,
        content_type="text/html; charset=iso-8859-1",
        use_detector=False,
    )
    assert enc == "latin-1"
    assert "Hôtel" in text


def test_decode_uses_html_meta_when_no_header() -> None:
    body = "Café".encode("latin-1")
    html = (
        b'<!DOCTYPE html><html><head><meta charset="iso-8859-1"></head><body><p>'
        + body
        + b"</p></body></html>"
    )
    text, enc = decode_http_text(html, content_type="text/html", use_detector=False)
    assert enc == "latin-1"
    assert "Café" in text


def test_decode_utf8_bom() -> None:
    body = b"\xef\xbb\xbfhello"
    text, enc = decode_http_text(body, content_type=None, use_detector=False)
    assert text == "hello"
    assert enc == "utf-8"


def test_decode_empty() -> None:
    text, enc = decode_http_text(b"")
    assert text == ""
    assert enc == "utf-8"


def test_decode_clean_utf8_without_charset() -> None:
    body = "hello 世界".encode("utf-8")
    text, enc = decode_http_text(body, content_type="text/html", use_detector=False)
    assert text == "hello 世界"
    assert enc == "utf-8"


def test_decode_latin1_via_header_for_tail_path() -> None:
    """Regression pin: tail recrawl should not mojibake Windows-1252 bodies."""
    raw = "naïve — café".encode("cp1252")
    html = b"<html><body><p>" + raw + b"</p></body></html>"
    text, enc = decode_http_text(
        html,
        content_type="text/html; charset=windows-1252",
        use_detector=False,
    )
    assert enc == "cp1252"
    assert "naïve" in text
    assert "café" in text
