from __future__ import annotations

from pathlib import Path

from awareness.storage.gdrive import _build_multipart_body, _file_mime


def test_multipart_body_preserves_raw_bytes_and_mime() -> None:
    raw = b"\x1f\x8b\x08\x00rawgzipbytes"  # gzip magic + payload
    body = _build_multipart_body(
        {"name": "c.jsonl.gz", "parents": ["folder1"]}, raw, "application/gzip", "BOUND"
    )
    assert isinstance(body, bytes)
    assert raw in body
    assert b"Content-Type: application/gzip" in body
    assert b'"name": "c.jsonl.gz"' in body
    assert body.startswith(b"--BOUND\r\n")
    assert body.rstrip().endswith(b"--BOUND--")


def test_file_mime_by_extension() -> None:
    assert _file_mime(Path("c.jsonl.gz")) == "application/gzip"
    assert _file_mime(Path("c.jsonl")) == "application/x-ndjson"
