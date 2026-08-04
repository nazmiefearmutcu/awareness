from __future__ import annotations

import pytest

from awareness.storage.state import _verify_dedup_schema


class _FakeInspector:
    def __init__(self, columns: list[str]) -> None:
        self._columns = columns

    def get_columns(self, table: str):
        return [{"name": c} for c in self._columns]


def test_missing_sig_hex_raises() -> None:
    insp = _FakeInspector(["id", "doc_id", "seg", "seg_value"])  # no sig_hex
    with pytest.raises(RuntimeError):
        _verify_dedup_schema(insp)


def test_present_sig_hex_passes() -> None:
    insp = _FakeInspector(
        ["id", "doc_id", "sig_hex", "token_hash", "token_count", "seg", "seg_value"]
    )
    _verify_dedup_schema(insp)  # must not raise


def test_missing_token_sketch_raises() -> None:
    """W19: the token-set sketch columns are required after migration — a DB
    missing token_hash/token_count would fail the content-diversity guard's
    SELECT with a confusing 'no such column' on every dedup write."""
    insp = _FakeInspector(["id", "doc_id", "sig_hex", "seg", "seg_value"])
    with pytest.raises(RuntimeError, match="token_hash"):
        _verify_dedup_schema(insp)
    insp2 = _FakeInspector(["id", "doc_id", "sig_hex", "token_hash", "seg", "seg_value"])
    with pytest.raises(RuntimeError, match="token_count"):
        _verify_dedup_schema(insp2)


def test_no_table_yet_passes() -> None:
    _verify_dedup_schema(_FakeInspector([]))  # no columns → must not raise
