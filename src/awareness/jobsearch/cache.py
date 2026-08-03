"""Short-TTL disk (+ memory) cache for job-search HTTP payloads.

Used by LinkedIn guest search / detail enrichment to cut rate risk and latency.
Keys are sha256 of kind+params; values are JSON-serializable (usually HTML text).

Disk writes are atomic (tmp + ``os.replace``) so a crash mid-write never leaves
a truncated cache entry that would be served as a partial payload (L-03).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

# Search result pages go stale quickly; job details are more stable.
SEARCH_TTL_SEC = 20 * 60  # 20 minutes
DETAIL_TTL_SEC = 2 * 60 * 60  # 2 hours


def _stable_key(kind: str, params: dict[str, Any]) -> str:
    blob = json.dumps({"kind": kind, "params": params}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:40]


def _sweep_expired(root: Path, now: float | None = None) -> None:
    """Delete expired cache files on startup (L-03) so stale HTML is never
    served after a process restart."""
    now = time.time() if now is None else now
    try:
        for path in root.glob("*.json"):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                exp = float(data.get("expires_at") or 0)
            except (OSError, json.JSONDecodeError, UnicodeError, ValueError):
                continue
            if exp < now:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
    except OSError:
        pass


class JobSearchCache:
    """Disk cache under ``{data_dir}/jobsearch_cache/`` with a small memory layer."""

    def __init__(self, data_dir: Path | str) -> None:
        self.root = Path(data_dir) / "jobsearch_cache"
        self.root.mkdir(parents=True, exist_ok=True)
        # key -> (expires_at, value)
        self._mem: dict[str, tuple[float, Any]] = {}
        _sweep_expired(self.root)

    def get(self, kind: str, params: dict[str, Any]) -> Any | None:
        key = _stable_key(kind, params)
        now = time.time()

        hit = self._mem.get(key)
        if hit is not None:
            exp, val = hit
            if now < exp:
                return val
            self._mem.pop(key, None)

        path = self.root / f"{key}.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            return None
        exp = float(data.get("expires_at") or 0)
        if now >= exp:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return None
        val = data.get("value")
        self._mem[key] = (exp, val)
        return val

    def set(self, kind: str, params: dict[str, Any], value: Any, ttl_sec: int) -> None:
        key = _stable_key(kind, params)
        expires_at = time.time() + max(1, int(ttl_sec))
        self._mem[key] = (expires_at, value)
        path = self.root / f"{key}.json"
        payload = {
            "kind": kind,
            "params": params,
            "expires_at": expires_at,
            "value": value,
        }
        try:
            # Atomic write (L-03): write to a temp file in the same dir, fsync
            # via close, then rename over the target.
            fd, tmp = tempfile.mkstemp(dir=self.root, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False)
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            # Memory still holds the entry for this process.
            pass

    def clear_memory(self) -> None:
        self._mem.clear()
