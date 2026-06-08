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
    insp = _FakeInspector(["id", "doc_id", "sig_hex", "seg", "seg_value"])
    _verify_dedup_schema(insp)  # must not raise


def test_no_table_yet_passes() -> None:
    _verify_dedup_schema(_FakeInspector([]))  # no columns → must not raise
