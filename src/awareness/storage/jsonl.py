"""Atomic JSONL staging writer.

Each batch writes to ``<dir>/captures/<yyyy>/<mm>/<dd>/<batch_id>.jsonl.tmp``
and is renamed to ``.jsonl`` on commit so partial files are never readable.

Files are gzip-optional (.jsonl or .jsonl.gz) and rotate by size or count.

Design:
- One writer instance per job/worker.
- ``write()`` is thread-safe (instance lock); the underlying chunk file is owned.
- After each non-empty ``write()`` batch the open chunk is ``fsync``'d (crash-safe
  mid-chunk durability) without renaming — see ``sync()``.
- ``flush()`` finalizes the current chunk (rename to .jsonl) and rolls a new one.
- ``recover_orphan_temps()`` promotes leftover ``.tmp`` files after a crash.
- Always written before Iceberg/compaction. Iceberg can fail and we still have data.
"""

from __future__ import annotations

import gzip
import json
import os
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from awareness.obs.logging import get_logger
from awareness.obs.metrics import get_metrics

logger = get_logger("storage.jsonl")


def _serialize_value(v: Any) -> Any:
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=UTC)
        return v.astimezone(UTC).isoformat()
    return v


def _row_to_jsonl(row: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            {k: _serialize_value(v) for k, v in row.items()},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _is_jsonl_temp(path: Path) -> bool:
    """True for writer temps: ``*.jsonl.tmp`` or ``*.jsonl.gz.tmp``."""
    name = path.name
    return name.endswith(".jsonl.tmp") or name.endswith(".jsonl.gz.tmp")


def _finalized_path_for_temp(tmp: Path) -> Path:
    """Map a writer temp path to its committed name (strip trailing ``.tmp``)."""
    s = str(tmp)
    if s.endswith(".tmp"):
        return Path(s[:-4])
    return tmp


def _temp_has_valid_jsonl_record(tmp: Path) -> bool:
    """Return True if *tmp* contains at least one complete JSON object line.

    Gzip and plain temps are supported. A trailing partial line (mid-write crash)
    is ignored; empty or unreadable files return False.
    """
    try:
        if tmp.name.endswith(".jsonl.gz.tmp") or str(tmp).endswith(".jsonl.gz.tmp"):
            opener: Any = gzip.open
            mode = "rt"
        else:
            opener = open
            mode = "r"
        with opener(tmp, mode, encoding="utf-8") as fh:  # type: ignore[call-arg]
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj:
                    return True
    except (OSError, EOFError, gzip.BadGzipFile, UnicodeDecodeError):
        return False
    return False


def recover_orphan_temps(root: Path) -> list[Path]:
    """Promote leftover ``*.jsonl(.gz).tmp`` files after a crash.

    Temps with at least one valid JSONL record are renamed to the final name
    and the parent directory is fsync'd. Empty/corrupt temps are deleted.
    Returns the list of promoted (final) paths. Emits ``jsonl.orphan_*`` metrics.
    """
    root = Path(root)
    if not root.exists():
        return []
    promoted: list[Path] = []
    m = get_metrics()
    for tmp in sorted(root.rglob("*.tmp")):
        if not tmp.is_file() or not _is_jsonl_temp(tmp):
            continue
        if not _temp_has_valid_jsonl_record(tmp):
            try:
                tmp.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("jsonl_orphan_delete_failed", path=str(tmp), err=str(exc))
            m.inc("jsonl.orphans_removed")
            logger.info("jsonl_orphan_temp_removed", path=str(tmp))
            continue
        final = _finalized_path_for_temp(tmp)
        try:
            if final.exists():
                # Collision: keep the already-committed file, drop the temp.
                tmp.unlink(missing_ok=True)
                m.inc("jsonl.orphans_removed")
                logger.info(
                    "jsonl_orphan_temp_dropped_collision",
                    tmp=str(tmp),
                    final=str(final),
                )
                continue
            tmp.rename(final)
            try:
                dir_fd = os.open(str(final.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
            promoted.append(final)
            m.inc("jsonl.orphans_recovered")
            logger.info("jsonl_orphan_temp_recovered", path=str(final))
        except OSError as exc:
            logger.warning("jsonl_orphan_recover_failed", path=str(tmp), err=str(exc))
            m.inc("jsonl.orphans_recover_errors")
    return promoted


class JsonlStagingWriter:
    """Append-only, atomic, rotating JSONL writer."""

    def __init__(
        self,
        root: Path,
        max_records_per_file: int = 5_000,
        max_bytes_per_file: int = 64 * 1024 * 1024,
        compress: bool = False,
        flush_seconds: float = 10.0,
    ) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)
        self._max_records = max(1, max_records_per_file)
        self._max_bytes = max(64 * 1024, max_bytes_per_file)
        self._compress = compress
        self._flush_seconds = max(1.0, flush_seconds)

        self._lock = threading.RLock()
        self._fh: Any = None
        self._current_path: Path | None = None
        self._current_records = 0
        self._current_bytes = 0
        self._opened_at = 0.0
        self._committed_files: list[Path] = []

    # ------------------------------------------------------------------
    def _open_new(self) -> None:
        now = datetime.now(UTC)
        # Layout: <root>/captures/YYYY/MM/DD/*.jsonl
        day_dir = self._root / "captures" / f"{now.year:04d}" / f"{now.month:02d}" / f"{now.day:02d}"
        day_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".jsonl.gz" if self._compress else ".jsonl"
        name = f"captures-{int(now.timestamp() * 1000)}-{uuid.uuid4().hex[:8]}{suffix}.tmp"
        path = day_dir / name
        if self._compress:
            self._fh = gzip.open(path, "ab")
        else:
            # Buffered binary write; we use os.fsync in commit().
            self._fh = open(path, "ab", buffering=1024 * 64)
        self._current_path = path
        self._current_records = 0
        self._current_bytes = 0
        self._opened_at = time.time()

    def _fsync_handle(self) -> None:
        """Flush and fsync the open chunk (plain or gzip-wrapped).

        Plain files use ``fileno()`` directly. ``gzip.GzipFile`` has no fileno
        of its own — fsync the underlying file object so compressed staging is
        crash-safe before the atomic rename.
        """
        fh = self._fh
        if fh is None:
            return
        fh.flush()
        raw = fh
        if self._compress:
            # CPython GzipFile: prefer .fileobj (read) / .myfileobj (write path).
            raw = getattr(fh, "fileobj", None) or getattr(fh, "myfileobj", None) or fh
        try:
            if raw is not None and hasattr(raw, "fileno"):
                if hasattr(raw, "flush"):
                    raw.flush()
                os.fsync(raw.fileno())
        except (OSError, ValueError, AttributeError):
            # Closed / non-file handles — best-effort only.
            pass

    def _sync_unlocked(self) -> bool:
        """Fsync the open chunk without rename. Caller must hold ``_lock``.

        Makes mid-chunk records durable after a process crash while the file
        is still a ``.tmp`` (compaction only reads finalized names).
        """
        if self._fh is None or self._current_path is None:
            return False
        t0 = time.perf_counter()
        try:
            self._fsync_handle()
        except OSError as exc:
            logger.warning(
                "jsonl_sync_failed",
                path=str(self._current_path),
                err=str(exc),
            )
            get_metrics().inc("jsonl.syncs", labels={"outcome": "error"})
            return False
        elapsed = max(0.0, time.perf_counter() - t0)
        m = get_metrics()
        m.inc("jsonl.syncs", labels={"outcome": "ok"})
        m.observe("jsonl.sync_seconds", elapsed)
        m.set("jsonl.open_records", float(self._current_records))
        m.set("jsonl.open_bytes", float(self._current_bytes))
        return True

    def _update_open_gauges_unlocked(self) -> None:
        m = get_metrics()
        if self._fh is None:
            m.set("jsonl.open_records", 0.0)
            m.set("jsonl.open_bytes", 0.0)
        else:
            m.set("jsonl.open_records", float(self._current_records))
            m.set("jsonl.open_bytes", float(self._current_bytes))

    def _commit_current(self) -> Path | None:
        if self._fh is None or self._current_path is None:
            return None
        t0 = time.perf_counter()
        records = self._current_records
        nbytes = self._current_bytes
        try:
            self._fsync_handle()
        except OSError:
            pass
        try:
            self._fh.close()
        except OSError as exc:
            logger.warning("jsonl_close_failed", err=str(exc))
        self._fh = None

        # Rename .tmp → final
        finalized = self._current_path.with_suffix("") if str(self._current_path).endswith(".tmp") else self._current_path
        if str(self._current_path).endswith(".tmp"):
            finalized = Path(str(self._current_path)[:-4])
        try:
            self._current_path.rename(finalized)
        except OSError as exc:
            logger.warning("jsonl_rename_failed", src=str(self._current_path), err=str(exc))
            finalized = self._current_path

        # Durability: fsync the parent directory so the rename is durable.
        try:
            dir_fd = os.open(str(finalized.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass

        self._committed_files.append(finalized)
        self._current_path = None
        self._current_records = 0
        self._current_bytes = 0
        elapsed = max(0.0, time.perf_counter() - t0)
        # Process-local staging observability (mirrors Iceberg append metrics).
        m = get_metrics()
        m.inc("jsonl.chunks_committed")
        m.inc("jsonl.records_committed", value=float(records))
        m.inc("jsonl.bytes_committed", value=float(nbytes))
        m.observe("jsonl.commit_seconds", elapsed)
        self._update_open_gauges_unlocked()
        logger.info(
            "jsonl_chunk_committed",
            path=str(finalized),
            records=records,
            bytes=nbytes,
            seconds=round(elapsed, 4),
        )
        return finalized

    # ------------------------------------------------------------------
    def write(self, rows: list[dict[str, Any]]) -> int:
        """Append rows to the current chunk. Rotates when limits hit.

        After a non-empty batch, ``sync()`` fsyncs the open ``.tmp`` so records
        survive a crash before the next size/time rotate (crash-safe flush).
        """
        if not rows:
            return 0
        written = 0
        with self._lock:
            if self._fh is None:
                self._open_new()
            assert self._fh is not None
            for row in rows:
                payload = _row_to_jsonl(row)
                if self._should_rotate(payload):
                    self._commit_current()
                    self._open_new()
                    assert self._fh is not None
                self._fh.write(payload)
                self._current_records += 1
                self._current_bytes += len(payload)
                written += 1
            if self._should_rotate_time():
                self._commit_current()
            # Mid-chunk durability: fsync open temp without rename.
            if written and self._fh is not None:
                self._sync_unlocked()
        if written:
            get_metrics().inc("jsonl.records_written", value=float(written))
        return written

    def _should_rotate(self, next_payload: bytes) -> bool:
        if self._current_records >= self._max_records:
            return True
        if self._current_bytes + len(next_payload) > self._max_bytes:
            return True
        return self._should_rotate_time()

    def _should_rotate_time(self) -> bool:
        if self._opened_at == 0.0:
            return False
        return (time.time() - self._opened_at) >= self._flush_seconds

    # ------------------------------------------------------------------
    def sync(self) -> bool:
        """Fsync the open chunk without finalizing (crash-safe mid-chunk flush).

        Returns True when an open handle was successfully synced. No-op when
        no chunk is open. Prefer calling after batches; ``write()`` already
        does this automatically.
        """
        with self._lock:
            return self._sync_unlocked()

    def flush(self) -> Path | None:
        with self._lock:
            return self._commit_current()

    def close(self) -> Path | None:
        return self.flush()

    @property
    def committed_files(self) -> list[Path]:
        with self._lock:
            return list(self._committed_files)

    @property
    def open_records(self) -> int:
        """Records buffered in the open (unfinalized) chunk."""
        with self._lock:
            return int(self._current_records) if self._fh is not None else 0

    @property
    def open_bytes(self) -> int:
        """Bytes buffered in the open (unfinalized) chunk."""
        with self._lock:
            return int(self._current_bytes) if self._fh is not None else 0

    # ------------------------------------------------------------------
    # Context manager.
    def __enter__(self) -> JsonlStagingWriter:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()
