"""F-4: orphan-temp repair is atomic and gzip-boundary truncation is repaired.

``_drop_trailing_partial_record`` must never rewrite the truncated temp in
place: a crash mid-repair would destroy the very records H-11 meant to
preserve. The repaired content goes to a fresh ``*.repair`` sibling, is
fsync'd, and is ``os.replace``-d over the original — on any failure the
original stays untouched and no ``*.repair`` leftovers survive.

Also covers the gzip boundary-truncation case: a stream whose last record is
complete but whose EOS marker/trailer is missing must be rewritten into a
fresh VALID gzip (strict readers like ``gzip.decompress`` raise EOFError on
the truncated stream) instead of being promoted as-is.
"""

from __future__ import annotations

import gzip
import json
import zlib
from pathlib import Path

import pytest

from awareness.storage.jsonl import _drop_trailing_partial_record, recover_orphan_temps


def _write_gzip_temp(day: Path, name: str, records: list[dict]) -> Path:
    """Single-member gzip temp, mirroring the real writer (one handle)."""
    with gzip.open(day / name, "wb") as fh:
        for r in records:
            fh.write((json.dumps(r) + "\n").encode("utf-8"))
    return day / name


def _write_silent_truncated_gzip(day: Path, name: str, records: list[dict]) -> Path:
    """Gzip stream with complete records but NO stream-end marker/trailer.

    ``Z_SYNC_FLUSH`` ends the deflate stream at a clean block boundary, so on
    CPython < 3.13 iteration yields every line and then ends silently (no
    exception, no verified ``_eof``) — the exact "promote truncated gzip
    as-is" bug F-4 fixes. On 3.13+ the same file raises EOFError at the end of
    iteration; both paths must converge on a repair into a valid gzip.
    """
    co = zlib.compressobj(level=9, wbits=31)
    data = "".join(json.dumps(r) + "\n" for r in records).encode("utf-8")
    path = day / name
    path.write_bytes(co.compress(data) + co.flush(zlib.Z_SYNC_FLUSH))
    return path


def _read_gzip_jsonl(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _records(n: int) -> list[dict]:
    return [
        {"doc_id": f"d{i}", "text": "word " * (60 + i * 5), "fetch_ts": "2026-01-01T00:00:00+00:00"}
        for i in range(n)
    ]


# ── mid-line truncation ──────────────────────────────────────────────────


def test_mid_line_truncation_repairs_to_n_minus_one(tmp_path: Path) -> None:
    day = tmp_path / "captures" / "2026" / "01" / "01"
    day.mkdir(parents=True)
    tmp = day / "plain.jsonl.tmp"
    tmp.write_text(
        '{"doc_id": "d1"}\n'
        '{"doc_id": "d2"}\n'
        '{"doc_id": "d3", "text": "truncated-mid-li',
        encoding="utf-8",
    )

    promoted = recover_orphan_temps(tmp_path)
    assert len(promoted) == 1
    lines = [line for line in promoted[0].read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 2, "N-1 complete records must survive"
    assert [json.loads(line)["doc_id"] for line in lines] == ["d1", "d2"]


# ── repair failure leaves the original untouched ─────────────────────────


def test_repair_failure_leaves_original_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    day = tmp_path / "captures" / "2026" / "01" / "01"
    day.mkdir(parents=True)
    tmp = day / "plain.jsonl.tmp"
    original = (
        '{"doc_id": "d1"}\n'
        '{"doc_id": "d2"}\n'
        '{"doc_id": "d3", "text": "truncated-mid-li'
    )
    tmp.write_text(original, encoding="utf-8")
    before = tmp.read_bytes()

    def _boom(src: object, dst: object) -> None:
        raise OSError("simulated crash mid-repair")

    monkeypatch.setattr("os.replace", _boom)

    with pytest.raises(OSError):
        _drop_trailing_partial_record(tmp)

    # The original temp is byte-for-byte untouched, and no .repair file leaks.
    assert tmp.read_bytes() == before
    assert list(day.glob("*.repair")) == []


def test_repair_failure_on_gzip_leaves_original_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    day = tmp_path / "captures" / "2026" / "01" / "01"
    day.mkdir(parents=True)
    tmp = _write_silent_truncated_gzip(day, "cut.jsonl.gz.tmp", _records(3))
    before = tmp.read_bytes()

    def _boom(src: object, dst: object) -> None:
        raise OSError("simulated crash mid-repair")

    monkeypatch.setattr("os.replace", _boom)

    with pytest.raises(OSError):
        _drop_trailing_partial_record(tmp)

    assert tmp.read_bytes() == before
    assert list(day.glob("*.repair")) == []


# ── gzip boundary truncation (complete records, missing EOS) ─────────────


def test_gzip_boundary_truncation_missing_eos_promotes_valid_gzip(tmp_path: Path) -> None:
    day = tmp_path / "captures" / "2026" / "01" / "01"
    day.mkdir(parents=True)
    records = _records(3)
    tmp = _write_silent_truncated_gzip(day, "eos.jsonl.gz.tmp", records)

    # The truncated stream itself must be flagged as needing repair.
    assert _drop_trailing_partial_record(tmp) is True

    promoted = recover_orphan_temps(tmp_path)
    assert len(promoted) == 1
    final = promoted[0]
    assert final.suffix == ".gz"

    # The promoted file is a VALID gzip: gzip.decompress verifies the trailer
    # (EOS marker, CRC32, ISIZE) and raises EOFError on the truncated input.
    raw = final.read_bytes()
    assert gzip.decompress(raw) == "".join(json.dumps(r) + "\n" for r in records).encode("utf-8")
    assert _read_gzip_jsonl(final) == records


# ── crash-mid-repair simulation: temp+replace, no leftovers ──────────────


def test_repair_uses_repair_temp_then_replace_no_leftovers(tmp_path: Path) -> None:
    day = tmp_path / "captures" / "2026" / "01" / "01"
    day.mkdir(parents=True)
    tmp = day / "plain.jsonl.tmp"
    tmp.write_text(
        '{"doc_id": "d1"}\n'
        '{"doc_id": "d2"}\n'
        '{"doc_id": "d3", "text": "truncated-mid-li',
        encoding="utf-8",
    )

    promoted = recover_orphan_temps(tmp_path)
    assert len(promoted) == 1
    # No *.repair leftovers anywhere in the tree after a successful repair.
    assert list(tmp_path.rglob("*.repair")) == []
    lines = [line for line in promoted[0].read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 2


def test_gzip_repair_writes_valid_gzip_not_plain_text(tmp_path: Path) -> None:
    """A repaired gzip temp must remain a real gzip, not raw JSONL text."""
    day = tmp_path / "captures" / "2026" / "01" / "01"
    day.mkdir(parents=True)
    records = _records(40)
    tmp = _write_gzip_temp(day, "cut.jsonl.gz.tmp", records)
    raw = tmp.read_bytes()
    # Cut mid-stream: complete records before the cut, partial tail after.
    cut = int(len(raw) * 0.5)
    tmp.write_bytes(raw[:cut])

    assert _drop_trailing_partial_record(tmp) is True

    # Magic bytes prove it is gzip, and a strict read gets the records.
    assert tmp.read_bytes()[:2] == b"\x1f\x8b"
    recovered = _read_gzip_jsonl(tmp)
    assert len(recovered) >= 2
    assert recovered == records[: len(recovered)]
