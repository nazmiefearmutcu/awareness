"""JSONL persistence for operator-recorded quality snapshots.

:class:`QualityStore` appends one JSON object per line to
``<data_dir>/quality_history.jsonl`` (written by ``awareness quality
--record``, typically cron-driven). JSONL is chosen over SQLite so a crash
mid-write can never corrupt history: a torn final line fails ``json.loads``
on the next read and is skipped, while every earlier line stays intact and
parseable.

The store is a *cache* of operator-recorded daily snapshots. The per-day
history served to users is computed directly from the corpus by
:class:`~awareness.qualityx.engine.QualityTimeEngine`, so a missing or
empty store never blocks history reads.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Recorded field -> QualitySnapshot field. ``total_captures`` /
# ``capture_rate_per_day`` / ``dedup_group_count`` are shortened so the
# JSONL line stays readable on the operator's disk.
_FIELD_MAP = {
    "total": "total_captures",
    "duplicate_ratio": "duplicate_ratio",
    "near_duplicate_ratio": "near_duplicate_ratio",
    "avg_length": "avg_length",
    "capture_rate": "capture_rate_per_day",
    "dedup_groups": "dedup_group_count",
}


class QualityStore:
    """Append-only JSONL store of recorded quality snapshots."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        """The JSONL file backing this store."""
        return self._path

    def record(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Append one line for *snapshot*; return the stored record.

        ``ts`` is stamped as UTC ISO-8601. The write is a single ``write``
        followed by ``flush``, so the line lands as one filesystem write; a
        crash mid-line still only tears the final line, which reads skip.
        """
        record: dict[str, Any] = {"ts": datetime.now(UTC).isoformat()}
        for field, source in _FIELD_MAP.items():
            record[field] = snapshot.get(source)
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
        return record

    def list(self, days: int | None = None) -> tuple[list[dict[str, Any]], int]:
        """Stored records (oldest first), optionally the trailing *days*.

        Returns ``(records, skipped)`` where *skipped* counts lines that
        failed to parse (torn final lines, foreign content) — those are
        dropped, never fatal. Well-formed ``ts`` strings are parsed to
        :class:`datetime`; unparseable ones stay as strings and are kept.
        """
        if not self._path.exists():
            return [], 0
        since: datetime | None = None
        if days is not None:
            since = datetime.now(UTC) - timedelta(days=max(int(days), 0))
        records: list[dict[str, Any]] = []
        skipped = 0
        with open(self._path, encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
                if not isinstance(rec, dict):
                    skipped += 1
                    continue
                ts = rec.get("ts")
                if isinstance(ts, str):
                    try:
                        rec["ts"] = datetime.fromisoformat(ts)
                    except ValueError:
                        pass
                if since is not None:
                    ts = rec.get("ts")
                    if isinstance(ts, datetime) and ts < since:
                        continue
                records.append(rec)
        return records, skipped

    def latest(self) -> dict[str, Any] | None:
        """Most recent stored record, or ``None`` when the store is empty."""
        records, _ = self.list()
        return records[-1] if records else None
