from __future__ import annotations

import builtins
from datetime import UTC, datetime

import pytest

from awareness.schemas.doc import SourceKind
from awareness.schemas.jobs import BackfillRequest
from awareness.sources.fineweb import FineWebAdapter, FineWebDependencyMissing


def test_explicit_fineweb_without_datasets_raises(monkeypatch) -> None:
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "datasets":
            raise ImportError("no datasets")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    adapter = FineWebAdapter()
    req = BackfillRequest(
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 2, 1, tzinfo=UTC),
        sources=[SourceKind.FINEWEB],
    )
    with pytest.raises(FineWebDependencyMissing):
        adapter.plan(req)
