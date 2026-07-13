"""Feed/sitemap body charset decoding (Content-Type aware)."""

from __future__ import annotations

import json

import httpx
import pytest

from awareness.obs.metrics import get_metrics
from awareness.sources.feeds import (
    _body_for_xml_parser,
    decode_feed_text,
    parse_json_feed_urls,
    _read_feed,
)


def test_decode_feed_text_honors_content_type_latin1() -> None:
    body = '{"version":"https://jsonfeed.org/version/1","items":[]}'.encode("latin-1")
    # Pure ASCII still latin-1-encodable; force non-utf8 path with accented key noise
    body = ('{"version":"https://jsonfeed.org/version/1","title":"Café","items":[]}').encode(
        "latin-1"
    )
    text, enc = decode_feed_text(body, content_type="application/feed+json; charset=iso-8859-1")
    assert enc == "latin-1"
    assert "Café" in text


def test_parse_json_feed_urls_latin1_content_type() -> None:
    doc = {
        "version": "https://jsonfeed.org/version/1",
        "title": "Café feed",
        "items": [
            {"id": "1", "url": "https://example.com/café/1", "title": "One"},
        ],
    }
    body = json.dumps(doc, ensure_ascii=False).encode("latin-1")
    urls = parse_json_feed_urls(
        body,
        content_type="application/json; charset=ISO-8859-1",
    )
    assert urls == ["https://example.com/café/1"]


def test_parse_json_feed_urls_still_rejects_xml_bytes() -> None:
    assert parse_json_feed_urls(b"<?xml version='1.0'?><rss/>") is None


def test_body_for_xml_parser_returns_unicode_for_latin1() -> None:
    # Minimal RSS with non-ascii title, no XML encoding declaration.
    xml = (
        '<?xml version="1.0"?>'
        "<rss><channel><title>Café</title>"
        "<item><link>https://example.com/a</link></item>"
        "</channel></rss>"
    ).encode("latin-1")
    out = _body_for_xml_parser(
        xml,
        content_type="application/rss+xml; charset=iso-8859-1",
        kind="rss",
    )
    assert isinstance(out, str)
    assert "Café" in out


def test_body_for_xml_parser_keeps_utf8_bytes() -> None:
    xml = b'<?xml version="1.0"?><rss><channel></channel></rss>'
    out = _body_for_xml_parser(xml, content_type="application/rss+xml; charset=utf-8", kind="rss")
    assert isinstance(out, (bytes, bytearray))


@pytest.mark.asyncio
async def test_read_feed_json_latin1(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: latin-1 JSON Feed via Content-Type is not dropped."""
    from tests.unit.test_feeds_http_retry import _patch_client_and_retries  # noqa: PLC0415

    doc = {
        "version": "https://jsonfeed.org/version/1",
        "items": [{"id": "1", "url": "https://example.com/lat/1", "title": "Hôtel"}],
    }
    raw = json.dumps(doc, ensure_ascii=False).encode("latin-1")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=raw,
            headers={"Content-Type": "application/feed+json; charset=iso-8859-1"},
        )

    _patch_client_and_retries(monkeypatch, handler, module="awareness.sources.feeds")
    before = get_metrics().counter_sum("feeds.decode_charset")
    urls = await _read_feed("https://example.com/feed.json", "TestBot/1.0")
    assert urls == ["https://example.com/lat/1"]
    assert get_metrics().counter_sum("feeds.decode_charset") > before
